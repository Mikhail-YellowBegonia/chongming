//! H.264 video encoding (Record) and Annex-B access-unit demuxing (Replay) via `ffmpeg`.
//!
//! Uses `CHONGMING_FFMPEG` (falling back to `ffmpeg` in `PATH`) so V-Switch has zero
//! native FFmpeg C-library dependencies.

use std::collections::{BTreeMap, HashMap};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};

use thiserror::Error;

use crate::proto::{CameraRole, FrameIndexEntry, FramedVideoPacket, PixelFormat, VideoCodec};
use crate::storage::{
    EncodingPresetSpec, TestSceneReader, VideoTrackMeta, DEFAULT_D3B_BUFSIZE_KBPS, DEFAULT_D3B_CRF,
    DEFAULT_D3B_MAXRATE_KBPS, DEFAULT_D3B_PIXEL_FORMAT, DEFAULT_D3B_PRESET_ID,
    DEFAULT_D3B_X264_PRESET,
};

/// Default D3b H.264 FPS (30 fps).
pub const DEFAULT_D3B_H264_FPS: f64 = 30.0;
pub const DEFAULT_D3B_H264_PRESET_ID: &str = DEFAULT_D3B_PRESET_ID;
pub const DEFAULT_D3B_H264_PRESET: &str = DEFAULT_D3B_X264_PRESET;
pub const DEFAULT_D3B_H264_CRF: u32 = DEFAULT_D3B_CRF;
pub const DEFAULT_D3B_H264_MAXRATE_KBPS: u32 = DEFAULT_D3B_MAXRATE_KBPS;
pub const DEFAULT_D3B_H264_BUFSIZE_KBPS: u32 = DEFAULT_D3B_BUFSIZE_KBPS;
pub const DEFAULT_D3B_H264_PIXEL_FORMAT: &str = DEFAULT_D3B_PIXEL_FORMAT;

#[derive(Debug, Clone)]
pub struct H264EncodePreset {
    pub spec: EncodingPresetSpec,
    pub gop_size: Option<u32>,
    pub fps: f64,
    pub is_track_override: bool,
}

impl Default for H264EncodePreset {
    fn default() -> Self {
        Self {
            spec: EncodingPresetSpec::default(),
            gop_size: None,
            fps: DEFAULT_D3B_H264_FPS,
            is_track_override: false,
        }
    }
}

