//! FrameSource abstraction and virtual V-Switch video packet decoder.

use std::collections::HashMap;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Instant;

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::bus::{BusClientError, OnlineBusClient};
use crate::framing::{read_length_prefixed_async, FramingError, MAX_FRAME_BYTES};
use crate::proto::{
    bus_message, CameraRole, CaptureTime, FramedVideoPacket, PixelFormat, StationConfig, VideoCodec,
};

#[derive(Debug, Error)]
pub enum FrameSourceError {
    #[error("bus error: {0}")]
    Bus(#[from] BusClientError),
    #[error("framing error: {0}")]
    Framing(#[from] FramingError),
    #[error("invalid video packet: {0}")]
    InvalidPacket(String),
    #[error("H.264 decode failed: {0}")]
    DecodeFailed(String),
    #[error("unexpected BusMessage on Edge input stream: {0}")]
    UnexpectedMessage(String),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LocalPixelFormat {
    Gray8,
    Rgb8,
    Bgr8,
}

impl LocalPixelFormat {
    pub fn channels(self) -> usize {
        match self {
            Self::Gray8 => 1,
            Self::Rgb8 | Self::Bgr8 => 3,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ImagePlane {
    pub width_px: u32,
    pub height_px: u32,
    pub pixel_format: LocalPixelFormat,
    pub row_stride_bytes: usize,
    pub pixels: Vec<u8>,
}

#[derive(Debug, Clone)]
pub struct CameraFrame {
    pub station_id: String,
    pub station_session_id: String,
    pub camera_role: CameraRole,
    pub frame_id: String,
    pub capture_time: CaptureTime,
    pub image: ImagePlane,
    pub camera_config_ref: String,
}

#[derive(Debug, Clone)]
pub struct DecodedFrame {
    pub frame: CameraFrame,
    pub frame_index: u64,
    pub decode_duration_ns: u64,
    pub decoded_pixel_sha256: String,
}

#[allow(async_fn_in_trait)]
pub trait FrameSource {
    fn take_station_configs(&mut self) -> Vec<StationConfig>;
    async fn next_frame(&mut self) -> Result<Option<DecodedFrame>, FrameSourceError>;
    fn close(&mut self);
}

pub fn resolve_ffmpeg_bin(override_bin: Option<&str>) -> PathBuf {
    if let Some(bin) = override_bin {
        let trimmed = bin.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    if let Ok(val) = std::env::var("CHONGMING_FFMPEG") {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    PathBuf::from("ffmpeg")
}

/// Split an Annex-B byte stream into individual NAL units without start codes.
pub fn split_annex_b_nals(payload: &[u8]) -> Vec<&[u8]> {
    let mut nals = Vec::new();
    let len = payload.len();
    let mut pos = 0usize;
    let mut current_start: Option<usize> = None;

    while pos + 2 < len {
        if let Some(rel) = payload[pos..].windows(3).position(|w| w == [0, 0, 1]) {
            let idx = pos + rel;
            let code_start = if idx > 0 && payload[idx - 1] == 0 {
                idx - 1
            } else {
                idx
            };
            if let Some(start) = current_start {
                if code_start > start {
                    nals.push(&payload[start..code_start]);
                }
            }
            current_start = Some(idx + 3);
            pos = idx + 3;
        } else {
            break;
        }
    }
    if let Some(start) = current_start {
        if start < len {
            nals.push(&payload[start..]);
        }
    }
    nals
}

/// GOP-aware Annex-B H.264 decoder yielding RGB8 frames via `ffmpeg`.
pub struct H264StreamDecoder {
    width_px: u32,
    height_px: u32,
    frame_bytes: usize,
    ffmpeg_bin: PathBuf,
    sps: Option<Vec<u8>>,
    pps: Option<Vec<u8>>,
    gop_buf: Vec<u8>,
    gop_frame_count: usize,
}

impl H264StreamDecoder {
    pub fn new(
        width_px: u32,
        height_px: u32,
        ffmpeg_bin: Option<&str>,
    ) -> Result<Self, FrameSourceError> {
        if width_px == 0 || height_px == 0 {
            return Err(FrameSourceError::InvalidPacket(
                "H.264 stream dimensions must be positive".to_string(),
            ));
        }
        let frame_bytes = (width_px as usize)
            .checked_mul(height_px as usize)
            .and_then(|n| n.checked_mul(3))
            .ok_or_else(|| FrameSourceError::InvalidPacket("image byte count overflow".into()))?;
        Ok(Self {
            width_px,
            height_px,
            frame_bytes,
            ffmpeg_bin: resolve_ffmpeg_bin(ffmpeg_bin),
            sps: None,
            pps: None,
            gop_buf: Vec::new(),
            gop_frame_count: 0,
        })
    }

    pub fn decode(
        &mut self,
        payload: &[u8],
        is_keyframe: bool,
    ) -> Result<Vec<u8>, FrameSourceError> {
        let nals = split_annex_b_nals(payload);
        if nals.is_empty() {
            return Err(FrameSourceError::DecodeFailed(
                "H.264 FramedVideoPacket contains no Annex-B NAL units".to_string(),
            ));
        }

        let mut saw_idr = false;
        let mut saw_param_update = false;
        let mut picture_nals: Vec<Vec<u8>> = Vec::new();

        for nal in nals {
            if nal.is_empty() {
                continue;
            }
            let nal_type = nal[0] & 0x1F;
            let mut prefixed = Vec::with_capacity(4 + nal.len());
            prefixed.extend_from_slice(&[0, 0, 0, 1]);
            prefixed.extend_from_slice(nal);
            match nal_type {
                7 => {
                    self.sps = Some(prefixed);
                    saw_param_update = true;
                }
                8 => {
                    self.pps = Some(prefixed);
                    saw_param_update = true;
                }
                9 => continue,
                5 => {
                    saw_idr = true;
                    picture_nals.push(prefixed);
                }
                _ => picture_nals.push(prefixed),
            }
        }

        if picture_nals.is_empty() {
            return Err(FrameSourceError::DecodeFailed(
                "H.264 FramedVideoPacket contains no video slice NAL units".to_string(),
            ));
        }
        let sps = self.sps.as_ref().ok_or_else(|| {
            FrameSourceError::DecodeFailed(
                "H.264 stream missing SPS before coded picture".to_string(),
            )
        })?;
        let pps = self.pps.as_ref().ok_or_else(|| {
            FrameSourceError::DecodeFailed(
                "H.264 stream missing PPS before coded picture".to_string(),
            )
        })?;

        if saw_idr || is_keyframe || self.gop_buf.is_empty() {
            self.gop_buf.clear();
            self.gop_buf.extend_from_slice(sps);
            self.gop_buf.extend_from_slice(pps);
            self.gop_frame_count = 0;
        } else if saw_param_update {
            self.gop_buf.extend_from_slice(sps);
            self.gop_buf.extend_from_slice(pps);
        }
        for nal in picture_nals {
            self.gop_buf.extend_from_slice(&nal);
        }

        let target_index = self.gop_frame_count;
        self.gop_frame_count += 1;

        let mut cmd = Command::new(&self.ffmpeg_bin);
        cmd.args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-analyzeduration",
            "0",
            "-framerate",
            "30",
            "-f",
            "h264",
            "-i",
            "pipe:0",
        ]);
        if target_index > 0 {
            cmd.args([
                "-vf",
                &format!("select=eq(n\\,{target_index})"),
                "-fps_mode",
                "passthrough",
            ]);
        }
        cmd.args([
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]);
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn().map_err(|err| {
            FrameSourceError::DecodeFailed(format!(
                "failed to spawn ffmpeg ({}): {err}",
                self.ffmpeg_bin.display()
            ))
        })?;
        if let Some(mut stdin) = child.stdin.take() {
            stdin.write_all(&self.gop_buf)?;
        }
        let output = child.wait_with_output()?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(FrameSourceError::DecodeFailed(format!(
                "FFmpeg H.264 decode failed: {detail}"
            )));
        }
        if output.stdout.len() != self.frame_bytes {
            return Err(FrameSourceError::DecodeFailed(format!(
                "FFmpeg decoded {} bytes, expected {}",
                output.stdout.len(),
                self.frame_bytes
            )));
        }
        Ok(output.stdout)
    }
}

pub fn decode_video_packet(
    packet: FramedVideoPacket,
    h264_decoders: &mut HashMap<(String, i32), H264StreamDecoder>,
    ffmpeg_bin: Option<&str>,
) -> Result<DecodedFrame, FrameSourceError> {
    if packet.station_id.is_empty()
        || packet.station_session_id.is_empty()
        || packet.frame_id.is_empty()
    {
        return Err(FrameSourceError::InvalidPacket(
            "FramedVideoPacket requires station_id, station_session_id, and frame_id".to_string(),
        ));
    }
    if packet.station_config_digest.is_empty() {
        return Err(FrameSourceError::InvalidPacket(
            "FramedVideoPacket requires station_config_digest".to_string(),
        ));
    }
    if packet.width_px == 0 || packet.height_px == 0 {
        return Err(FrameSourceError::InvalidPacket(
            "FramedVideoPacket dimensions must be positive".to_string(),
        ));
    }

    let role = CameraRole::try_from(packet.camera_role).unwrap_or(CameraRole::Unspecified);
    if role == CameraRole::Unspecified {
        return Err(FrameSourceError::InvalidPacket(
            "FramedVideoPacket requires CAMERA_ROLE_MAIN or CAMERA_ROLE_AUXILIARY".to_string(),
        ));
    }
    let capture_time = packet.capture_time.ok_or_else(|| {
        FrameSourceError::InvalidPacket("FramedVideoPacket missing capture_time".to_string())
    })?;
    if capture_time.clock_domain_id.trim().is_empty()
        || capture_time.exposure_end_ns < capture_time.exposure_start_ns
        || capture_time.clock_uncertainty_ns < 0
        || capture_time.rolling_shutter_model.trim().is_empty()
    {
        return Err(FrameSourceError::InvalidPacket(
            "FramedVideoPacket contains invalid CaptureTime".to_string(),
        ));
    }

    let width = packet.width_px;
    let height = packet.height_px;
    let codec = VideoCodec::try_from(packet.codec).unwrap_or(VideoCodec::Unspecified);

    let (pixels, pixel_format, stride, decode_ns) = match codec {
        VideoCodec::Raw => {
            let pb_fmt =
                PixelFormat::try_from(packet.pixel_format).unwrap_or(PixelFormat::Unspecified);
            let fmt = match pb_fmt {
                PixelFormat::Gray8 => LocalPixelFormat::Gray8,
                PixelFormat::Rgb8 => LocalPixelFormat::Rgb8,
                PixelFormat::Bgr8 => LocalPixelFormat::Bgr8,
                other => {
                    return Err(FrameSourceError::InvalidPacket(format!(
                        "unsupported RAW pixel format: {other:?}"
                    )));
                }
            };
            let t0 = Instant::now();
            let stride = packet.row_stride_bytes as usize;
            let min_stride = (width as usize) * fmt.channels();
            if stride < min_stride {
                return Err(FrameSourceError::InvalidPacket(format!(
                    "RAW row_stride_bytes {stride} smaller than {min_stride}"
                )));
            }
            let expected_bytes = stride * (height as usize);
            if packet.payload.len() != expected_bytes {
                return Err(FrameSourceError::InvalidPacket(format!(
                    "RAW payload size {} != expected {expected_bytes}",
                    packet.payload.len()
                )));
            }
            let elapsed = u64::try_from(t0.elapsed().as_nanos()).unwrap_or(u64::MAX);
            (packet.payload, fmt, stride, elapsed)
        }
        VideoCodec::H264 => {
            let key = (packet.station_id.clone(), role as i32);
            let decoder = match h264_decoders.entry(key) {
                std::collections::hash_map::Entry::Occupied(entry) => {
                    let dec = entry.into_mut();
                    if (dec.width_px, dec.height_px) != (width, height) {
                        return Err(FrameSourceError::InvalidPacket(
                            "H.264 stream dimensions changed mid-session".to_string(),
                        ));
                    }
                    dec
                }
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(H264StreamDecoder::new(width, height, ffmpeg_bin)?)
                }
            };
            let t0 = Instant::now();
            let decoded = decoder.decode(&packet.payload, packet.is_keyframe)?;
            let elapsed = u64::try_from(t0.elapsed().as_nanos()).unwrap_or(u64::MAX);
            (
                decoded,
                LocalPixelFormat::Rgb8,
                (width as usize) * 3,
                elapsed,
            )
        }
        other => {
            return Err(FrameSourceError::InvalidPacket(format!(
                "unsupported FramedVideoPacket codec: {other:?}"
            )));
        }
    };

    let digest = hex::encode(Sha256::digest(&pixels));
    Ok(DecodedFrame {
        frame: CameraFrame {
            station_id: packet.station_id,
            station_session_id: packet.station_session_id,
            camera_role: role,
            frame_id: packet.frame_id,
            capture_time,
            image: ImagePlane {
                width_px: width,
                height_px: height,
                pixel_format,
                row_stride_bytes: stride,
                pixels,
            },
            camera_config_ref: packet.station_config_digest,
        },
        frame_index: packet.frame_index,
        decode_duration_ns: decode_ns,
        decoded_pixel_sha256: digest,
    })
}

/// FrameSource reading `StationConfig`, `FramedVideoPacket`, and `ProgressReport` from V-Switch.
pub struct VirtualBusFrameSource<R> {
    reader: R,
    timeout: std::time::Duration,
    ffmpeg_bin: Option<String>,
    h264_decoders: HashMap<(String, i32), H264StreamDecoder>,
    pending_configs: Vec<StationConfig>,
}

impl<R: tokio::io::AsyncRead + Unpin> VirtualBusFrameSource<R> {
    pub fn new(reader: R, timeout: std::time::Duration, ffmpeg_bin: Option<String>) -> Self {
        Self {
            reader,
            timeout,
            ffmpeg_bin,
            h264_decoders: HashMap::new(),
            pending_configs: Vec::new(),
        }
    }
}

impl<R: tokio::io::AsyncRead + Unpin> FrameSource for VirtualBusFrameSource<R> {
    fn take_station_configs(&mut self) -> Vec<StationConfig> {
        std::mem::take(&mut self.pending_configs)
    }

    async fn next_frame(&mut self) -> Result<Option<DecodedFrame>, FrameSourceError> {
        loop {
            let msg: Option<crate::proto::BusMessage> = tokio::time::timeout(
                self.timeout,
                read_length_prefixed_async(&mut self.reader, MAX_FRAME_BYTES),
            )
            .await
            .map_err(|_| BusClientError::Timeout(self.timeout))??;
            let Some(msg) = msg else {
                return Ok(None);
            };
            match msg.payload {
                Some(bus_message::Payload::StationConfig(cfg)) => {
                    self.pending_configs.push(cfg);
                }
                Some(bus_message::Payload::VideoPacket(packet)) => {
                    let decoded = decode_video_packet(
                        packet,
                        &mut self.h264_decoders,
                        self.ffmpeg_bin.as_deref(),
                    )?;
                    return Ok(Some(decoded));
                }
                Some(bus_message::Payload::ProgressReport(report)) if report.finite_end_reached => {
                    return Ok(None);
                }
                other => {
                    return Err(FrameSourceError::UnexpectedMessage(format!("{other:?}")));
                }
            }
        }
    }

    fn close(&mut self) {
        self.h264_decoders.clear();
    }
}

/// Split `OnlineBusClient` into a reader handle for `VirtualBusFrameSource` and a writer handle for `EdgeProcess`.
pub struct BusHandle {
    pub reader: tokio::net::tcp::OwnedReadHalf,
    pub writer: tokio::net::tcp::OwnedWriteHalf,
    pub timeout: std::time::Duration,
    pub next_seq: u64,
}

impl OnlineBusClient {
    pub fn into_split(self) -> (tokio::net::tcp::OwnedReadHalf, BusWriter) {
        // Expose reader/writer split after handshake.
        let (reader, writer, timeout, next_seq) = self.into_parts();
        (
            reader,
            BusWriter {
                writer,
                timeout,
                next_seq,
            },
        )
    }
}

pub struct BusWriter {
    writer: tokio::net::tcp::OwnedWriteHalf,
    timeout: std::time::Duration,
    next_seq: u64,
}

impl BusWriter {
    pub async fn send_payload(
        &mut self,
        payload: bus_message::Payload,
    ) -> Result<(), BusClientError> {
        let seq = self.next_seq;
        self.next_seq += 1;
        let msg = crate::proto::BusMessage {
            bus_sequence_no: seq,
            sent_wall_time_ns: crate::proto::now_wall_time_ns(),
            payload: Some(payload),
        };
        tokio::time::timeout(
            self.timeout,
            crate::framing::write_length_prefixed_async(&mut self.writer, &msg, MAX_FRAME_BYTES),
        )
        .await
        .map_err(|_| BusClientError::Timeout(self.timeout))??;
        Ok(())
    }

    pub async fn shutdown(&mut self) {
        use tokio::io::AsyncWriteExt;
        let _ = self.writer.shutdown().await;
    }
}
