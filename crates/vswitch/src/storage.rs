//! Immutable `Test Scene` and per-run `Run Output` package storage.
//!
//! Binary- and manifest-compatible with `src/chongming_scorer/storage.py`.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::framing::MAX_FRAME_BYTES;
use crate::pbzst::{iter_pb_zst, PbZstError, PbZstReader, PbZstWriter, DEFAULT_ZSTD_LEVEL};
use crate::proto::{
    BusMessage, CameraDistortion, CameraIntrinsics, CameraModel, FrameIndexEntry, GeodeticAnchor,
    InstallationStateRef, InstallationUncertainty, Mat3, RayEvent, RigidTransform, SparseVoxelGrid,
    StationConfig, SurveyedLandmark, SurveyedReference, TruthFrame, Vec3,
};

pub const TEST_SCENE_FORMAT: &str = "chongming-test-scene-v1";
pub const RUN_OUTPUT_FORMAT: &str = "chongming-run-output-v1";
pub const VALID_RUN_MODES: &[&str] = &["edge", "center", "integrated"];
pub const REQUIRED_SCENE_STREAMS: &[&str] =
    &["frame_index.pb.zst", "truth.pb.zst", "virtual_rays.pb.zst"];

#[derive(Debug, Error)]
pub enum StorageError {
    #[error("path already exists: {0}")]
    AlreadyExists(PathBuf),
    #[error("missing package file or directory: {0}")]
    NotFound(PathBuf),
    #[error("invalid package relative path {0:?}")]
    InvalidRelpath(String),
    #[error("package file must not be a symlink: {0}")]
    ForbiddenSymlink(String),
    #[error("package path escapes root: {0}")]
    PathEscape(String),
    #[error("invalid manifest: {0}")]
    InvalidManifest(String),
    #[error("manifest content_sha256 mismatch: expected {expected}, got {actual}")]
    ContentSha256Mismatch { expected: String, actual: String },
    #[error("byte count mismatch for {relpath}: expected {expected}, got {actual}")]
    ByteCountMismatch {
        relpath: String,
        expected: u64,
        actual: u64,
    },
    #[error("sha256 mismatch for {relpath}: expected {expected}, got {actual}")]
    FileSha256Mismatch {
        relpath: String,
        expected: String,
        actual: String,
    },
    #[error("storage writer is already closed or finalized")]
    WriterClosed,
    #[error("pb.zst error: {0}")]
    PbZst(#[from] PbZstError),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileDigestEntry {
    pub sha256: String,
    pub bytes: u64,
}

/// D3b default encoding preset constants matching `docs/protocol.md`.
pub const DEFAULT_D3B_PRESET_ID: &str = "d3b-yuv420p-crf18-max6m-v1";
pub const DEFAULT_D3B_CODEC: &str = "libx264";
pub const DEFAULT_D3B_CONTAINER: &str = "mp4";
pub const DEFAULT_D3B_PIXEL_FORMAT: &str = "yuv420p";
pub const DEFAULT_D3B_X264_PRESET: &str = "medium";
pub const DEFAULT_D3B_CRF: u32 = 18;
pub const DEFAULT_D3B_MAXRATE_KBPS: u32 = 6000;
pub const DEFAULT_D3B_BUFSIZE_KBPS: u32 = 12000;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EncodingPresetSpec {
    pub preset_id: String,
    pub codec: String,
    pub container: String,
    pub encoded_pixel_format: String,
    pub x264_preset: String,
    pub crf: Option<u32>,
    pub maxrate_kbps: Option<u32>,
    pub bufsize_kbps: Option<u32>,
    pub target_bitrate_kbps: Option<u32>,
}

impl Default for EncodingPresetSpec {
    fn default() -> Self {
        Self {
            preset_id: DEFAULT_D3B_PRESET_ID.to_string(),
            codec: DEFAULT_D3B_CODEC.to_string(),
            container: DEFAULT_D3B_CONTAINER.to_string(),
            encoded_pixel_format: DEFAULT_D3B_PIXEL_FORMAT.to_string(),
            x264_preset: DEFAULT_D3B_X264_PRESET.to_string(),
            crf: Some(DEFAULT_D3B_CRF),
            maxrate_kbps: Some(DEFAULT_D3B_MAXRATE_KBPS),
            bufsize_kbps: Some(DEFAULT_D3B_BUFSIZE_KBPS),
            target_bitrate_kbps: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct VideoTrackMeta {
    pub station_id: String,
    pub camera_role: String,
    pub codec: String,
    pub container: String,
    pub fps: f64,
    pub frame_count: u64,
    pub width_px: u32,
    pub height_px: u32,
    pub pixel_format: String,
    pub encoded_pixel_format: String,
    pub quality: String,
    pub average_bitrate_kbps: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub encoding_preset: Option<EncodingPresetSpec>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

/// Compute lowercase hex SHA-256 digest of a file using streaming reads.
pub fn sha256_file(path: impl AsRef<Path>) -> Result<String, StorageError> {
    let file = File::open(path.as_ref())?;
    let mut reader = BufReader::with_capacity(64 * 1024, file);
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Atomically write JSON via a sibling `.tmp` file and rename.
pub fn write_json_atomic(
    path: impl AsRef<Path>,
    value: &Value,
    exclusive: bool,
) -> Result<(), StorageError> {
    let path = path.as_ref();
    if exclusive && path.exists() {
        return Err(StorageError::AlreadyExists(path.to_path_buf()));
    }
    let mut payload = serde_json::to_string_pretty(value)?;
    payload.push('\n');

    let tmp_name = match path.file_name().and_then(|n| n.to_str()) {
        Some(name) => format!("{name}.tmp"),
        None => return Err(StorageError::InvalidManifest("invalid target path".into())),
    };
    let tmp_path = path.with_file_name(tmp_name);

    let mut opts = OpenOptions::new();
    opts.write(true);
    if exclusive {
        opts.create_new(true);
    } else {
        opts.create(true).truncate(true);
    }
    {
        let mut file = opts.open(&tmp_path)?;
        file.write_all(payload.as_bytes())?;
        file.flush()?;
    }

    if exclusive && path.exists() {
        let _ = fs::remove_file(&tmp_path);
        return Err(StorageError::AlreadyExists(path.to_path_buf()));
    }
    fs::rename(&tmp_path, path)?;
    Ok(())
}

/// Format an `f64` using Python `json.dumps` (`repr`) rules for canonical hash parity.
fn format_python_f64(value: f64) -> Result<String, StorageError> {
    if !value.is_finite() {
        return Err(StorageError::InvalidManifest(
            "non-finite float in manifest".to_string(),
        ));
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        });
    }
    let neg = value.is_sign_negative();
    let abs = value.abs();
    // Use serde_json (zmij/ryu) to obtain the shortest exact decimal string.
    let raw = serde_json::to_string(&abs)?;

    // Parse mantissa digits and base-10 exponent from `raw` (either decimal or scientific).
    let (digits, exp): (String, i32) = if let Some((mant, exp_str)) = raw.split_once('e') {
        let e: i32 = exp_str.parse().map_err(|_| {
            StorageError::InvalidManifest(format!("unexpected float format: {raw}"))
        })?;
        let dot_pos = mant.find('.');
        let d: String = mant.chars().filter(|&c| c != '.').collect();
        let int_digits = dot_pos.unwrap_or(d.len()) as i32;
        let trimmed = d.trim_end_matches('0');
        let leading_zeros = trimmed.chars().take_while(|&c| c == '0').count() as i32;
        let sig = trimmed[leading_zeros as usize..].to_string();
        (sig, e + int_digits - 1 - leading_zeros)
    } else if let Some((int_part, frac_part)) = raw.split_once('.') {
        if int_part == "0" {
            let leading_zeros = frac_part.chars().take_while(|&c| c == '0').count() as i32;
            let sig = frac_part[leading_zeros as usize..]
                .trim_end_matches('0')
                .to_string();
            (sig, -(leading_zeros + 1))
        } else {
            let combined = format!("{int_part}{frac_part}");
            let sig = combined.trim_end_matches('0').to_string();
            (sig, int_part.len() as i32 - 1)
        }
    } else {
        let sig = raw.trim_end_matches('0').to_string();
        (sig, raw.len() as i32 - 1)
    };

    let mut out = String::new();
    if neg {
        out.push('-');
    }
    if !(-4..16).contains(&exp) {
        let first = &digits[..1];
        let rest = &digits[1..];
        out.push_str(first);
        if !rest.is_empty() {
            out.push('.');
            out.push_str(rest);
        }
        if exp >= 0 {
            out.push_str(&format!("e+{exp:02}"));
        } else {
            out.push_str(&format!("e-{:02}", -exp));
        }
    } else if exp < 0 {
        out.push_str("0.");
        for _ in 0..(-exp - 1) {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        let int_len = (exp + 1) as usize;
        if digits.len() <= int_len {
            out.push_str(&digits);
            for _ in 0..(int_len - digits.len()) {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..int_len]);
            out.push('.');
            out.push_str(&digits[int_len..]);
        }
    }
    Ok(out)
}

fn write_canonical_json(value: &Value, out: &mut String) -> Result<(), StorageError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else if let Some(f) = n.as_f64() {
                out.push_str(&format_python_f64(f)?);
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => {
            out.push_str(&serde_json::to_string(s)?);
        }
        Value::Array(arr) => {
            out.push('[');
            for (idx, item) in arr.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                write_canonical_json(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let sorted: BTreeMap<&str, &Value> = map.iter().map(|(k, v)| (k.as_str(), v)).collect();
            out.push('{');
            for (idx, (k, v)) in sorted.iter().enumerate() {
                if idx > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(k)?);
                out.push(':');
                write_canonical_json(v, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// Compute `content_sha256` matching Python `canonical_manifest_sha256`.
pub fn canonical_manifest_sha256(manifest: &Value) -> Result<String, StorageError> {
    let obj = manifest
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("manifest must be a JSON object".into()))?;
    let mut filtered = Map::new();
    for (k, v) in obj {
        if k != "content_sha256" {
            filtered.insert(k.clone(), v.clone());
        }
    }
    let mut encoded = String::new();
    write_canonical_json(&Value::Object(filtered), &mut encoded)?;
    let digest = Sha256::digest(encoded.as_bytes());
    Ok(hex::encode(digest))
}

/// Validate a package-relative POSIX path (rejecting traversal, backslashes, or absolute paths).
pub fn validate_relpath(relpath: &str) -> Result<Vec<String>, StorageError> {
    if relpath.is_empty() || relpath.contains('\\') || relpath.starts_with('/') {
        return Err(StorageError::InvalidRelpath(relpath.to_string()));
    }
    let parts: Vec<String> = relpath.split('/').map(ToString::to_string).collect();
    if parts.iter().any(|p| p.is_empty() || p == "." || p == "..") {
        return Err(StorageError::InvalidRelpath(relpath.to_string()));
    }
    Ok(parts)
}

fn resolve_package_file(
    root: &Path,
    relpath: &str,
    allow_symlink: bool,
) -> Result<PathBuf, StorageError> {
    let parts = validate_relpath(relpath)?;
    let root_resolved = fs::canonicalize(root)?;
    let mut candidate = root.to_path_buf();
    for part in &parts {
        candidate.push(part);
    }

    let meta = fs::symlink_metadata(&candidate).map_err(|err| {
        if err.kind() == io::ErrorKind::NotFound {
            StorageError::NotFound(candidate.clone())
        } else {
            StorageError::Io(err)
        }
    })?;
    if !allow_symlink && meta.file_type().is_symlink() {
        return Err(StorageError::ForbiddenSymlink(relpath.to_string()));
    }

    if allow_symlink {
        let parent = candidate
            .parent()
            .ok_or_else(|| StorageError::PathEscape(relpath.to_string()))?;
        let parent_resolved = fs::canonicalize(parent)?;
        if !parent_resolved.starts_with(&root_resolved) {
            return Err(StorageError::PathEscape(relpath.to_string()));
        }
    } else {
        let resolved = fs::canonicalize(&candidate)?;
        if !resolved.starts_with(&root_resolved) {
            return Err(StorageError::PathEscape(relpath.to_string()));
        }
    }

    if !candidate.is_file() {
        return Err(StorageError::NotFound(candidate));
    }
    Ok(candidate)
}

fn verify_file_entry(
    root: &Path,
    relpath: &str,
    entry: &Value,
    allow_symlink: bool,
    check_digest: bool,
) -> Result<PathBuf, StorageError> {
    let path = resolve_package_file(root, relpath, allow_symlink)?;
    if check_digest {
        let obj = entry.as_object().ok_or_else(|| {
            StorageError::InvalidManifest(format!("file entry for {relpath} must be an object"))
        })?;
        let expected_bytes = obj.get("bytes").and_then(Value::as_u64).ok_or_else(|| {
            StorageError::InvalidManifest(format!("invalid byte count in manifest for {relpath}"))
        })?;
        let expected_sha = obj
            .get("sha256")
            .and_then(Value::as_str)
            .filter(|s| s.len() == 64)
            .ok_or_else(|| {
                StorageError::InvalidManifest(format!("invalid sha256 in manifest for {relpath}"))
            })?;
        let actual_bytes = fs::metadata(&path)?.len();
        if actual_bytes != expected_bytes {
            return Err(StorageError::ByteCountMismatch {
                relpath: relpath.to_string(),
                expected: expected_bytes,
                actual: actual_bytes,
            });
        }
        let actual_sha = sha256_file(&path)?;
        if actual_sha != expected_sha {
            return Err(StorageError::FileSha256Mismatch {
                relpath: relpath.to_string(),
                expected: expected_sha.to_string(),
                actual: actual_sha,
            });
        }
    }
    Ok(path)
}

// --- StationConfig <-> Protobuf JSON (`preserving_proto_field_name=True`) ---

fn vec3_to_json(v: &Vec3) -> Value {
    let mut m = Map::new();
    if v.x != 0.0 {
        m.insert("x".into(), Value::from(v.x));
    }
    if v.y != 0.0 {
        m.insert("y".into(), Value::from(v.y));
    }
    if v.z != 0.0 {
        m.insert("z".into(), Value::from(v.z));
    }
    Value::Object(m)
}

fn mat3_to_json(mat: &Mat3) -> Value {
    let mut m = Map::new();
    if !mat.values.is_empty() {
        m.insert(
            "values".into(),
            Value::Array(mat.values.iter().copied().map(Value::from).collect()),
        );
    }
    Value::Object(m)
}

fn geodetic_anchor_to_json(g: &GeodeticAnchor) -> Value {
    let mut m = Map::new();
    if !g.anchor_id.is_empty() {
        m.insert("anchor_id".into(), Value::from(g.anchor_id.as_str()));
    }
    if !g.datum.is_empty() {
        m.insert("datum".into(), Value::from(g.datum.as_str()));
    }
    if !g.datum_epoch.is_empty() {
        m.insert("datum_epoch".into(), Value::from(g.datum_epoch.as_str()));
    }
    if g.latitude_deg != 0.0 {
        m.insert("latitude_deg".into(), Value::from(g.latitude_deg));
    }
    if g.longitude_deg != 0.0 {
        m.insert("longitude_deg".into(), Value::from(g.longitude_deg));
    }
    if g.height_m != 0.0 {
        m.insert("height_m".into(), Value::from(g.height_m));
    }
    if !g.height_reference.is_empty() {
        m.insert(
            "height_reference".into(),
            Value::from(g.height_reference.as_str()),
        );
    }
    Value::Object(m)
}

fn camera_intrinsics_to_json(c: &CameraIntrinsics) -> Value {
    let mut m = Map::new();
    if c.image_width_px != 0 {
        m.insert("image_width_px".into(), Value::from(c.image_width_px));
    }
    if c.image_height_px != 0 {
        m.insert("image_height_px".into(), Value::from(c.image_height_px));
    }
    if let Some(mat) = &c.matrix_px {
        m.insert("matrix_px".into(), mat3_to_json(mat));
    }
    Value::Object(m)
}

fn camera_distortion_to_json(d: &CameraDistortion) -> Value {
    let mut m = Map::new();
    if !d.model.is_empty() {
        m.insert("model".into(), Value::from(d.model.as_str()));
    }
    if !d.coefficients.is_empty() {
        m.insert(
            "coefficients".into(),
            Value::Array(d.coefficients.iter().copied().map(Value::from).collect()),
        );
    }
    Value::Object(m)
}

fn camera_model_to_json(cam: &CameraModel) -> Value {
    let mut m = Map::new();
    if let Some(intr) = &cam.intrinsics {
        m.insert("intrinsics".into(), camera_intrinsics_to_json(intr));
    }
    if let Some(dist) = &cam.distortion {
        m.insert("distortion".into(), camera_distortion_to_json(dist));
    }
    Value::Object(m)
}

fn rigid_transform_to_json(rt: &RigidTransform) -> Value {
    let mut m = Map::new();
    if let Some(rot) = &rt.rotation_a_from_b {
        m.insert("rotation_a_from_b".into(), mat3_to_json(rot));
    }
    if let Some(t) = &rt.translation_a_m {
        m.insert("translation_a_m".into(), vec3_to_json(t));
    }
    Value::Object(m)
}

fn installation_state_ref_to_json(r: &InstallationStateRef) -> Value {
    let mut m = Map::new();
    if !r.station_id.is_empty() {
        m.insert("station_id".into(), Value::from(r.station_id.as_str()));
    }
    if !r.config_digest.is_empty() {
        m.insert(
            "config_digest".into(),
            Value::from(r.config_digest.as_str()),
        );
    }
    if !r.state_version.is_empty() {
        m.insert(
            "state_version".into(),
            Value::from(r.state_version.as_str()),
        );
    }
    Value::Object(m)
}

fn installation_uncertainty_to_json(u: &InstallationUncertainty) -> Value {
    let mut m = Map::new();
    if let Some(sr) = &u.state_ref {
        m.insert("state_ref".into(), installation_state_ref_to_json(sr));
    }
    if let Some(p) = &u.position_covariance_enu_m2 {
        m.insert("position_covariance_enu_m2".into(), mat3_to_json(p));
    }
    if let Some(o) = &u.orientation_covariance_enu_rad2 {
        m.insert("orientation_covariance_enu_rad2".into(), mat3_to_json(o));
    }
    if let Some(f) = &u.fixed_extrinsic_covariance_aux_rad2 {
        m.insert(
            "fixed_extrinsic_covariance_aux_rad2".into(),
            mat3_to_json(f),
        );
    }
    if !u.measurement_source.is_empty() {
        m.insert(
            "measurement_source".into(),
            Value::from(u.measurement_source.as_str()),
        );
    }
    Value::Object(m)
}

fn surveyed_landmark_to_json(lm: &SurveyedLandmark) -> Value {
    let mut m = Map::new();
    if !lm.landmark_id.is_empty() {
        m.insert("landmark_id".into(), Value::from(lm.landmark_id.as_str()));
    }
    if let Some(p) = &lm.position_enu_m {
        m.insert("position_enu_m".into(), vec3_to_json(p));
    }
    if let Some(cov) = &lm.covariance_enu_m2 {
        m.insert("covariance_enu_m2".into(), mat3_to_json(cov));
    }
    if !lm.visual_description.is_empty() {
        m.insert(
            "visual_description".into(),
            Value::from(lm.visual_description.as_str()),
        );
    }
    Value::Object(m)
}

fn surveyed_reference_to_json(r: &SurveyedReference) -> Value {
    let mut m = Map::new();
    if !r.reference_id.is_empty() {
        m.insert("reference_id".into(), Value::from(r.reference_id.as_str()));
    }
    if !r.world_frame_id.is_empty() {
        m.insert(
            "world_frame_id".into(),
            Value::from(r.world_frame_id.as_str()),
        );
    }
    if !r.landmarks.is_empty() {
        m.insert(
            "landmarks".into(),
            Value::Array(r.landmarks.iter().map(surveyed_landmark_to_json).collect()),
        );
    }
    Value::Object(m)
}

/// Convert `StationConfig` to JSON matching `MessageToDict(..., preserving_proto_field_name=True)`.
pub fn station_config_to_json(s: &StationConfig) -> Value {
    let mut m = Map::new();
    if !s.station_id.is_empty() {
        m.insert("station_id".into(), Value::from(s.station_id.as_str()));
    }
    if !s.config_version.is_empty() {
        m.insert(
            "config_version".into(),
            Value::from(s.config_version.as_str()),
        );
    }
    if !s.config_digest.is_empty() {
        m.insert(
            "config_digest".into(),
            Value::from(s.config_digest.as_str()),
        );
    }
    if !s.world_frame_id.is_empty() {
        m.insert(
            "world_frame_id".into(),
            Value::from(s.world_frame_id.as_str()),
        );
    }
    if let Some(g) = &s.geodetic_anchor {
        m.insert("geodetic_anchor".into(), geodetic_anchor_to_json(g));
    }
    if let Some(p) = &s.main_camera_position_enu_m {
        m.insert("main_camera_position_enu_m".into(), vec3_to_json(p));
    }
    if let Some(b) = &s.baseline_orientation_enu_from_main {
        m.insert("baseline_orientation_enu_from_main".into(), mat3_to_json(b));
    }
    if let Some(cam) = &s.main_camera {
        m.insert("main_camera".into(), camera_model_to_json(cam));
    }
    if let Some(aux) = &s.auxiliary_camera {
        m.insert("auxiliary_camera".into(), camera_model_to_json(aux));
    }
    if let Some(rot) = &s.fixed_rotation_aux_from_main {
        m.insert("fixed_rotation_aux_from_main".into(), mat3_to_json(rot));
    }
    if let Some(rt) = &s.full_rigid_transform_aux_from_main {
        m.insert(
            "full_rigid_transform_aux_from_main".into(),
            rigid_transform_to_json(rt),
        );
    }
    if !s.reference_scene_version.is_empty() {
        m.insert(
            "reference_scene_version".into(),
            Value::from(s.reference_scene_version.as_str()),
        );
    }
    if let Some(iu) = &s.installation_uncertainty {
        m.insert(
            "installation_uncertainty".into(),
            installation_uncertainty_to_json(iu),
        );
    }
    if let Some(ra) = &s.reference_anchor {
        m.insert("reference_anchor".into(), surveyed_reference_to_json(ra));
    }
    Value::Object(m)
}

fn json_str(obj: &Map<String, Value>, key: &str) -> String {
    obj.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn json_f64(obj: &Map<String, Value>, key: &str) -> f64 {
    obj.get(key).and_then(Value::as_f64).unwrap_or(0.0)
}

fn json_u32(obj: &Map<String, Value>, key: &str) -> u32 {
    obj.get(key)
        .and_then(Value::as_u64)
        .and_then(|v| u32::try_from(v).ok())
        .unwrap_or(0)
}

fn json_f64_vec(obj: &Map<String, Value>, key: &str) -> Result<Vec<f64>, StorageError> {
    let Some(val) = obj.get(key) else {
        return Ok(Vec::new());
    };
    let arr = val
        .as_array()
        .ok_or_else(|| StorageError::InvalidManifest(format!("expected array for field {key}")))?;
    arr.iter()
        .map(|item| {
            item.as_f64().ok_or_else(|| {
                StorageError::InvalidManifest(format!("expected number in array {key}"))
            })
        })
        .collect()
}

fn vec3_from_json(v: &Value) -> Result<Vec3, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected Vec3 object".into()))?;
    Ok(Vec3 {
        x: json_f64(obj, "x"),
        y: json_f64(obj, "y"),
        z: json_f64(obj, "z"),
    })
}

fn mat3_from_json(v: &Value) -> Result<Mat3, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected Mat3 object".into()))?;
    Ok(Mat3 {
        values: json_f64_vec(obj, "values")?,
    })
}

fn geodetic_anchor_from_json(v: &Value) -> Result<GeodeticAnchor, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected GeodeticAnchor object".into()))?;
    Ok(GeodeticAnchor {
        anchor_id: json_str(obj, "anchor_id"),
        datum: json_str(obj, "datum"),
        datum_epoch: json_str(obj, "datum_epoch"),
        latitude_deg: json_f64(obj, "latitude_deg"),
        longitude_deg: json_f64(obj, "longitude_deg"),
        height_m: json_f64(obj, "height_m"),
        height_reference: json_str(obj, "height_reference"),
    })
}

fn camera_model_from_json(v: &Value) -> Result<CameraModel, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected CameraModel object".into()))?;
    let intrinsics = match obj.get("intrinsics") {
        Some(intr_v) => {
            let i_obj = intr_v.as_object().ok_or_else(|| {
                StorageError::InvalidManifest("expected CameraIntrinsics object".into())
            })?;
            Some(CameraIntrinsics {
                image_width_px: json_u32(i_obj, "image_width_px"),
                image_height_px: json_u32(i_obj, "image_height_px"),
                matrix_px: i_obj.get("matrix_px").map(mat3_from_json).transpose()?,
            })
        }
        None => None,
    };
    let distortion = match obj.get("distortion") {
        Some(dist_v) => {
            let d_obj = dist_v.as_object().ok_or_else(|| {
                StorageError::InvalidManifest("expected CameraDistortion object".into())
            })?;
            Some(CameraDistortion {
                model: json_str(d_obj, "model"),
                coefficients: json_f64_vec(d_obj, "coefficients")?,
            })
        }
        None => None,
    };
    Ok(CameraModel {
        intrinsics,
        distortion,
    })
}

fn rigid_transform_from_json(v: &Value) -> Result<RigidTransform, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected RigidTransform object".into()))?;
    Ok(RigidTransform {
        rotation_a_from_b: obj
            .get("rotation_a_from_b")
            .map(mat3_from_json)
            .transpose()?,
        translation_a_m: obj.get("translation_a_m").map(vec3_from_json).transpose()?,
    })
}

fn installation_uncertainty_from_json(v: &Value) -> Result<InstallationUncertainty, StorageError> {
    let obj = v.as_object().ok_or_else(|| {
        StorageError::InvalidManifest("expected InstallationUncertainty object".into())
    })?;
    let state_ref = match obj.get("state_ref") {
        Some(sr_v) => {
            let sr = sr_v.as_object().ok_or_else(|| {
                StorageError::InvalidManifest("expected InstallationStateRef object".into())
            })?;
            Some(InstallationStateRef {
                station_id: json_str(sr, "station_id"),
                config_digest: json_str(sr, "config_digest"),
                state_version: json_str(sr, "state_version"),
            })
        }
        None => None,
    };
    Ok(InstallationUncertainty {
        state_ref,
        position_covariance_enu_m2: obj
            .get("position_covariance_enu_m2")
            .map(mat3_from_json)
            .transpose()?,
        orientation_covariance_enu_rad2: obj
            .get("orientation_covariance_enu_rad2")
            .map(mat3_from_json)
            .transpose()?,
        fixed_extrinsic_covariance_aux_rad2: obj
            .get("fixed_extrinsic_covariance_aux_rad2")
            .map(mat3_from_json)
            .transpose()?,
        measurement_source: json_str(obj, "measurement_source"),
    })
}

fn surveyed_landmark_from_json(v: &Value) -> Result<SurveyedLandmark, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected SurveyedLandmark object".into()))?;
    Ok(SurveyedLandmark {
        landmark_id: json_str(obj, "landmark_id"),
        position_enu_m: obj.get("position_enu_m").map(vec3_from_json).transpose()?,
        covariance_enu_m2: obj
            .get("covariance_enu_m2")
            .map(mat3_from_json)
            .transpose()?,
        visual_description: json_str(obj, "visual_description"),
    })
}

fn surveyed_reference_from_json(v: &Value) -> Result<SurveyedReference, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected SurveyedReference object".into()))?;
    let landmarks = match obj.get("landmarks") {
        Some(arr) => arr
            .as_array()
            .ok_or_else(|| StorageError::InvalidManifest("expected landmarks array".into()))?
            .iter()
            .map(surveyed_landmark_from_json)
            .collect::<Result<Vec<_>, _>>()?,
        None => Vec::new(),
    };
    Ok(SurveyedReference {
        reference_id: json_str(obj, "reference_id"),
        world_frame_id: json_str(obj, "world_frame_id"),
        landmarks,
    })
}

/// Parse a `StationConfig` JSON object produced by `MessageToDict(..., preserving_proto_field_name=True)`.
pub fn station_config_from_json(v: &Value) -> Result<StationConfig, StorageError> {
    let obj = v
        .as_object()
        .ok_or_else(|| StorageError::InvalidManifest("expected StationConfig object".into()))?;
    Ok(StationConfig {
        station_id: json_str(obj, "station_id"),
        config_version: json_str(obj, "config_version"),
        config_digest: json_str(obj, "config_digest"),
        world_frame_id: json_str(obj, "world_frame_id"),
        geodetic_anchor: obj
            .get("geodetic_anchor")
            .map(geodetic_anchor_from_json)
            .transpose()?,
        main_camera_position_enu_m: obj
            .get("main_camera_position_enu_m")
            .map(vec3_from_json)
            .transpose()?,
        baseline_orientation_enu_from_main: obj
            .get("baseline_orientation_enu_from_main")
            .map(mat3_from_json)
            .transpose()?,
        main_camera: obj
            .get("main_camera")
            .map(camera_model_from_json)
            .transpose()?,
        auxiliary_camera: obj
            .get("auxiliary_camera")
            .map(camera_model_from_json)
            .transpose()?,
        fixed_rotation_aux_from_main: obj
            .get("fixed_rotation_aux_from_main")
            .map(mat3_from_json)
            .transpose()?,
        full_rigid_transform_aux_from_main: obj
            .get("full_rigid_transform_aux_from_main")
            .map(rigid_transform_from_json)
            .transpose()?,
        reference_scene_version: json_str(obj, "reference_scene_version"),
        installation_uncertainty: obj
            .get("installation_uncertainty")
            .map(installation_uncertainty_from_json)
            .transpose()?,
        reference_anchor: obj
            .get("reference_anchor")
            .map(surveyed_reference_from_json)
            .transpose()?,
    })
}

// --- TestSceneReader (strictly read-only) ---

pub struct TestSceneReader {
    root: PathBuf,
    manifest: Value,
    stations: Vec<StationConfig>,
}

impl TestSceneReader {
    /// Open and validate an immutable Test Scene directory without mutating it.
    pub fn open(
        root: impl AsRef<Path>,
        verify_hashes: bool,
        verify_videos: bool,
    ) -> Result<Self, StorageError> {
        let root = root.as_ref().to_path_buf();
        if !root.is_dir() {
            return Err(StorageError::NotFound(root));
        }
        let manifest_path = root.join("manifest.json");
        let meta = fs::symlink_metadata(&manifest_path).map_err(|err| {
            if err.kind() == io::ErrorKind::NotFound {
                StorageError::NotFound(manifest_path.clone())
            } else {
                StorageError::Io(err)
            }
        })?;
        if meta.file_type().is_symlink() || !manifest_path.is_file() {
            return Err(StorageError::NotFound(manifest_path));
        }

        let raw = fs::read_to_string(&manifest_path)?;
        let manifest: Value = serde_json::from_str(&raw)?;
        let obj = manifest.as_object().ok_or_else(|| {
            StorageError::InvalidManifest("manifest root must be an object".into())
        })?;
        if obj.get("format").and_then(Value::as_str) != Some(TEST_SCENE_FORMAT) {
            return Err(StorageError::InvalidManifest(format!(
                "invalid Test Scene manifest format in {}",
                manifest_path.display()
            )));
        }
        let expected_digest = obj
            .get("content_sha256")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let actual_digest = canonical_manifest_sha256(&manifest)?;
        if expected_digest != actual_digest {
            return Err(StorageError::ContentSha256Mismatch {
                expected: expected_digest,
                actual: actual_digest,
            });
        }

        let files = obj.get("files").and_then(Value::as_object).ok_or_else(|| {
            StorageError::InvalidManifest("manifest missing files mapping".into())
        })?;
        for &required in REQUIRED_SCENE_STREAMS {
            if !files.contains_key(required) {
                return Err(StorageError::InvalidManifest(format!(
                    "Test Scene missing required stream {required}"
                )));
            }
        }
        for (relpath, entry) in files {
            let parts = validate_relpath(relpath)?;
            let is_video = parts.first().is_some_and(|p| p == "video");
            let check_digest = verify_hashes && (verify_videos || !is_video);
            verify_file_entry(&root, relpath, entry, is_video, check_digest)?;
        }

        let stations_val = obj
            .get("stations")
            .and_then(Value::as_array)
            .ok_or_else(|| {
                StorageError::InvalidManifest("manifest missing stations array".into())
            })?;
        let stations = stations_val
            .iter()
            .map(station_config_from_json)
            .collect::<Result<Vec<_>, _>>()?;

        Ok(Self {
            root,
            manifest,
            stations,
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn manifest(&self) -> &Value {
        &self.manifest
    }

    pub fn content_sha256(&self) -> &str {
        self.manifest
            .get("content_sha256")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    pub fn scenario_id(&self) -> &str {
        self.manifest
            .get("scenario_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    pub fn world_frame_id(&self) -> &str {
        self.manifest
            .get("world_frame_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    pub fn clock_domain_id(&self) -> &str {
        self.manifest
            .get("clock_domain_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    pub fn start_time_ns(&self) -> i64 {
        self.manifest
            .get("start_time_ns")
            .and_then(Value::as_i64)
            .unwrap_or(0)
    }

    pub fn end_time_ns(&self) -> i64 {
        self.manifest
            .get("end_time_ns")
            .and_then(Value::as_i64)
            .unwrap_or(0)
    }

    pub fn station_configs(&self) -> &[StationConfig] {
        &self.stations
    }

    pub fn video_path(&self, relpath: &str) -> Result<PathBuf, StorageError> {
        let parts = validate_relpath(relpath)?;
        if parts.first().map(String::as_str) != Some("video") {
            return Err(StorageError::InvalidRelpath(relpath.to_string()));
        }
        resolve_package_file(&self.root, relpath, true)
    }

    pub fn iter_frame_index(&self) -> Result<PbZstReader<FrameIndexEntry>, StorageError> {
        Ok(iter_pb_zst(self.root.join("frame_index.pb.zst"))?)
    }

    pub fn iter_truth_frames(&self) -> Result<PbZstReader<TruthFrame>, StorageError> {
        Ok(iter_pb_zst(self.root.join("truth.pb.zst"))?)
    }

    pub fn iter_virtual_rays(&self) -> Result<PbZstReader<RayEvent>, StorageError> {
        Ok(iter_pb_zst(self.root.join("virtual_rays.pb.zst"))?)
    }
}

// --- TestSceneWriter ---

#[derive(Debug, Clone)]
pub struct TestSceneConfig {
    pub scenario_id: String,
    pub world_frame_id: String,
    pub clock_domain_id: String,
    pub start_time_ns: i64,
    pub end_time_ns: i64,
    pub stations: Vec<StationConfig>,
    pub occlusion_mode: String,
    pub random_seed: Option<i64>,
    pub targets_meta: BTreeMap<String, Value>,
    pub encoding_preset: Option<EncodingPresetSpec>,
    pub flush_every: usize,
}

impl Default for TestSceneConfig {
    fn default() -> Self {
        Self {
            scenario_id: "recorded".to_string(),
            world_frame_id: "enu".to_string(),
            clock_domain_id: "sim".to_string(),
            start_time_ns: 0,
            end_time_ns: 0,
            stations: Vec::new(),
            occlusion_mode: "known".to_string(),
            random_seed: None,
            targets_meta: BTreeMap::new(),
            encoding_preset: None,
            flush_every: 256,
        }
    }
}

pub struct TestSceneWriter {
    root: PathBuf,
    config: TestSceneConfig,
    frame_index_writer: Option<PbZstWriter>,
    truth_writer: Option<PbZstWriter>,
    virtual_rays_writer: Option<PbZstWriter>,
    video_meta: BTreeMap<String, Value>,
    video_files: BTreeMap<String, FileDigestEntry>,
    finalized: bool,
    closed: bool,
}

impl TestSceneWriter {
    /// Create a fresh immutable Test Scene directory; refuses if `root` already exists.
    pub fn create(root: impl AsRef<Path>, config: TestSceneConfig) -> Result<Self, StorageError> {
        if config.occlusion_mode != "known" && config.occlusion_mode != "unknown" {
            return Err(StorageError::InvalidManifest(format!(
                "unsupported occlusion_mode: {:?}",
                config.occlusion_mode
            )));
        }
        let root = root.as_ref().to_path_buf();
        if root.exists() {
            return Err(StorageError::AlreadyExists(root));
        }
        if let Some(parent) = root.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        match fs::create_dir(&root) {
            Ok(()) => {}
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                return Err(StorageError::AlreadyExists(root));
            }
            Err(err) => return Err(StorageError::Io(err)),
        }
        fs::create_dir(root.join("video"))?;

        let frame_index_writer = PbZstWriter::create(
            root.join("frame_index.pb.zst"),
            DEFAULT_ZSTD_LEVEL,
            config.flush_every,
            false,
            MAX_FRAME_BYTES,
        )?;
        let truth_writer = PbZstWriter::create(
            root.join("truth.pb.zst"),
            DEFAULT_ZSTD_LEVEL,
            config.flush_every,
            false,
            MAX_FRAME_BYTES,
        )?;
        let virtual_rays_writer = PbZstWriter::create(
            root.join("virtual_rays.pb.zst"),
            DEFAULT_ZSTD_LEVEL,
            config.flush_every,
            false,
            MAX_FRAME_BYTES,
        )?;

        Ok(Self {
            root,
            config,
            frame_index_writer: Some(frame_index_writer),
            truth_writer: Some(truth_writer),
            virtual_rays_writer: Some(virtual_rays_writer),
            video_meta: BTreeMap::new(),
            video_files: BTreeMap::new(),
            finalized: false,
            closed: false,
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn set_time_bounds(&mut self, start_time_ns: i64, end_time_ns: i64) {
        self.config.start_time_ns = start_time_ns;
        self.config.end_time_ns = end_time_ns;
    }

    pub fn set_stations(&mut self, stations: Vec<StationConfig>) {
        self.config.stations = stations;
    }

    pub fn add_station(&mut self, station: StationConfig) {
        if let Some(existing) = self
            .config
            .stations
            .iter_mut()
            .find(|s| s.station_id == station.station_id)
        {
            *existing = station;
        } else {
            self.config.stations.push(station);
        }
    }

    fn ensure_open(&self) -> Result<(), StorageError> {
        if self.finalized || self.closed {
            return Err(StorageError::WriterClosed);
        }
        Ok(())
    }

    pub fn add_frame_index(&mut self, entry: &FrameIndexEntry) -> Result<(), StorageError> {
        self.ensure_open()?;
        self.frame_index_writer
            .as_mut()
            .ok_or(StorageError::WriterClosed)?
            .write(entry)?;
        Ok(())
    }

    pub fn add_truth_frame(&mut self, frame: &TruthFrame) -> Result<(), StorageError> {
        self.ensure_open()?;
        self.truth_writer
            .as_mut()
            .ok_or(StorageError::WriterClosed)?
            .write(frame)?;
        Ok(())
    }

    pub fn record_targets_from_truth(&mut self, frame: &TruthFrame) {
        for tgt in &frame.targets {
            if self.config.targets_meta.contains_key(&tgt.target_id) {
                continue;
            }
            let mut meta = Map::new();
            meta.insert(
                "target_class".into(),
                Value::from(tgt.target_class.as_str()),
            );
            meta.insert(
                "bounding_radius_m".into(),
                Value::from(tgt.bounding_radius_m),
            );
            if let Some(diag) = tgt.body_aabb_diagonal_m {
                meta.insert("body_aabb_diagonal_m".into(), Value::from(diag));
            }
            self.config
                .targets_meta
                .insert(tgt.target_id.clone(), Value::Object(meta));
        }
    }

    pub fn add_virtual_ray(&mut self, ray: &RayEvent) -> Result<(), StorageError> {
        self.ensure_open()?;
        self.virtual_rays_writer
            .as_mut()
            .ok_or(StorageError::WriterClosed)?
            .write(ray)?;
        Ok(())
    }

    /// Copy an external video file into `video/<...>` and record its metadata & SHA-256.
    pub fn add_video_file(
        &mut self,
        source: impl AsRef<Path>,
        relpath: &str,
        metadata: Value,
    ) -> Result<(), StorageError> {
        self.ensure_open()?;
        let parts = validate_relpath(relpath)?;
        if parts.first().map(String::as_str) != Some("video") {
            return Err(StorageError::InvalidRelpath(relpath.to_string()));
        }
        let source = source.as_ref();
        if !source.is_file() {
            return Err(StorageError::NotFound(source.to_path_buf()));
        }
        let mut dest = self.root.clone();
        for part in &parts {
            dest.push(part);
        }
        if dest.exists() {
            return Err(StorageError::AlreadyExists(dest));
        }
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(source, &dest)?;
        self.register_existing_video_file(relpath, metadata)
    }

    /// Register a video file already created inside `<scene-root>/video/...`.
    pub fn register_existing_video_file(
        &mut self,
        relpath: &str,
        metadata: Value,
    ) -> Result<(), StorageError> {
        self.ensure_open()?;
        let parts = validate_relpath(relpath)?;
        if parts.first().map(String::as_str) != Some("video") {
            return Err(StorageError::InvalidRelpath(relpath.to_string()));
        }
        let dest = resolve_package_file(&self.root, relpath, false)?;
        let sha256 = sha256_file(&dest)?;
        let bytes = fs::metadata(&dest)?.len();
        self.video_meta.insert(relpath.to_string(), metadata);
        self.video_files
            .insert(relpath.to_string(), FileDigestEntry { sha256, bytes });
        Ok(())
    }

    pub fn close(&mut self) -> Result<(), StorageError> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        if let Some(w) = self.frame_index_writer.take() {
            w.close()?;
        }
        if let Some(w) = self.truth_writer.take() {
            w.close()?;
        }
        if let Some(w) = self.virtual_rays_writer.take() {
            w.close()?;
        }
        Ok(())
    }

    /// Finalize streams, compute digests, and atomically write `manifest.json`.
    pub fn finalize(mut self) -> Result<Value, StorageError> {
        if self.finalized {
            return Err(StorageError::WriterClosed);
        }
        self.close()?;

        let mut files = Map::new();
        for &relpath in REQUIRED_SCENE_STREAMS {
            let path = self.root.join(relpath);
            let sha256 = sha256_file(&path)?;
            let bytes = fs::metadata(&path)?.len();
            files.insert(
                relpath.to_string(),
                serde_json::to_value(FileDigestEntry { sha256, bytes })?,
            );
        }
        for (relpath, entry) in &self.video_files {
            files.insert(relpath.clone(), serde_json::to_value(entry)?);
        }

        let mut manifest_map = Map::new();
        manifest_map.insert("format".into(), Value::from(TEST_SCENE_FORMAT));
        manifest_map.insert(
            "scenario_id".into(),
            Value::from(self.config.scenario_id.as_str()),
        );
        manifest_map.insert(
            "world_frame_id".into(),
            Value::from(self.config.world_frame_id.as_str()),
        );
        manifest_map.insert(
            "clock_domain_id".into(),
            Value::from(self.config.clock_domain_id.as_str()),
        );
        manifest_map.insert(
            "start_time_ns".into(),
            Value::from(self.config.start_time_ns),
        );
        manifest_map.insert("end_time_ns".into(), Value::from(self.config.end_time_ns));
        manifest_map.insert(
            "random_seed".into(),
            match self.config.random_seed {
                Some(seed) => Value::from(seed),
                None => Value::Null,
            },
        );
        manifest_map.insert(
            "occlusion_mode".into(),
            Value::from(self.config.occlusion_mode.as_str()),
        );
        manifest_map.insert(
            "stations".into(),
            Value::Array(
                self.config
                    .stations
                    .iter()
                    .map(station_config_to_json)
                    .collect(),
            ),
        );
        manifest_map.insert(
            "targets".into(),
            Value::Object(self.config.targets_meta.clone().into_iter().collect()),
        );
        if let Some(preset) = &self.config.encoding_preset {
            manifest_map.insert("encoding_preset".into(), serde_json::to_value(preset)?);
        }
        manifest_map.insert(
            "video".into(),
            Value::Object(self.video_meta.clone().into_iter().collect()),
        );
        manifest_map.insert("files".into(), Value::Object(files));

        let mut manifest = Value::Object(manifest_map);
        let content_sha256 = canonical_manifest_sha256(&manifest)?;
        if let Some(obj) = manifest.as_object_mut() {
            obj.insert("content_sha256".into(), Value::from(content_sha256));
        }

        write_json_atomic(self.root.join("manifest.json"), &manifest, true)?;
        self.finalized = true;
        Ok(manifest)
    }
}

// --- RunOutputWriter ---

#[derive(Debug, Clone)]
pub struct RunOutputConfig {
    pub run_id: String,
    pub mode: String,
    pub test_scene_path: PathBuf,
    pub test_scene_sha256: String,
    pub provenance: BTreeMap<String, Value>,
    pub flush_every: usize,
}

pub struct RunOutputWriter {
    root: PathBuf,
    config: RunOutputConfig,
    edge_writer: Option<PbZstWriter>,
    center_writer: Option<PbZstWriter>,
    voxel_writer: Option<PbZstWriter>,
    written_relpaths: Vec<&'static str>,
    finalized: bool,
    closed: bool,
}

impl RunOutputWriter {
    /// Create a fresh Run Output directory; refuses if `root` already exists.
    pub fn create(root: impl AsRef<Path>, config: RunOutputConfig) -> Result<Self, StorageError> {
        if !VALID_RUN_MODES.contains(&config.mode.as_str()) {
            return Err(StorageError::InvalidManifest(format!(
                "unsupported run mode: {:?}",
                config.mode
            )));
        }
        if config.run_id.is_empty() {
            return Err(StorageError::InvalidManifest(
                "run_id must be non-empty".into(),
            ));
        }
        if config.test_scene_sha256.is_empty() {
            return Err(StorageError::InvalidManifest(
                "test_scene_sha256 must be non-empty".into(),
            ));
        }
        let root = root.as_ref().to_path_buf();
        if root.exists() {
            return Err(StorageError::AlreadyExists(root));
        }
        if let Some(parent) = root.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        match fs::create_dir(&root) {
            Ok(()) => {}
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                return Err(StorageError::AlreadyExists(root));
            }
            Err(err) => return Err(StorageError::Io(err)),
        }

        Ok(Self {
            root,
            config,
            edge_writer: None,
            center_writer: None,
            voxel_writer: None,
            written_relpaths: Vec::new(),
            finalized: false,
            closed: false,
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn ensure_open(&self) -> Result<(), StorageError> {
        if self.finalized || self.closed {
            return Err(StorageError::WriterClosed);
        }
        Ok(())
    }

    pub fn write_edge_message(&mut self, message: &BusMessage) -> Result<(), StorageError> {
        self.ensure_open()?;
        if self.edge_writer.is_none() {
            let writer = PbZstWriter::create(
                self.root.join("edge_events.pb.zst"),
                DEFAULT_ZSTD_LEVEL,
                self.config.flush_every,
                false,
                MAX_FRAME_BYTES,
            )?;
            self.edge_writer = Some(writer);
            self.written_relpaths.push("edge_events.pb.zst");
        }
        self.edge_writer
            .as_mut()
            .expect("edge_writer initialized")
            .write(message)?;
        Ok(())
    }

    pub fn write_center_message(&mut self, message: &BusMessage) -> Result<(), StorageError> {
        self.ensure_open()?;
        if self.center_writer.is_none() {
            let writer = PbZstWriter::create(
                self.root.join("center_output.pb.zst"),
                DEFAULT_ZSTD_LEVEL,
                self.config.flush_every,
                false,
                MAX_FRAME_BYTES,
            )?;
            self.center_writer = Some(writer);
            self.written_relpaths.push("center_output.pb.zst");
        }
        self.center_writer
            .as_mut()
            .expect("center_writer initialized")
            .write(message)?;
        Ok(())
    }

    pub fn write_voxel_grid(&mut self, grid: &SparseVoxelGrid) -> Result<(), StorageError> {
        self.ensure_open()?;
        if self.voxel_writer.is_none() {
            let writer = PbZstWriter::create(
                self.root.join("voxels.pb.zst"),
                DEFAULT_ZSTD_LEVEL,
                self.config.flush_every,
                false,
                MAX_FRAME_BYTES,
            )?;
            self.voxel_writer = Some(writer);
            self.written_relpaths.push("voxels.pb.zst");
        }
        self.voxel_writer
            .as_mut()
            .expect("voxel_writer initialized")
            .write(grid)?;
        Ok(())
    }

    pub fn close(&mut self) -> Result<(), StorageError> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        if let Some(w) = self.edge_writer.take() {
            w.close()?;
        }
        if let Some(w) = self.center_writer.take() {
            w.close()?;
        }
        if let Some(w) = self.voxel_writer.take() {
            w.close()?;
        }
        Ok(())
    }

    pub fn finalize(mut self) -> Result<Value, StorageError> {
        if self.finalized {
            return Err(StorageError::WriterClosed);
        }
        self.close()?;

        let mut files = Map::new();
        for &relpath in &self.written_relpaths {
            let path = self.root.join(relpath);
            let sha256 = sha256_file(&path)?;
            let bytes = fs::metadata(&path)?.len();
            files.insert(
                relpath.to_string(),
                serde_json::to_value(FileDigestEntry { sha256, bytes })?,
            );
        }

        let mut manifest_map = Map::new();
        manifest_map.insert("format".into(), Value::from(RUN_OUTPUT_FORMAT));
        manifest_map.insert("run_id".into(), Value::from(self.config.run_id.as_str()));
        manifest_map.insert("mode".into(), Value::from(self.config.mode.as_str()));
        manifest_map.insert(
            "test_scene_path".into(),
            Value::from(self.config.test_scene_path.to_string_lossy().as_ref()),
        );
        manifest_map.insert(
            "test_scene_sha256".into(),
            Value::from(self.config.test_scene_sha256.as_str()),
        );
        manifest_map.insert(
            "provenance".into(),
            Value::Object(self.config.provenance.clone().into_iter().collect()),
        );
        manifest_map.insert("files".into(), Value::Object(files));

        let manifest = Value::Object(manifest_map);
        write_json_atomic(self.root.join("run_manifest.json"), &manifest, true)?;
        self.finalized = true;
        Ok(manifest)
    }
}

/// Ensure a relative path only has normal components (used for validating CLI/video paths).
pub fn is_safe_relative_subpath(path: &Path) -> bool {
    !path.is_absolute() && path.components().all(|c| matches!(c, Component::Normal(_)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::{
        bus_message, CameraRole, CaptureTime, ComponentRole, EventEnvelope, EventType,
        ProgressReport, SparseVoxelCell,
    };
    use tempfile::TempDir;

    fn sample_station() -> StationConfig {
        StationConfig {
            station_id: "s1".into(),
            config_version: "v1".into(),
            config_digest: "cfg-1".into(),
            world_frame_id: "enu".into(),
            main_camera_position_enu_m: Some(Vec3 {
                x: 0.0,
                y: 0.0,
                z: 10.0,
            }),
            baseline_orientation_enu_from_main: Some(Mat3 {
                values: vec![1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            }),
            installation_uncertainty: Some(InstallationUncertainty {
                state_ref: Some(InstallationStateRef {
                    station_id: "s1".into(),
                    config_digest: "cfg-1".into(),
                    state_version: "1".into(),
                }),
                position_covariance_enu_m2: Some(Mat3 {
                    values: vec![1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
                }),
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn test_scene_write_verify_and_tamper_detection() {
        let dir = TempDir::new().unwrap();
        let raw_video = dir.path().join("cam.mp4");
        fs::write(&raw_video, b"mp4-fixture-bytes").unwrap();
        let scene_dir = dir.path().join("scene");

        {
            let mut writer = TestSceneWriter::create(
                &scene_dir,
                TestSceneConfig {
                    scenario_id: "sc-1".into(),
                    world_frame_id: "enu".into(),
                    clock_domain_id: "clk".into(),
                    start_time_ns: 0,
                    end_time_ns: 1_000_000,
                    stations: vec![sample_station()],
                    random_seed: Some(42),
                    ..Default::default()
                },
            )
            .unwrap();
            writer
                .add_video_file(
                    &raw_video,
                    "video/s1-main.mp4",
                    serde_json::json!({"fps": 30}),
                )
                .unwrap();
            writer
                .add_frame_index(&FrameIndexEntry {
                    station_id: "s1".into(),
                    station_session_id: "sess-1".into(),
                    camera_role: CameraRole::Main as i32,
                    frame_index: 0,
                    frame_id: "f-0".into(),
                    capture_time: Some(CaptureTime {
                        clock_domain_id: "clk".into(),
                        exposure_start_ns: 0,
                        exposure_end_ns: 0,
                        ..Default::default()
                    }),
                    station_config_digest: "cfg-1".into(),
                    video_relpath: "video/s1-main.mp4".into(),
                    width_px: 64,
                    height_px: 64,
                })
                .unwrap();
            writer
                .add_truth_frame(&TruthFrame {
                    world_frame_id: "enu".into(),
                    experiment_time_ns: 0,
                    step_index: 0,
                    ..Default::default()
                })
                .unwrap();
            writer
                .add_virtual_ray(&RayEvent {
                    envelope: Some(EventEnvelope {
                        event_type: EventType::Ray as i32,
                        station_id: "s1".into(),
                        station_session_id: "sess-1".into(),
                        sequence_no: 1,
                        ..Default::default()
                    }),
                    frame_id: "f-0".into(),
                    ..Default::default()
                })
                .unwrap();
            writer.finalize().unwrap();
        }

        // Refuses existing directory
        assert!(matches!(
            TestSceneWriter::create(&scene_dir, TestSceneConfig::default()),
            Err(StorageError::AlreadyExists(_))
        ));

        let reader = TestSceneReader::open(&scene_dir, true, true).unwrap();
        assert_eq!(reader.station_configs().len(), 1);
        assert_eq!(reader.station_configs()[0].station_id, "s1");
        assert_eq!(
            reader
                .iter_frame_index()
                .unwrap()
                .collect_all()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            reader
                .iter_truth_frames()
                .unwrap()
                .collect_all()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            reader
                .iter_virtual_rays()
                .unwrap()
                .collect_all()
                .unwrap()
                .len(),
            1
        );

        // Tamper detection on truth stream
        let truth_path = scene_dir.join("truth.pb.zst");
        let orig = fs::read(&truth_path).unwrap();
        let mut tampered = orig.clone();
        tampered.extend_from_slice(b"tamper");
        fs::write(&truth_path, tampered).unwrap();
        assert!(matches!(
            TestSceneReader::open(&scene_dir, true, false),
            Err(StorageError::ByteCountMismatch { .. } | StorageError::FileSha256Mismatch { .. })
        ));
        fs::write(&truth_path, orig).unwrap();

        // Manifest content_sha256 tamper detection
        let manifest_path = scene_dir.join("manifest.json");
        let mut manifest: Value =
            serde_json::from_str(&fs::read_to_string(&manifest_path).unwrap()).unwrap();
        manifest["files"]["../escape.pb.zst"] =
            serde_json::json!({"sha256": "0".repeat(64), "bytes": 0});
        fs::write(&manifest_path, serde_json::to_string(&manifest).unwrap()).unwrap();
        assert!(matches!(
            TestSceneReader::open(&scene_dir, false, false),
            Err(StorageError::ContentSha256Mismatch { .. })
        ));
    }

    #[test]
    fn run_output_write_and_refuse_existing() {
        let dir = TempDir::new().unwrap();
        let run_dir = dir.path().join("run-1");
        let mut rw = RunOutputWriter::create(
            &run_dir,
            RunOutputConfig {
                run_id: "r-1".into(),
                mode: "center".into(),
                test_scene_path: dir.path().join("scene"),
                test_scene_sha256: "a".repeat(64),
                provenance: BTreeMap::new(),
                flush_every: 16,
            },
        )
        .unwrap();
        rw.write_center_message(&BusMessage {
            bus_sequence_no: 1,
            sent_wall_time_ns: 123,
            payload: Some(bus_message::Payload::ProgressReport(ProgressReport {
                source_role: ComponentRole::Center as i32,
                source_id: "center".into(),
                session_id: "c1".into(),
                completed_time_ns: 0,
                completed_sequence_no: 1,
                finite_end_reached: true,
            })),
        })
        .unwrap();
        rw.write_voxel_grid(&SparseVoxelGrid {
            center_session_id: "c1".into(),
            world_frame_id: "enu".into(),
            window_start_ns: 0,
            window_end_ns: 1_000,
            cells: vec![SparseVoxelCell {
                ix: 1,
                iy: 2,
                iz: 3,
                station_votes: 2,
            }],
            ..Default::default()
        })
        .unwrap();
        let manifest = rw.finalize().unwrap();
        assert_eq!(manifest["format"], RUN_OUTPUT_FORMAT);
        assert!(manifest["files"].get("center_output.pb.zst").is_some());
        assert!(manifest["files"].get("voxels.pb.zst").is_some());
        assert!(manifest["files"].get("edge_events.pb.zst").is_none());
    }

    #[test]
    fn station_config_reference_anchor_json_roundtrip() {
        let mut st = sample_station();
        st.reference_anchor = Some(SurveyedReference {
            reference_id: "board-1".into(),
            world_frame_id: "enu".into(),
            landmarks: vec![SurveyedLandmark {
                landmark_id: "board-corner-0".into(),
                position_enu_m: Some(Vec3 {
                    x: 1.0,
                    y: 2.0,
                    z: 0.0,
                }),
                covariance_enu_m2: Some(Mat3 {
                    values: vec![1e-6, 0.0, 0.0, 0.0, 1e-6, 0.0, 0.0, 0.0, 1e-6],
                }),
                visual_description: "".into(),
            }],
        });
        let json = station_config_to_json(&st);
        let ra_obj = json.get("reference_anchor").unwrap().as_object().unwrap();
        let lm_obj = ra_obj["landmarks"][0].as_object().unwrap();
        assert!(!lm_obj.contains_key("visual_description"));
        assert!(!lm_obj["position_enu_m"]
            .as_object()
            .unwrap()
            .contains_key("z"));
        let decoded = station_config_from_json(&json).unwrap();
        assert_eq!(decoded, st);
    }
}