#[derive(Debug, Error)]
pub enum VideoError {
    #[error("invalid video track parameters: {0}")]
    InvalidParams(String),
    #[error("ffmpeg failed ({context}): {details}")]
    FfmpegFailed { context: String, details: String },
    #[error("H.264 frame count mismatch for {relpath}: expected {expected} from frame_index, got {actual}")]
    FrameCountMismatch {
        relpath: String,
        expected: usize,
        actual: usize,
    },
    #[error("storage error: {0}")]
    Storage(#[from] crate::storage::StorageError),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Clone)]
pub struct H264AccessUnit {
    pub annex_b: Vec<u8>,
    pub is_keyframe: bool,
}

/// Resolve the `ffmpeg` executable from `CHONGMING_FFMPEG` or `PATH`.
pub fn resolve_ffmpeg_bin() -> PathBuf {
    if let Ok(val) = std::env::var("CHONGMING_FFMPEG") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    PathBuf::from("ffmpeg")
}

/// Demux an `.mp4` H.264 track into per-frame Annex-B access units.
pub fn demux_h264_mp4_to_access_units(
    mp4_path: impl AsRef<Path>,
    expected_frames: Option<usize>,
    relpath_label: &str,
) -> Result<Vec<H264AccessUnit>, VideoError> {
    let mp4_path = mp4_path.as_ref();
    let ffmpeg = resolve_ffmpeg_bin();
    let output = Command::new(&ffmpeg)
        .arg("-hide_banner")
        .arg("-loglevel")
        .arg("error")
        .arg("-i")
        .arg(mp4_path)
        .arg("-c:v")
        .arg("copy")
        .arg("-bsf:v")
        .arg("h264_mp4toannexb,h264_metadata=aud=insert")
        .arg("-f")
        .arg("h264")
        .arg("pipe:1")
        .output()?;

    if !output.status.success() {
        return Err(VideoError::FfmpegFailed {
            context: format!("demux {}", mp4_path.display()),
            details: String::from_utf8_lossy(&output.stderr).trim().to_string(),
        });
    }

    let aus = split_annex_b_by_aud(&output.stdout)?;
    if let Some(expected) = expected_frames {
        if aus.len() != expected {
            return Err(VideoError::FrameCountMismatch {
                relpath: relpath_label.to_string(),
                expected,
                actual: aus.len(),
            });
        }
    }
    Ok(aus)
}

/// Split a raw H.264 Annex-B byte stream into access units using Access Unit Delimiters (NAL type 9).
pub fn split_annex_b_by_aud(stream: &[u8]) -> Result<Vec<H264AccessUnit>, VideoError> {
    let nal_ranges = find_annex_b_nals(stream);
    if nal_ranges.is_empty() {
        return Ok(Vec::new());
    }

    let mut aus: Vec<H264AccessUnit> = Vec::new();
    let mut current_start: Option<usize> = None;
    let mut current_end: usize = 0;
    let mut current_is_keyframe = false;

    for (start, end, nal_type) in nal_ranges {
        if nal_type == 9 {
            if let Some(prev_start) = current_start {
                aus.push(H264AccessUnit {
                    annex_b: stream[prev_start..current_end].to_vec(),
                    is_keyframe: current_is_keyframe,
                });
            }
            current_start = Some(start);
            current_end = end;
            current_is_keyframe = false;
        } else {
            if current_start.is_none() {
                current_start = Some(start);
            }
            current_end = end;
            if nal_type == 5 {
                current_is_keyframe = true;
            }
        }
    }

    if let Some(prev_start) = current_start {
        aus.push(H264AccessUnit {
            annex_b: stream[prev_start..current_end].to_vec(),
            is_keyframe: current_is_keyframe,
        });
    }

    Ok(aus)
}

/// Return `(nal_start_with_start_code, nal_end, nal_unit_type)` for each Annex-B NAL unit.
fn find_annex_b_nals(data: &[u8]) -> Vec<(usize, usize, u8)> {
    let mut starts: Vec<(usize, usize)> = Vec::new();
    let mut i = 0usize;
    while i + 3 <= data.len() {
        if i + 4 <= data.len() && data[i..i + 4] == [0, 0, 0, 1] {
            starts.push((i, 4));
            i += 4;
        } else if data[i..i + 3] == [0, 0, 1] {
            starts.push((i, 3));
            i += 3;
        } else {
            i += 1;
        }
    }

    let mut nals = Vec::with_capacity(starts.len());
    for idx in 0..starts.len() {
        let (sc_start, sc_len) = starts[idx];
        let nal_end = if idx + 1 < starts.len() {
            starts[idx + 1].0
        } else {
            data.len()
        };
        let header_pos = sc_start + sc_len;
        if header_pos < nal_end {
            let nal_type = data[header_pos] & 0x1f;
            nals.push((sc_start, nal_end, nal_type));
        }
    }
    nals
}

/// Demux all video tracks referenced by `entries` in an immutable `TestSceneReader`.
pub fn demux_scene_tracks(
    scene: &TestSceneReader,
    entries: &[FrameIndexEntry],
) -> Result<HashMap<String, Vec<H264AccessUnit>>, VideoError> {
    let mut counts_by_relpath: BTreeMap<String, usize> = BTreeMap::new();
    for entry in entries {
        *counts_by_relpath
            .entry(entry.video_relpath.clone())
            .or_insert(0) += 1;
    }

    let mut tracks = HashMap::with_capacity(counts_by_relpath.len());
    for (relpath, expected_count) in counts_by_relpath {
        let path = scene.video_path(&relpath)?;
        let aus = demux_h264_mp4_to_access_units(&path, Some(expected_count), &relpath)?;
        tracks.insert(relpath, aus);
    }
    Ok(tracks)
}

fn ffmpeg_raw_pix_fmt(fmt: PixelFormat) -> Result<&'static str, VideoError> {
    match fmt {
        PixelFormat::Gray8 => Ok("gray"),
        PixelFormat::Rgb8 => Ok("rgb24"),
        PixelFormat::Bgr8 => Ok("bgr24"),
        PixelFormat::Yuv420p => Ok("yuv420p"),
        PixelFormat::Yuv444p => Ok("yuv444p"),
        PixelFormat::Unspecified => Err(VideoError::InvalidParams(
            "pixel_format must be specified for RAW video packets".into(),
        )),
    }
}

fn pack_raw_frame<'a>(
    packet: &'a FramedVideoPacket,
    fmt: PixelFormat,
    scratch: &'a mut Vec<u8>,
) -> Result<&'a [u8], VideoError> {
    let w = packet.width_px as usize;
    let h = packet.height_px as usize;
    if w == 0 || h == 0 {
        return Err(VideoError::InvalidParams(
            "video frame width_px and height_px must be > 0".into(),
        ));
    }

    match fmt {
        PixelFormat::Gray8 | PixelFormat::Rgb8 | PixelFormat::Bgr8 => {
            let bpp = match fmt {
                PixelFormat::Gray8 => 1usize,
                PixelFormat::Rgb8 | PixelFormat::Bgr8 => 3usize,
                _ => unreachable!(),
            };
            let packed_row = w * bpp;
            let stride = if packet.row_stride_bytes == 0 {
                packed_row
            } else {
                packet.row_stride_bytes as usize
            };
            if stride < packed_row {
                return Err(VideoError::InvalidParams(format!(
                    "row_stride_bytes ({stride}) < packed row ({packed_row})"
                )));
            }
            let min_bytes = stride * h;
            if packet.payload.len() < min_bytes {
                return Err(VideoError::InvalidParams(format!(
                    "raw packet payload too short: {} < {min_bytes}",
                    packet.payload.len()
                )));
            }
            if stride == packed_row && packet.payload.len() == packed_row * h {
                return Ok(&packet.payload);
            }
            scratch.clear();
            scratch.reserve(packed_row * h);
            for row in 0..h {
                let start = row * stride;
                scratch.extend_from_slice(&packet.payload[start..start + packed_row]);
            }
            Ok(scratch.as_slice())
        }
        PixelFormat::Yuv420p => {
            if !w.is_multiple_of(2) || !h.is_multiple_of(2) {
                return Err(VideoError::InvalidParams(format!(
                    "YUV420P requires even dimensions, got {w}x{h}"
                )));
            }
            let expected = w * h * 3 / 2;
            if packet.payload.len() != expected {
                return Err(VideoError::InvalidParams(format!(
                    "YUV420P payload size mismatch: {} != {expected}",
                    packet.payload.len()
                )));
            }
            Ok(&packet.payload)
        }
        PixelFormat::Yuv444p => {
            let expected = w * h * 3;
            if packet.payload.len() != expected {
                return Err(VideoError::InvalidParams(format!(
                    "YUV444P payload size mismatch: {} != {expected}",
                    packet.payload.len()
                )));
            }
            Ok(&packet.payload)
        }
        PixelFormat::Unspecified => Err(VideoError::InvalidParams(
            "pixel_format must be specified".into(),
        )),
    }
}

