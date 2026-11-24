//! Strict `DevicePolicy` JSON loader and validator matching `contracts/device.py` and `composition/station.py`.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;
use thiserror::Error;

use crate::math::covariance_factor_2x2;
use crate::proto::{ScoreDefinition as ProtoScoreDefinition, ScoreKind as ProtoScoreKind};

pub const MOG2_ALGORITHM_ID: &str = "mog2-components-v1";
pub const TRACKING_ALGORITHM_ID: &str = "constant-velocity-nearest-neighbor-v1";
pub const WORKBENCH_DETECTION_ID: &str = "frame-difference-occupancy-v1";
pub const WORKBENCH_MOG2_ID: &str = "mog2-workbench-v1";
pub const WORKBENCH_TRACKING_ID: &str = "workbench-kalman-v1";
pub const OPENCV_CALIBRATION_ESTIMATOR_ID: &str = "opencv-checkerboard-v1";
pub const RAY_UNCERTAINTY_MODEL_ID: &str = "tangent_gaussian_v1";

#[derive(Debug, Error)]
pub enum PolicyError {
    #[error("failed to read policy file: {0}")]
    Io(#[from] std::io::Error),
    #[error("failed to parse policy JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid policy: {0}")]
    Invalid(String),
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScoreDefinitionPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub definition_id: String,
    pub kind: String,
    pub minimum: Option<f64>,
    pub maximum: Option<f64>,
    pub cross_station_comparison_group: Option<String>,
}

impl PartialEq for ScoreDefinitionPolicy {
    fn eq(&self, other: &Self) -> bool {
        self.definition_id == other.definition_id
            && self.kind == other.kind
            && self.minimum == other.minimum
            && self.maximum == other.maximum
            && self.cross_station_comparison_group == other.cross_station_comparison_group
    }
}

impl ScoreDefinitionPolicy {
    pub fn to_proto(&self) -> Result<ProtoScoreDefinition, PolicyError> {
        let kind = match self.kind.as_str() {
            "ranking" => ProtoScoreKind::Ranking as i32,
            "calibrated_probability" => ProtoScoreKind::CalibratedProbability as i32,
            other => {
                return Err(PolicyError::Invalid(format!(
                    "unsupported score kind: {other:?}"
                )));
            }
        };
        Ok(ProtoScoreDefinition {
            definition_id: self.definition_id.clone(),
            kind,
            minimum: self.minimum,
            maximum: self.maximum,
            cross_station_comparison_group: self.cross_station_comparison_group.clone(),
        })
    }
}

pub fn fill_ratio_score() -> ScoreDefinitionPolicy {
    ScoreDefinitionPolicy {
        type_tag: None,
        definition_id: "foreground-component-fill-ratio-v1".to_string(),
        kind: "ranking".to_string(),
        minimum: Some(0.0),
        maximum: Some(1.0),
        cross_station_comparison_group: None,
    }
}

pub fn track_quality_score() -> ScoreDefinitionPolicy {
    ScoreDefinitionPolicy {
        type_tag: None,
        definition_id: "consecutive-detection-fraction-v1".to_string(),
        kind: "ranking".to_string(),
        minimum: Some(0.0),
        maximum: Some(1.0),
        cross_station_comparison_group: None,
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mog2DetectionPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub algorithm_id: String,
    pub score_definition: ScoreDefinitionPolicy,
    pub minimum_component_area_px2: f64,
    pub maximum_component_area_px2: Option<f64>,
    #[serde(default)]
    pub background_warmup_frame_count: Option<i64>,
    #[serde(default)]
    pub background_history_frame_count: Option<i64>,
    #[serde(default)]
    pub background_variance_threshold: Option<f64>,
    #[serde(default)]
    pub background_learning_rate: Option<f64>,
    #[serde(default)]
    pub background_detect_shadows: Option<bool>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FrameDifferencePolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub algorithm_id: String,
    pub downsample_factor: i64,
    pub mode: String,
    pub threshold: f64,
    pub morphology_kernel_size: i64,
    pub apply_opening: bool,
    pub apply_closing: bool,
    pub score_increment: f64,
    pub score_decay: f64,
    pub mask_threshold: f64,
    pub occupancy_dilation_size: i64,
    pub minimum_component_area_px2: f64,
    pub maximum_component_area_px2: Option<f64>,
    pub blob_dilation_size: i64,
    #[serde(default = "default_true")]
    pub apply_occupancy: bool,
    #[serde(default)]
    pub mog2_history: Option<i64>,
    #[serde(default)]
    pub mog2_variance_threshold: Option<f64>,
    #[serde(default)]
    pub mog2_learning_rate: Option<f64>,
    #[serde(default)]
    pub mog2_detect_shadows: Option<bool>,
    #[serde(default)]
    pub apply_erosion: bool,
    #[serde(default)]
    pub vibe_library_path: Option<String>,
    #[serde(default)]
    pub vibe_library_sha256: Option<String>,
    #[serde(default = "default_vibe_seed")]
    pub vibe_seed: u64,
}

fn default_true() -> bool {
    true
}

fn default_vibe_seed() -> u64 {
    42
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum DetectionPolicyConfig {
    Standard(Mog2DetectionPolicy),
    Workbench(FrameDifferencePolicy),
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tracking2DPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub algorithm_id: String,
    pub association_gate_px: f64,
    pub confirmation_frame_count: i64,
    pub maximum_missing_duration_ns: i64,
    pub quality_definition: ScoreDefinitionPolicy,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KalmanTrackingPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub algorithm_id: String,
    pub confirmation_frame_count: i64,
    pub maximum_lost_frame_count: i64,
    pub association_gate_px: f64,
    pub covariance_floor_px: f64,
    pub process_noise_position: f64,
    pub process_noise_velocity: f64,
    pub measurement_noise_px: f64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum TrackingPolicyConfig {
    Standard(Tracking2DPolicy),
    Workbench(KalmanTrackingPolicy),
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalibrationPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub update_period_ns: i64,
    pub stale_after_ns: i64,
    pub recovery_success_count: i64,
    pub minimum_reference_count: i64,
    pub maximum_reprojection_rmse_px: f64,
    pub maximum_rotation_only_parallax_px: Option<f64>,
    pub estimator_id: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RayGenerationPolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub uncertainty_model_id: String,
    pub fallback_centroid_covariance_px2: Option<[[f64; 2]; 2]>,
    pub unmodelled_high_frequency_jitter_covariance_rad2: Option<[[f64; 2]; 2]>,
    pub unmodelled_relative_flex_covariance_rad2: Option<[[f64; 2]; 2]>,
    #[serde(default)]
    pub residual_independence_basis: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DevicePolicy {
    #[serde(rename = "$type", default)]
    pub type_tag: Option<String>,
    pub detection: DetectionPolicyConfig,
    pub tracking: TrackingPolicyConfig,
    pub calibration: CalibrationPolicy,
    pub rays: RayGenerationPolicy,
}

fn is_pos(v: f64) -> bool {
    v.is_finite() && v > 0.0
}

fn check_type_tag(tag: Option<&str>, expected: &str) -> Result<(), PolicyError> {
    if let Some(actual) = tag {
        if actual != expected {
            return Err(PolicyError::Invalid(format!(
                "expected $type {expected:?}, got {actual:?}"
            )));
        }
    }
    Ok(())
}

pub fn validate_device_policy(policy: &DevicePolicy) -> Result<(), PolicyError> {
    check_type_tag(policy.type_tag.as_deref(), "DevicePolicy")?;
    let is_workbench_det = matches!(policy.detection, DetectionPolicyConfig::Workbench(_));
    let is_workbench_trk = matches!(policy.tracking, TrackingPolicyConfig::Workbench(_));
    if is_workbench_det != is_workbench_trk {
        return Err(PolicyError::Invalid(
            "detector and tracker algorithm families must agree".to_string(),
        ));
    }

    match &policy.detection {
        DetectionPolicyConfig::Standard(det) => {
            check_type_tag(det.type_tag.as_deref(), "DetectionPolicy")?;
            check_type_tag(det.score_definition.type_tag.as_deref(), "ScoreDefinition")?;
            if det.algorithm_id != MOG2_ALGORITHM_ID || det.score_definition != fill_ratio_score() {
                return Err(PolicyError::Invalid(
                    "MOG2 requires its algorithm and fill-ratio score definitions".to_string(),
                ));
            }
            if !is_pos(det.minimum_component_area_px2) {
                return Err(PolicyError::Invalid(
                    "minimum_component_area_px2 must be finite and positive".to_string(),
                ));
            }
            if let Some(max_area) = det.maximum_component_area_px2 {
                if !is_pos(max_area) || max_area < det.minimum_component_area_px2 {
                    return Err(PolicyError::Invalid(
                        "invalid maximum_component_area_px2".to_string(),
                    ));
                }
            }
            let (Some(warmup), Some(history), Some(threshold), Some(rate), Some(_shadows)) = (
                det.background_warmup_frame_count,
                det.background_history_frame_count,
                det.background_variance_threshold,
                det.background_learning_rate,
                det.background_detect_shadows,
            ) else {
                return Err(PolicyError::Invalid(
                    "MOG2 background parameters must all be explicitly configured".to_string(),
                ));
            };
            if warmup < 0
                || history <= 0
                || history > 2_147_483_647
                || !is_pos(threshold)
                || !rate.is_finite()
                || !(0.0..=1.0).contains(&rate)
            {
                return Err(PolicyError::Invalid(
                    "invalid MOG2 background parameter values".to_string(),
                ));
            }
        }
        DetectionPolicyConfig::Workbench(det) => {
            check_type_tag(det.type_tag.as_deref(), "FrameDifferencePolicy")?;
            let expected_id = match det.mode.as_str() {
                "two_frame" | "three_frame" | "five_frame" => WORKBENCH_DETECTION_ID,
                "mog2" => WORKBENCH_MOG2_ID,
                other => {
                    return Err(PolicyError::Invalid(format!(
                        "unsupported workbench detection mode: {other:?}"
                    )));
                }
            };
            if det.algorithm_id != expected_id {
                return Err(PolicyError::Invalid(format!(
                    "workbench detection algorithm_id {:?} does not match mode {:?}",
                    det.algorithm_id, det.mode
                )));
            }
            if !matches!(det.downsample_factor, 1 | 2 | 4) {
                return Err(PolicyError::Invalid(
                    "downsample_factor must be 1, 2, or 4".to_string(),
                ));
            }
            if det.morphology_kernel_size < 1
                || det.morphology_kernel_size % 2 == 0
                || det.occupancy_dilation_size < 0
                || det.blob_dilation_size < 0
            {
                return Err(PolicyError::Invalid(
                    "invalid workbench kernel size".to_string(),
                ));
            }
            if !is_pos(det.threshold)
                || !is_pos(det.score_increment)
                || !is_pos(det.score_decay)
                || !is_pos(det.mask_threshold)
                || !is_pos(det.minimum_component_area_px2)
            {
                return Err(PolicyError::Invalid(
                    "workbench thresholds and rates must be finite and positive".to_string(),
                ));
            }
            if let Some(max_area) = det.maximum_component_area_px2 {
                if !is_pos(max_area) || max_area < det.minimum_component_area_px2 {
                    return Err(PolicyError::Invalid(
                        "invalid maximum component area".to_string(),
                    ));
                }
            }
            if det.mode == "mog2" {
                let (Some(history), Some(var_thresh), Some(lr), Some(_shadows)) = (
                    det.mog2_history,
                    det.mog2_variance_threshold,
                    det.mog2_learning_rate,
                    det.mog2_detect_shadows,
                ) else {
                    return Err(PolicyError::Invalid(
                        "workbench MOG2 parameters must be explicit".to_string(),
                    ));
                };
                if history <= 0
                    || !is_pos(var_thresh)
                    || !lr.is_finite()
                    || (lr < 0.0 && (lr + 1.0).abs() > 1e-12)
                    || lr > 1.0
                {
                    return Err(PolicyError::Invalid(
                        "invalid workbench MOG2 parameters".to_string(),
                    ));
                }
            }
        }
    }

    match &policy.tracking {
        TrackingPolicyConfig::Standard(trk) => {
            check_type_tag(trk.type_tag.as_deref(), "Tracking2DPolicy")?;
            check_type_tag(
                trk.quality_definition.type_tag.as_deref(),
                "ScoreDefinition",
            )?;
            if trk.algorithm_id != TRACKING_ALGORITHM_ID
                || trk.quality_definition != track_quality_score()
                || !is_pos(trk.association_gate_px)
                || trk.confirmation_frame_count <= 0
                || trk.maximum_missing_duration_ns <= 0
            {
                return Err(PolicyError::Invalid(
                    "invalid local tracking policy or quality definition".to_string(),
                ));
            }
        }
        TrackingPolicyConfig::Workbench(trk) => {
            check_type_tag(trk.type_tag.as_deref(), "KalmanTrackingPolicy")?;
            if trk.algorithm_id != WORKBENCH_TRACKING_ID
                || trk.confirmation_frame_count < 1
                || trk.maximum_lost_frame_count < 0
                || !is_pos(trk.association_gate_px)
                || !is_pos(trk.covariance_floor_px)
                || !is_pos(trk.process_noise_position)
                || !is_pos(trk.process_noise_velocity)
                || !is_pos(trk.measurement_noise_px)
            {
                return Err(PolicyError::Invalid(
                    "invalid workbench Kalman tracking policy".to_string(),
                ));
            }
        }
    }

    let cal = &policy.calibration;
    check_type_tag(cal.type_tag.as_deref(), "CalibrationPolicy")?;
    if cal.estimator_id != OPENCV_CALIBRATION_ESTIMATOR_ID
        || cal.update_period_ns < 0
        || cal.stale_after_ns < 0
        || cal.recovery_success_count <= 0
        || cal.minimum_reference_count < 4
        || !is_pos(cal.maximum_reprojection_rmse_px)
    {
        return Err(PolicyError::Invalid(
            "invalid explicitly configured device calibration policy".to_string(),
        ));
    }
    if let Some(parallax) = cal.maximum_rotation_only_parallax_px {
        if !parallax.is_finite() || parallax < 0.0 {
            return Err(PolicyError::Invalid(
                "invalid maximum_rotation_only_parallax_px".to_string(),
            ));
        }
    }

    let rays = &policy.rays;
    check_type_tag(rays.type_tag.as_deref(), "RayGenerationPolicy")?;
    if rays.uncertainty_model_id != RAY_UNCERTAINTY_MODEL_ID {
        return Err(PolicyError::Invalid(
            "unsupported device ray uncertainty model".to_string(),
        ));
    }
    let mut has_residual = false;
    if let Some(cov) = rays.fallback_centroid_covariance_px2 {
        covariance_factor_2x2(cov, "fallback_centroid_covariance_px2")
            .map_err(|e| PolicyError::Invalid(e.to_string()))?;
    }
    for (cov_opt, label) in [
        (
            rays.unmodelled_high_frequency_jitter_covariance_rad2,
            "residual_jitter_rad2",
        ),
        (
            rays.unmodelled_relative_flex_covariance_rad2,
            "residual_flex_rad2",
        ),
    ] {
        if let Some(cov) = cov_opt {
            let factor = covariance_factor_2x2(cov, label)
                .map_err(|e| PolicyError::Invalid(e.to_string()))?;
            if factor.iter().flatten().any(|&x| x != 0.0) {
                has_residual = true;
            }
        }
    }
    if has_residual
        && rays
            .residual_independence_basis
            .as_deref()
            .map(str::trim)
            .unwrap_or("")
            .is_empty()
    {
        return Err(PolicyError::Invalid(
            "nonzero residuals require an explicit independence basis".to_string(),
        ));
    }

    Ok(())
}

pub fn load_device_policy_map(path: &Path) -> Result<BTreeMap<String, DevicePolicy>, PolicyError> {
    let raw = std::fs::read_to_string(path)?;
    let map: BTreeMap<String, DevicePolicy> = serde_json::from_str(&raw)?;
    if map.is_empty() {
        return Err(PolicyError::Invalid(
            "device policy file must configure at least one station".to_string(),
        ));
    }
    for policy in map.values() {
        validate_device_policy(policy)?;
    }
    Ok(map)
}