fn pixel_format_label(fmt: PixelFormat) -> &'static str {
    match fmt {
        PixelFormat::Gray8 => "gray8",
        PixelFormat::Rgb8 => "rgb8",
        PixelFormat::Bgr8 => "bgr8",
        PixelFormat::Yuv420p => "yuv420p",
        PixelFormat::Yuv444p => "yuv444p",
        PixelFormat::Unspecified => "unspecified",
    }
}

fn camera_role_label(role: i32) -> &'static str {
    match CameraRole::try_from(role).unwrap_or(CameraRole::Unspecified) {
        CameraRole::Main => "main",
        CameraRole::Auxiliary => "auxiliary",
        CameraRole::Unspecified => "unspecified",
    }
}

/// Streaming FFmpeg subprocess encoder creating a single `.mp4` track during Record mode.
pub struct VideoTrackEncoder {
    out_path: PathBuf,
    station_id: String,
    camera_role: String,
    codec: VideoCodec,
    pixel_format: PixelFormat,
    width_px: u32,
    height_px: u32,
    preset: H264EncodePreset,
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    frame_count: u64,
    scratch: Vec<u8>,
}

impl VideoTrackEncoder {
    pub fn start(
        out_path: impl AsRef<Path>,
        first_packet: &FramedVideoPacket,
        preset: H264EncodePreset,
    ) -> Result<Self, VideoError> {
        let out_path = out_path.as_ref().to_path_buf();
        if let Some(parent) = out_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let codec = VideoCodec::try_from(first_packet.codec).unwrap_or(VideoCodec::Unspecified);
        let pixel_format =
            PixelFormat::try_from(first_packet.pixel_format).unwrap_or(PixelFormat::Unspecified);
        if first_packet.width_px == 0 || first_packet.height_px == 0 {
            return Err(VideoError::InvalidParams(
                "width_px and height_px must be > 0".into(),
            ));
        }

        let ffmpeg = resolve_ffmpeg_bin();
        let mut cmd = Command::new(&ffmpeg);
        cmd.arg("-hide_banner")
            .arg("-loglevel")
            .arg("error")
            .arg("-y");
        let fps_str = format!("{:.6}", preset.fps.max(1.0));

        match codec {
            VideoCodec::Raw => {
                let raw_fmt = ffmpeg_raw_pix_fmt(pixel_format)?;
                let size_str = format!("{}x{}", first_packet.width_px, first_packet.height_px);
                cmd.args([
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    raw_fmt,
                    "-video_size",
                    &size_str,
                    "-framerate",
                    &fps_str,
                    "-i",
                    "pipe:0",
                    "-c:v",
                    &preset.spec.codec,
                    "-preset",
                    &preset.spec.x264_preset,
                ]);
                if let Some(crf) = preset.spec.crf {
                    cmd.args(["-crf", &crf.to_string()]);
                }
                if let Some(kbps) = preset.spec.target_bitrate_kbps {
                    cmd.args(["-b:v", &format!("{kbps}k")]);
                }
                if let Some(maxrate) = preset.spec.maxrate_kbps {
                    cmd.args(["-maxrate", &format!("{maxrate}k")]);
                }
                if let Some(bufsize) = preset.spec.bufsize_kbps {
                    cmd.args(["-bufsize", &format!("{bufsize}k")]);
                }
                if let Some(gop) = preset.gop_size {
                    cmd.args(["-g", &gop.to_string()]);
                }
                // Disable B-frames so decode order matches presentation/frame_index order 1:1.
                cmd.args([
                    "-bf",
                    "0",
                    "-pix_fmt",
                    &preset.spec.encoded_pixel_format,
                    "-movflags",
                    "+faststart",
                ]);
                cmd.arg(&out_path);
            }
            VideoCodec::H264 => {
                cmd.args([
                    "-f",
                    "h264",
                    "-framerate",
                    &fps_str,
                    "-i",
                    "pipe:0",
                    "-c:v",
                    "copy",
                    "-movflags",
                    "+faststart",
                ]);
                cmd.arg(&out_path);
            }
            VideoCodec::Unspecified => {
                return Err(VideoError::InvalidParams(
                    "video packet codec must be RAW or H264".into(),
                ));
            }
        }

        cmd.stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take().ok_or_else(|| VideoError::FfmpegFailed {
            context: "spawn encoder".into(),
            details: "failed to capture ffmpeg stdin".into(),
        })?;

        Ok(Self {
            out_path,
            station_id: first_packet.station_id.clone(),
            camera_role: camera_role_label(first_packet.camera_role).to_string(),
            codec,
            pixel_format,
            width_px: first_packet.width_px,
            height_px: first_packet.height_px,
            preset,
            child: Some(child),
            stdin: Some(stdin),
            frame_count: 0,
            scratch: Vec::new(),
        })
    }

    pub fn push_packet(&mut self, packet: &FramedVideoPacket) -> Result<(), VideoError> {
        let pkt_codec = VideoCodec::try_from(packet.codec).unwrap_or(VideoCodec::Unspecified);
        let pkt_fmt =
            PixelFormat::try_from(packet.pixel_format).unwrap_or(PixelFormat::Unspecified);
        if pkt_codec != self.codec
            || pkt_fmt != self.pixel_format
            || packet.width_px != self.width_px
            || packet.height_px != self.height_px
        {
            return Err(VideoError::InvalidParams(format!(
                "video packet parameters changed mid-track for {}",
                self.out_path.display()
            )));
        }

        let bytes = match self.codec {
            VideoCodec::Raw => pack_raw_frame(packet, self.pixel_format, &mut self.scratch)?,
            VideoCodec::H264 => packet.payload.as_slice(),
            VideoCodec::Unspecified => unreachable!(),
        };

        let stdin = self
            .stdin
            .as_mut()
            .ok_or_else(|| VideoError::InvalidParams("encoder already finished".into()))?;
        stdin.write_all(bytes)?;
        self.frame_count += 1;
        Ok(())
    }

    pub fn finish(mut self) -> Result<VideoTrackMeta, VideoError> {
        if let Some(mut stdin) = self.stdin.take() {
            let _ = stdin.flush();
            drop(stdin);
        }
        if let Some(child) = self.child.take() {
            let output = child.wait_with_output()?;
            if !output.status.success() {
                return Err(VideoError::FfmpegFailed {
                    context: format!("encode {}", self.out_path.display()),
                    details: String::from_utf8_lossy(&output.stderr).trim().to_string(),
                });
            }
        }
        let file_bytes = std::fs::metadata(&self.out_path)?.len();
        let fps = self.preset.fps.max(1.0);
        let duration_s = (self.frame_count as f64) / fps;
        let average_bitrate_kbps = if duration_s > 0.0 {
            (file_bytes as f64 * 8.0) / (duration_s * 1000.0)
        } else {
            0.0
        };
        let src_pix_fmt = if self.codec == VideoCodec::H264 {
            self.preset.spec.encoded_pixel_format.clone()
        } else {
            pixel_format_label(self.pixel_format).to_string()
        };

        Ok(VideoTrackMeta {
            station_id: self.station_id.clone(),
            camera_role: self.camera_role.clone(),
            codec: self.preset.spec.codec.clone(),
            container: self.preset.spec.container.clone(),
            fps,
            frame_count: self.frame_count,
            width_px: self.width_px,
            height_px: self.height_px,
            pixel_format: src_pix_fmt,
            encoded_pixel_format: self.preset.spec.encoded_pixel_format.clone(),
            quality: self.preset.spec.preset_id.clone(),
            average_bitrate_kbps,
            encoding_preset: self
                .preset
                .is_track_override
                .then(|| self.preset.spec.clone()),
            extra: BTreeMap::new(),
        })
    }
}

impl Drop for VideoTrackEncoder {
    fn drop(&mut self) {
        drop(self.stdin.take());
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::{CameraRole, CaptureTime};
    use tempfile::TempDir;

    #[test]
    fn raw_encode_then_demux_h264_roundtrip() {
        let dir = TempDir::new().unwrap();
        let mp4_path = dir.path().join("s1-main.mp4");

        let w = 64u32;
        let h = 48u32;
        let stride = w * 3 + 4; // test non-packed stride stripping
        let make_pkt = |idx: u64| {
            let mut payload = vec![0u8; (stride * h) as usize];
            for y in 0..h {
                for x in 0..w {
                    let off = (y * stride + x * 3) as usize;
                    payload[off] = ((x * 4 + idx as u32 * 2) % 256) as u8;
                    payload[off + 1] = ((y * 4) % 256) as u8;
                    payload[off + 2] = 128;
                }
            }
            FramedVideoPacket {
                station_id: "s1".into(),
                station_session_id: "sess-1".into(),
                camera_role: CameraRole::Main as i32,
                frame_id: format!("f-{idx}"),
                frame_index: idx,
                capture_time: Some(CaptureTime {
                    clock_domain_id: "clk".into(),
                    exposure_start_ns: (idx as i64) * 33_333_333,
                    exposure_end_ns: (idx as i64) * 33_333_333,
                    ..Default::default()
                }),
                station_config_digest: "cfg-1".into(),
                width_px: w,
                height_px: h,
                codec: VideoCodec::Raw as i32,
                pixel_format: PixelFormat::Rgb8 as i32,
                row_stride_bytes: stride,
                is_keyframe: idx == 0,
                payload,
            }
        };

        let first = make_pkt(0);
        let preset = H264EncodePreset::default();
        let mut enc = VideoTrackEncoder::start(&mp4_path, &first, preset).unwrap();
        for idx in 0..5u64 {
            enc.push_packet(&make_pkt(idx)).unwrap();
        }
        let meta = enc.finish().unwrap();
        assert_eq!(meta.station_id, "s1");
        assert_eq!(meta.camera_role, "main");
        assert_eq!(meta.codec, "libx264");
        assert_eq!(meta.container, "mp4");
        assert_eq!(meta.pixel_format, "rgb8");
        assert_eq!(meta.encoded_pixel_format, "yuv420p");
        assert_eq!(meta.quality, DEFAULT_D3B_H264_PRESET_ID);
        assert!(meta.average_bitrate_kbps > 0.0);
        assert_eq!(meta.frame_count, 5);
        assert_eq!(meta.width_px, 64);
        assert_eq!(meta.height_px, 48);

        let aus = demux_h264_mp4_to_access_units(&mp4_path, Some(5), "video/s1-main.mp4").unwrap();
        assert_eq!(aus.len(), 5);
        assert!(aus[0].is_keyframe);
        assert!(!aus[1].is_keyframe);
        assert!(aus.iter().all(|au| !au.annex_b.is_empty()));

        // Verify frame count mismatch fails cleanly
        assert!(matches!(
            demux_h264_mp4_to_access_units(&mp4_path, Some(4), "video/s1-main.mp4"),
            Err(VideoError::FrameCountMismatch {
                expected: 4,
                actual: 5,
                ..
            })
        ));
    }
}
