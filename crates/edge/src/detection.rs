//! Pure-Rust foreground detection (MOG2 & frame-difference/occupancy) and 2D tracking.

use std::collections::{BTreeMap, HashMap};

use thiserror::Error;

use crate::frame_source::{CameraFrame, ImagePlane, LocalPixelFormat};
use crate::math::{invert_2x2, Mat2};
use crate::policy::{
    fill_ratio_score, DetectionPolicyConfig, FrameDifferencePolicy, KalmanTrackingPolicy,
    Mog2DetectionPolicy, Tracking2DPolicy, TrackingPolicyConfig,
};
use crate::proto::{CameraRole, CaptureTime, FrameRef, Observation2D, ProcessingStatus, Vec2};

#[derive(Debug, Error)]
pub enum DetectionError {
    #[error("{0}")]
    InvalidInput(String),
}

pub fn capture_midpoint_ns(capture: &CaptureTime) -> Result<i64, DetectionError> {
    if capture.clock_domain_id.trim().is_empty()
        || capture.exposure_end_ns < capture.exposure_start_ns
        || capture.clock_uncertainty_ns < 0
    {
        return Err(DetectionError::InvalidInput(
            "invalid capture time or clock uncertainty".to_string(),
        ));
    }
    Ok(capture.exposure_start_ns + (capture.exposure_end_ns - capture.exposure_start_ns) / 2)
}

pub fn frame_ref(frame: &CameraFrame) -> FrameRef {
    FrameRef {
        station_id: frame.station_id.clone(),
        station_session_id: frame.station_session_id.clone(),
        camera_role: frame.camera_role as i32,
        frame_id: frame.frame_id.clone(),
    }
}

/// Convert `ImagePlane` (`GRAY8`, `RGB8`, or `BGR8`) to packed `GRAY8` using BT.601 weights.
pub fn image_to_gray(image: &ImagePlane) -> Result<Vec<u8>, DetectionError> {
    let w = image.width_px as usize;
    let h = image.height_px as usize;
    if w == 0 || h == 0 {
        return Err(DetectionError::InvalidInput(
            "image dimensions must be positive".to_string(),
        ));
    }
    let ch = image.pixel_format.channels();
    let min_stride = w * ch;
    if image.row_stride_bytes < min_stride {
        return Err(DetectionError::InvalidInput(
            "image row stride is smaller than packed row".to_string(),
        ));
    }
    if image.pixels.len() < image.row_stride_bytes * h {
        return Err(DetectionError::InvalidInput(
            "image buffer shorter than declared stride * height".to_string(),
        ));
    }
    let mut gray = Vec::with_capacity(w * h);
    match image.pixel_format {
        LocalPixelFormat::Gray8 => {
            for y in 0..h {
                let row = &image.pixels[y * image.row_stride_bytes..y * image.row_stride_bytes + w];
                gray.extend_from_slice(row);
            }
        }
        LocalPixelFormat::Rgb8 => {
            for y in 0..h {
                let row =
                    &image.pixels[y * image.row_stride_bytes..y * image.row_stride_bytes + w * 3];
                for px in row.as_chunks::<3>().0 {
                    let y_val = (0.299 * px[0] as f64 + 0.587 * px[1] as f64 + 0.114 * px[2] as f64)
                        .round()
                        .clamp(0.0, 255.0) as u8;
                    gray.push(y_val);
                }
            }
        }
        LocalPixelFormat::Bgr8 => {
            for y in 0..h {
                let row =
                    &image.pixels[y * image.row_stride_bytes..y * image.row_stride_bytes + w * 3];
                for px in row.as_chunks::<3>().0 {
                    let y_val = (0.299 * px[2] as f64 + 0.587 * px[1] as f64 + 0.114 * px[0] as f64)
                        .round()
                        .clamp(0.0, 255.0) as u8;
                    gray.push(y_val);
                }
            }
        }
    }
    Ok(gray)
}

#[derive(Debug, Clone)]
pub struct Candidate2D {
    pub centroid_px: [f64; 2],
    pub detection_score: f64,
}

#[derive(Debug, Clone)]
pub struct Candidate2DBatch {
    pub main_frame_ref: FrameRef,
    pub capture_time: CaptureTime,
    pub processing_status: ProcessingStatus,
    pub candidates: Vec<Candidate2D>,
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Track2DState {
    pub local_track_id: String,
    pub centroid_px: [f64; 2],
    pub velocity_pxps: Option<[f64; 2]>,
    pub lifecycle: String,
    pub track_quality: f64,
}

#[derive(Debug, Clone)]
pub struct Tracking2DResult {
    pub observations: Vec<Observation2D>,
    pub tracks: Vec<Track2DState>,
}

#[derive(Debug, Clone)]
pub struct MainDetectionResult {
    pub main_frame_ref: FrameRef,
    pub capture_time: CaptureTime,
    pub processing_status: ProcessingStatus,
    pub observations: Vec<Observation2D>,
    pub reason_codes: Vec<String>,
    pub tracks: Vec<Track2DState>,
}

/// Zivkovic adaptive per-pixel GMM background subtractor (`BackgroundSubtractorMOG2` parity).
#[derive(Debug, Clone)]
pub struct Mog2Model {
    width: usize,
    height: usize,
    history: usize,
    var_threshold: f64,
    detect_shadows: bool,
    frame_count: usize,
    n_modes: usize,
    // Per pixel: up to `n_modes` Gaussians (weight, mean, variance), plus active count
    weights: Vec<f32>,
    means: Vec<f32>,
    vars: Vec<f32>,
    used_modes: Vec<u8>,
}

impl Mog2Model {
    const MAX_MODES: usize = 5;
    const VAR_INIT: f32 = 15.0;
    const VAR_MIN: f32 = 4.0;
    const VAR_MAX: f32 = 5.0 * 15.0;
    const BG_RATIO: f32 = 0.9;
    const VAR_THRESHOLD_GEN: f32 = 9.0;
    const CT: f32 = 0.05;
    const SHADOW_TAU: f32 = 0.5;
    const SHADOW_VAL: u8 = 127;

    pub fn new(
        width: usize,
        height: usize,
        history: usize,
        var_threshold: f64,
        detect_shadows: bool,
    ) -> Self {
        let pixels = width * height;
        Self {
            width,
            height,
            history: history.max(1),
            var_threshold,
            detect_shadows,
            frame_count: 0,
            n_modes: Self::MAX_MODES,
            weights: vec![0.0; pixels * Self::MAX_MODES],
            means: vec![0.0; pixels * Self::MAX_MODES],
            vars: vec![Self::VAR_INIT; pixels * Self::MAX_MODES],
            used_modes: vec![0; pixels],
        }
    }

    pub fn apply(&mut self, gray: &[u8], learning_rate: f64) -> Vec<u8> {
        let pixels = self.width * self.height;
        assert_eq!(gray.len(), pixels);
        self.frame_count += 1;

        let alpha_t: f32 = if learning_rate < 0.0 {
            let denom = self.frame_count.min(self.history) as f32;
            (1.0 / denom).clamp(1e-6, 1.0)
        } else {
            (learning_rate as f32).clamp(0.0, 1.0)
        };
        let alpha1 = 1.0 - alpha_t;
        let prune = -alpha_t * Self::CT;
        let var_thresh = self.var_threshold as f32;

        let mut mask = vec![0u8; pixels];
        for (px_idx, &val_u8) in gray.iter().enumerate() {
            let val = val_u8 as f32;
            let base = px_idx * self.n_modes;
            let n_used = self.used_modes[px_idx] as usize;

            let mut background = false;
            let mut fits = false;
            let mut total_w = 0.0_f32;
            let mut swap_target: Option<usize> = None;

            for m in 0..n_used {
                let idx = base + m;
                let mut w = self.weights[idx];
                if !fits {
                    let d = val - self.means[idx];
                    let dist2 = d * d;
                    let var = self.vars[idx];
                    if total_w < Self::BG_RATIO && dist2 < var_thresh * var {
                        background = true;
                    }
                    if dist2 < Self::VAR_THRESHOLD_GEN * var {
                        fits = true;
                        w = alpha1 * w + alpha_t;
                        let k = alpha_t / w.max(1e-6);
                        let new_mean = self.means[idx] + k * d;
                        let new_var = (var + k * (dist2 - var)).clamp(Self::VAR_MIN, Self::VAR_MAX);
                        self.means[idx] = new_mean;
                        self.vars[idx] = new_var;
                        self.weights[idx] = w;
                        swap_target = Some(m);
                    } else {
                        w = alpha1 * w + prune;
                        self.weights[idx] = w;
                    }
                } else {
                    w = alpha1 * w + prune;
                    self.weights[idx] = w;
                }
                total_w += self.weights[idx];
            }

            // Reorder matched mode upward if its weight grew larger than preceding modes.
            if let Some(mut pos) = swap_target {
                while pos > 0 && self.weights[base + pos] > self.weights[base + pos - 1] {
                    self.weights.swap(base + pos, base + pos - 1);
                    self.means.swap(base + pos, base + pos - 1);
                    self.vars.swap(base + pos, base + pos - 1);
                    pos -= 1;
                }
            }

            // Prune negative-weight modes, then normalize weights.
            let mut active = n_used;
            while active > 0 && self.weights[base + active - 1] < 0.0 {
                active -= 1;
            }
            if !fits && alpha_t > 0.0 {
                if active < self.n_modes {
                    active += 1;
                }
                let idx = base + active - 1;
                self.weights[idx] = alpha_t;
                self.means[idx] = val;
                self.vars[idx] = Self::VAR_INIT;
                let mut pos = active - 1;
                while pos > 0 && self.weights[base + pos] > self.weights[base + pos - 1] {
                    self.weights.swap(base + pos, base + pos - 1);
                    self.means.swap(base + pos, base + pos - 1);
                    self.vars.swap(base + pos, base + pos - 1);
                    pos -= 1;
                }
            } else if active == 0 && n_used == 0 {
                active = 1;
                self.weights[base] = 1.0;
                self.means[base] = val;
                self.vars[base] = Self::VAR_INIT;
            }

            let sum_w: f32 = (0..active).map(|m| self.weights[base + m].max(0.0)).sum();
            if sum_w > 0.0 {
                let inv = 1.0 / sum_w;
                for m in 0..active {
                    self.weights[base + m] *= inv;
                }
            }
            self.used_modes[px_idx] = active as u8;

            if !background {
                let mut is_shadow = false;
                if self.detect_shadows {
                    let mut bg_w = 0.0_f32;
                    for m in 0..active {
                        let idx = base + m;
                        let mean = self.means[idx];
                        let var = self.vars[idx];
                        if val >= Self::SHADOW_TAU * mean
                            && val <= mean
                            && (val - mean).powi(2) < var_thresh * var * 4.0
                        {
                            is_shadow = true;
                            break;
                        }
                        bg_w += self.weights[idx];
                        if bg_w >= Self::BG_RATIO {
                            break;
                        }
                    }
                }
                mask[px_idx] = if is_shadow { Self::SHADOW_VAL } else { 255 };
            }
        }
        mask
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ConnectedComponent {
    pub area: usize,
    pub width: usize,
    pub height: usize,
    pub centroid_x: f64,
    pub centroid_y: f64,
}

/// 8-connected components on a binary mask (`!= 0` is foreground).
pub fn connected_components_8(mask: &[u8], width: usize, height: usize) -> Vec<ConnectedComponent> {
    assert_eq!(mask.len(), width * height);
    let mut visited = vec![false; width * height];
    let mut queue = Vec::new();
    let mut out = Vec::new();

    for start_y in 0..height {
        for start_x in 0..width {
            let start_idx = start_y * width + start_x;
            if mask[start_idx] == 0 || visited[start_idx] {
                continue;
            }
            visited[start_idx] = true;
            queue.clear();
            queue.push((start_x, start_y));
            let mut head = 0usize;
            let mut area = 0usize;
            let mut sum_x = 0.0_f64;
            let mut sum_y = 0.0_f64;
            let mut min_x = start_x;
            let mut max_x = start_x;
            let mut min_y = start_y;
            let mut max_y = start_y;

            while head < queue.len() {
                let (x, y) = queue[head];
                head += 1;
                area += 1;
                sum_x += x as f64;
                sum_y += y as f64;
                min_x = min_x.min(x);
                max_x = max_x.max(x);
                min_y = min_y.min(y);
                max_y = max_y.max(y);

                let y0 = y.saturating_sub(1);
                let y1 = (y + 1).min(height - 1);
                let x0 = x.saturating_sub(1);
                let x1 = (x + 1).min(width - 1);
                for ny in y0..=y1 {
                    for nx in x0..=x1 {
                        let nidx = ny * width + nx;
                        if mask[nidx] != 0 && !visited[nidx] {
                            visited[nidx] = true;
                            queue.push((nx, ny));
                        }
                    }
                }
            }

            out.push(ConnectedComponent {
                area,
                width: max_x - min_x + 1,
                height: max_y - min_y + 1,
                centroid_x: sum_x / (area as f64),
                centroid_y: sum_y / (area as f64),
            });
        }
    }
    out
}

fn elliptical_offsets(ksize: usize) -> Vec<(isize, isize)> {
    if ksize <= 1 {
        return vec![(0, 0)];
    }
    let rx = (ksize as f64) * 0.5;
    let ry = (ksize as f64) * 0.5;
    let cx = ((ksize - 1) as f64) * 0.5;
    let cy = ((ksize - 1) as f64) * 0.5;
    let mut offsets = Vec::new();
    for y in 0..ksize {
        for x in 0..ksize {
            let dx = (x as f64) - cx;
            let dy = (y as f64) - cy;
            if (dx * dx) / (rx * rx + 1e-12) + (dy * dy) / (ry * ry + 1e-12) <= 1.0 + 1e-9 {
                offsets.push((
                    x as isize - (ksize / 2) as isize,
                    y as isize - (ksize / 2) as isize,
                ));
            }
        }
    }
    if offsets.is_empty() {
        offsets.push((0, 0));
    }
    offsets
}

fn dilate_binary(mask: &[u8], w: usize, h: usize, offsets: &[(isize, isize)]) -> Vec<u8> {
    let mut out = vec![0u8; w * h];
    for y in 0..h {
        for x in 0..w {
            if mask[y * w + x] == 0 {
                continue;
            }
            for &(dx, dy) in offsets {
                let nx = x as isize + dx;
                let ny = y as isize + dy;
                if nx >= 0 && nx < w as isize && ny >= 0 && ny < h as isize {
                    out[(ny as usize) * w + (nx as usize)] = 255;
                }
            }
        }
    }
    out
}

fn erode_binary(mask: &[u8], w: usize, h: usize, offsets: &[(isize, isize)]) -> Vec<u8> {
    let mut out = vec![0u8; w * h];
    for y in 0..h {
        for x in 0..w {
            let mut all_set = true;
            for &(dx, dy) in offsets {
                let nx = x as isize + dx;
                let ny = y as isize + dy;
                if nx < 0 || nx >= w as isize || ny < 0 || ny >= h as isize {
                    all_set = false;
                    break;
                }
                if mask[(ny as usize) * w + (nx as usize)] == 0 {
                    all_set = false;
                    break;
                }
            }
            if all_set {
                out[y * w + x] = 255;
            }
        }
    }
    out
}

fn downsample_gray(gray: &[u8], w: usize, h: usize, factor: usize) -> (Vec<u8>, usize, usize) {
    if factor <= 1 {
        return (gray.to_vec(), w, h);
    }
    let mut cur = gray.to_vec();
    let mut cw = w;
    let mut ch = h;
    let mut remaining = factor;
    while remaining >= 2 {
        let nw = cw.div_ceil(2);
        let nh = ch.div_ceil(2);
        let mut next = vec![0u8; nw * nh];
        for y in 0..nh {
            for x in 0..nw {
                let sx = (x * 2).min(cw - 1);
                let sy = (y * 2).min(ch - 1);
                let sx1 = (sx + 1).min(cw - 1);
                let sy1 = (sy + 1).min(ch - 1);
                let sum = cur[sy * cw + sx] as u32
                    + cur[sy * cw + sx1] as u32
                    + cur[sy1 * cw + sx] as u32
                    + cur[sy1 * cw + sx1] as u32;
                next[y * nw + x] = ((sum + 2) / 4) as u8;
            }
        }
        cur = next;
        cw = nw;
        ch = nh;
        remaining /= 2;
    }
    (cur, cw, ch)
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct StreamSignature {
    station_id: String,
    station_session_id: String,
    config_ref: String,
    clock_domain_id: String,
    width_px: u32,
    height_px: u32,
    pixel_format: LocalPixelFormat,
}

impl StreamSignature {
    fn from_frame(frame: &CameraFrame) -> Self {
        Self {
            station_id: frame.station_id.clone(),
            station_session_id: frame.station_session_id.clone(),
            config_ref: frame.camera_config_ref.clone(),
            clock_domain_id: frame.capture_time.clock_domain_id.clone(),
            width_px: frame.image.width_px,
            height_px: frame.image.height_px,
            pixel_format: frame.image.pixel_format,
        }
    }
}

pub enum MotionDetectorImpl {
    Standard(StandardMog2Detector),
    Workbench(WorkbenchDetector),
}

impl MotionDetectorImpl {
    pub fn detect(&mut self, frame: &CameraFrame) -> Result<Candidate2DBatch, DetectionError> {
        match self {
            Self::Standard(det) => det.detect(frame),
            Self::Workbench(det) => det.detect(frame),
        }
    }
}

pub struct StandardMog2Detector {
    policy: Mog2DetectionPolicy,
    model: Option<Mog2Model>,
    stream: Option<StreamSignature>,
    last_time_ns: Option<i64>,
    frame_count: i64,
}

impl StandardMog2Detector {
    pub fn new(policy: Mog2DetectionPolicy) -> Self {
        Self {
            policy,
            model: None,
            stream: None,
            last_time_ns: None,
            frame_count: 0,
        }
    }

    pub fn detect(&mut self, frame: &CameraFrame) -> Result<Candidate2DBatch, DetectionError> {
        if frame.camera_role != CameraRole::Main {
            return Err(DetectionError::InvalidInput(
                "main camera frame required".to_string(),
            ));
        }
        if frame.station_id.is_empty()
            || frame.station_session_id.is_empty()
            || frame.frame_id.is_empty()
            || frame.camera_config_ref.is_empty()
        {
            return Err(DetectionError::InvalidInput(
                "invalid main frame identity".to_string(),
            ));
        }
        let midpoint = capture_midpoint_ns(&frame.capture_time)?;
        if frame.image.width_px == 0 || frame.image.height_px == 0 {
            return Err(DetectionError::InvalidInput(
                "invalid main image dimensions".to_string(),
            ));
        }
        let sig = StreamSignature::from_frame(frame);
        if let Some(prev) = &self.stream {
            if prev != &sig {
                return Err(DetectionError::InvalidInput(
                    "main stream mismatch".to_string(),
                ));
            }
        }
        if let Some(prev_t) = self.last_time_ns {
            if midpoint <= prev_t {
                return Err(DetectionError::InvalidInput(
                    "main exposure must increase strictly".to_string(),
                ));
            }
        }
        self.stream = Some(sig);
        self.last_time_ns = Some(midpoint);

        let gray = match image_to_gray(&frame.image) {
            Ok(g) => g,
            Err(err) => {
                return Ok(Candidate2DBatch {
                    main_frame_ref: frame_ref(frame),
                    capture_time: frame.capture_time.clone(),
                    processing_status: ProcessingStatus::Failed,
                    candidates: Vec::new(),
                    reason_codes: vec!["invalid_main_frame".to_string(), err.to_string()],
                });
            }
        };

        let w = frame.image.width_px as usize;
        let h = frame.image.height_px as usize;
        let model = self.model.get_or_insert_with(|| {
            Mog2Model::new(
                w,
                h,
                self.policy.background_history_frame_count.unwrap_or(500) as usize,
                self.policy.background_variance_threshold.unwrap_or(16.0),
                self.policy.background_detect_shadows.unwrap_or(false),
            )
        });
        let rate = self.policy.background_learning_rate.unwrap_or(0.0);
        let mask = model.apply(&gray, rate);
        let fg_mask: Vec<u8> = mask
            .into_iter()
            .map(|v| if v == 255 { 255 } else { 0 })
            .collect();
        let components = connected_components_8(&fg_mask, w, h);

        self.frame_count += 1;
        let warmup = self.policy.background_warmup_frame_count.unwrap_or(0);
        let mut candidates = Vec::new();
        if self.frame_count > warmup {
            for comp in components {
                let area = comp.area as f64;
                if area < self.policy.minimum_component_area_px2 {
                    continue;
                }
                if let Some(max_area) = self.policy.maximum_component_area_px2 {
                    if area > max_area {
                        continue;
                    }
                }
                candidates.push(Candidate2D {
                    centroid_px: [comp.centroid_x, comp.centroid_y],
                    detection_score: area / ((comp.width * comp.height) as f64),
                });
            }
        }
        candidates.sort_by(|a, b| {
            a.centroid_px[0]
                .total_cmp(&b.centroid_px[0])
                .then_with(|| a.centroid_px[1].total_cmp(&b.centroid_px[1]))
        });

        let reason_codes = if self.frame_count <= warmup {
            vec!["background_warmup".to_string()]
        } else if candidates.is_empty() {
            vec!["no_foreground_candidates".to_string()]
        } else {
            Vec::new()
        };

        Ok(Candidate2DBatch {
            main_frame_ref: frame_ref(frame),
            capture_time: frame.capture_time.clone(),
            processing_status: ProcessingStatus::Processed,
            candidates,
            reason_codes,
        })
    }
}

pub struct WorkbenchDetector {
    policy: FrameDifferencePolicy,
    stream: Option<StreamSignature>,
    last_time_ns: Option<i64>,
    mog2: Option<Mog2Model>,
    prev_frames: Vec<Vec<u8>>,
    occupancy_map: Vec<f32>,
    morph_offsets: Vec<(isize, isize)>,
    occ_offsets: Vec<(isize, isize)>,
    blob_offsets: Vec<(isize, isize)>,
}

impl WorkbenchDetector {
    pub fn new(policy: FrameDifferencePolicy) -> Self {
        let morph_offsets = elliptical_offsets(policy.morphology_kernel_size as usize);
        let occ_offsets = if policy.occupancy_dilation_size > 0 {
            elliptical_offsets(policy.occupancy_dilation_size as usize)
        } else {
            Vec::new()
        };
        let blob_offsets = if policy.blob_dilation_size > 0 {
            elliptical_offsets(policy.blob_dilation_size as usize)
        } else {
            Vec::new()
        };
        Self {
            policy,
            stream: None,
            last_time_ns: None,
            mog2: None,
            prev_frames: Vec::new(),
            occupancy_map: Vec::new(),
            morph_offsets,
            occ_offsets,
            blob_offsets,
        }
    }

    pub fn detect(&mut self, frame: &CameraFrame) -> Result<Candidate2DBatch, DetectionError> {
        if frame.camera_role != CameraRole::Main
            || frame.station_id.is_empty()
            || frame.station_session_id.is_empty()
            || frame.frame_id.is_empty()
            || frame.camera_config_ref.is_empty()
        {
            return Err(DetectionError::InvalidInput(
                "valid main frame identity required".to_string(),
            ));
        }
        let time_ns = capture_midpoint_ns(&frame.capture_time)?;
        let sig = StreamSignature::from_frame(frame);
        if let Some(prev) = &self.stream {
            if prev != &sig {
                return Err(DetectionError::InvalidInput(
                    "main stream mismatch".to_string(),
                ));
            }
        }
        if let Some(prev_t) = self.last_time_ns {
            if time_ns <= prev_t {
                return Err(DetectionError::InvalidInput(
                    "main exposure must increase strictly".to_string(),
                ));
            }
        }
        self.stream = Some(sig);
        self.last_time_ns = Some(time_ns);

        let full_gray = match image_to_gray(&frame.image) {
            Ok(g) => g,
            Err(_) => {
                return Ok(Candidate2DBatch {
                    main_frame_ref: frame_ref(frame),
                    capture_time: frame.capture_time.clone(),
                    processing_status: ProcessingStatus::Failed,
                    candidates: Vec::new(),
                    reason_codes: vec!["invalid_main_frame".to_string()],
                });
            }
        };

        let factor = self.policy.downsample_factor as usize;
        let (gray, w, h) = downsample_gray(
            &full_gray,
            frame.image.width_px as usize,
            frame.image.height_px as usize,
            factor,
        );

        let mut motion_mask = match self.policy.mode.as_str() {
            "mog2" => {
                let mog = self.mog2.get_or_insert_with(|| {
                    Mog2Model::new(
                        w,
                        h,
                        self.policy.mog2_history.unwrap_or(500) as usize,
                        self.policy.mog2_variance_threshold.unwrap_or(16.0),
                        self.policy.mog2_detect_shadows.unwrap_or(false),
                    )
                });
                let lr = self.policy.mog2_learning_rate.unwrap_or(-1.0);
                mog.apply(&gray, lr)
                    .into_iter()
                    .map(|v| if v == 255 { 255 } else { 0 })
                    .collect::<Vec<u8>>()
            }
            "two_frame" => {
                let m = if let Some(prev) = self.prev_frames.last() {
                    gray.iter()
                        .zip(prev.iter())
                        .map(|(&c, &p)| {
                            if (c as i16 - p as i16).abs() as f64 > self.policy.threshold {
                                255
                            } else {
                                0
                            }
                        })
                        .collect()
                } else {
                    vec![0u8; w * h]
                };
                self.prev_frames.clear();
                self.prev_frames.push(gray);
                m
            }
            "three_frame" => {
                self.prev_frames.push(gray);
                if self.prev_frames.len() > 3 {
                    self.prev_frames.remove(0);
                }
                if self.prev_frames.len() == 3 {
                    let f0 = &self.prev_frames[0];
                    let f1 = &self.prev_frames[1];
                    let f2 = &self.prev_frames[2];
                    (0..w * h)
                        .map(|i| {
                            let d1 = (f2[i] as i16 - f1[i] as i16).abs() as f64;
                            let d2 = (f1[i] as i16 - f0[i] as i16).abs() as f64;
                            if d1 > self.policy.threshold && d2 > self.policy.threshold {
                                255
                            } else {
                                0
                            }
                        })
                        .collect()
                } else {
                    vec![0u8; w * h]
                }
            }
            "five_frame" => {
                self.prev_frames.push(gray);
                if self.prev_frames.len() > 5 {
                    self.prev_frames.remove(0);
                }
                if self.prev_frames.len() == 5 {
                    let cur = &self.prev_frames[4];
                    (0..w * h)
                        .map(|i| {
                            let all = self.prev_frames[..4].iter().all(|p| {
                                ((cur[i] as i16 - p[i] as i16).abs() as f64) > self.policy.threshold
                            });
                            if all {
                                255
                            } else {
                                0
                            }
                        })
                        .collect()
                } else {
                    vec![0u8; w * h]
                }
            }
            _ => vec![0u8; w * h],
        };

        // Morphology
        if self.policy.apply_erosion {
            motion_mask = erode_binary(&motion_mask, w, h, &self.morph_offsets);
        }
        if self.policy.apply_opening {
            motion_mask = erode_binary(&motion_mask, w, h, &self.morph_offsets);
            motion_mask = dilate_binary(&motion_mask, w, h, &self.morph_offsets);
        }
        if self.policy.apply_closing {
            motion_mask = dilate_binary(&motion_mask, w, h, &self.morph_offsets);
            motion_mask = erode_binary(&motion_mask, w, h, &self.morph_offsets);
        }

        // Occupancy filter
        let filtered_mask = if self.policy.apply_occupancy {
            if self.occupancy_map.len() != w * h {
                self.occupancy_map = vec![0.0_f32; w * h];
            }
            let inc = self.policy.score_increment as f32;
            let dec = self.policy.score_decay as f32;
            let thresh = self.policy.mask_threshold as f32;
            let mut bg_mask = vec![0u8; w * h];
            for i in 0..w * h {
                if motion_mask[i] != 0 {
                    self.occupancy_map[i] += inc;
                } else {
                    self.occupancy_map[i] = (self.occupancy_map[i] - dec).max(0.0);
                }
                if self.occupancy_map[i] >= thresh {
                    bg_mask[i] = 255;
                }
            }
            let dilated_bg = if !self.occ_offsets.is_empty() {
                dilate_binary(&bg_mask, w, h, &self.occ_offsets)
            } else {
                bg_mask
            };
            motion_mask
                .iter()
                .zip(dilated_bg.iter())
                .map(|(&m, &b)| if m != 0 && b == 0 { 255 } else { 0 })
                .collect()
        } else {
            motion_mask
        };

        // Blob extraction
        let working_mask = if !self.blob_offsets.is_empty() {
            dilate_binary(&filtered_mask, w, h, &self.blob_offsets)
        } else {
            filtered_mask
        };
        let components = connected_components_8(&working_mask, w, h);
        let scale = self.policy.downsample_factor as f64;
        let mut candidates = Vec::new();
        for comp in components {
            let area = comp.area as f64;
            if area < self.policy.minimum_component_area_px2 {
                continue;
            }
            if let Some(max_area) = self.policy.maximum_component_area_px2 {
                if area > max_area {
                    continue;
                }
            }
            candidates.push(Candidate2D {
                centroid_px: [comp.centroid_x * scale, comp.centroid_y * scale],
                detection_score: (area / ((comp.width * comp.height) as f64)).min(1.0),
            });
        }
        candidates.sort_by(|a, b| {
            a.centroid_px[0]
                .total_cmp(&b.centroid_px[0])
                .then_with(|| a.centroid_px[1].total_cmp(&b.centroid_px[1]))
        });

        let reason_codes = if candidates.is_empty() {
            vec!["no_foreground_candidates".to_string()]
        } else {
            Vec::new()
        };
        Ok(Candidate2DBatch {
            main_frame_ref: frame_ref(frame),
            capture_time: frame.capture_time.clone(),
            processing_status: ProcessingStatus::Processed,
            candidates,
            reason_codes,
        })
    }
}

pub enum Tracker2DImpl {
    Standard(NearestNeighborTracker2D),
    Workbench(KalmanTracker2D),
}

impl Tracker2DImpl {
    pub fn update(&mut self, batch: &Candidate2DBatch) -> Result<Tracking2DResult, DetectionError> {
        match self {
            Self::Standard(trk) => trk.update(batch),
            Self::Workbench(trk) => trk.update(batch),
        }
    }
}

#[derive(Debug, Clone)]
struct NnTrack {
    local_id: String,
    centroid: [f64; 2],
    time_ns: i64,
    velocity: Option<[f64; 2]>,
    consecutive_hits: i64,
    confirmed: bool,
}

impl NnTrack {
    fn predict(&self, time_ns: i64) -> [f64; 2] {
        let Some(vel) = self.velocity else {
            return self.centroid;
        };
        let dt = ((time_ns - self.time_ns) as f64) / 1e9;
        [
            self.centroid[0] + vel[0] * dt,
            self.centroid[1] + vel[1] * dt,
        ]
    }
}

pub struct NearestNeighborTracker2D {
    station_id: String,
    station_session_id: String,
    policy: Tracking2DPolicy,
    tracks: BTreeMap<String, NnTrack>,
    next_id: u64,
    clock: Option<String>,
    last_time_ns: Option<i64>,
}

impl NearestNeighborTracker2D {
    pub fn new(station_id: String, station_session_id: String, policy: Tracking2DPolicy) -> Self {
        Self {
            station_id,
            station_session_id,
            policy,
            tracks: BTreeMap::new(),
            next_id: 1,
            clock: None,
            last_time_ns: None,
        }
    }

    pub fn update(&mut self, batch: &Candidate2DBatch) -> Result<Tracking2DResult, DetectionError> {
        let r = &batch.main_frame_ref;
        if r.station_id != self.station_id
            || r.station_session_id != self.station_session_id
            || r.camera_role != CameraRole::Main as i32
            || r.frame_id.is_empty()
        {
            return Err(DetectionError::InvalidInput(
                "local tracking station/session/main frame mismatch".to_string(),
            ));
        }
        let midpoint = capture_midpoint_ns(&batch.capture_time)?;
        if let Some(c) = &self.clock {
            if c != &batch.capture_time.clock_domain_id {
                return Err(DetectionError::InvalidInput(
                    "local tracking clock mismatch".to_string(),
                ));
            }
        }
        if let Some(prev) = self.last_time_ns {
            if midpoint <= prev {
                return Err(DetectionError::InvalidInput(
                    "local tracking exposure must increase strictly".to_string(),
                ));
            }
        }
        self.clock = Some(batch.capture_time.clock_domain_id.clone());
        self.last_time_ns = Some(midpoint);

        // Expire stale tracks first
        let max_missing = self.policy.maximum_missing_duration_ns;
        self.tracks
            .retain(|_, trk| midpoint - trk.time_ns <= max_missing);

        let mut pairs: Vec<(f64, String, usize)> = Vec::new();
        for (id, trk) in &self.tracks {
            let pred = trk.predict(midpoint);
            for (cand_idx, cand) in batch.candidates.iter().enumerate() {
                let dist = (cand.centroid_px[0] - pred[0]).hypot(cand.centroid_px[1] - pred[1]);
                if dist <= self.policy.association_gate_px {
                    pairs.push((dist, id.clone(), cand_idx));
                }
            }
        }
        pairs.sort_by(|a, b| {
            a.0.total_cmp(&b.0)
                .then_with(|| a.1.cmp(&b.1))
                .then_with(|| a.2.cmp(&b.2))
        });

        let mut matched_tracks: HashMap<String, usize> = HashMap::new();
        let mut matched_cands: HashMap<usize, String> = HashMap::new();
        for (_, id, cand_idx) in pairs {
            if matched_tracks.contains_key(&id) || matched_cands.contains_key(&cand_idx) {
                continue;
            }
            matched_tracks.insert(id.clone(), cand_idx);
            matched_cands.insert(cand_idx, id);
        }

        // Unmatched tracks keep coasting with streak reset to 0
        for (id, trk) in &mut self.tracks {
            if !matched_tracks.contains_key(id) {
                trk.consecutive_hits = 0;
            }
        }

        let confirm_n = self.policy.confirmation_frame_count;
        let mut observations = Vec::new();
        let quality_def = self
            .policy
            .quality_definition
            .to_proto()
            .expect("validated quality_definition");
        let score_def = fill_ratio_score()
            .to_proto()
            .expect("validated score_definition");

        for (cand_idx, cand) in batch.candidates.iter().enumerate() {
            let local_id = if let Some(id) = matched_cands.get(&cand_idx) {
                let trk = self.tracks.get_mut(id).expect("matched track exists");
                let dt = ((midpoint - trk.time_ns) as f64) / 1e9;
                if dt > 0.0 {
                    trk.velocity = Some([
                        (cand.centroid_px[0] - trk.centroid[0]) / dt,
                        (cand.centroid_px[1] - trk.centroid[1]) / dt,
                    ]);
                }
                trk.centroid = cand.centroid_px;
                trk.time_ns = midpoint;
                trk.consecutive_hits += 1;
                if trk.consecutive_hits >= confirm_n {
                    trk.confirmed = true;
                }
                id.clone()
            } else {
                let id = format!("local-{}", self.next_id);
                self.next_id += 1;
                self.tracks.insert(
                    id.clone(),
                    NnTrack {
                        local_id: id.clone(),
                        centroid: cand.centroid_px,
                        time_ns: midpoint,
                        velocity: None,
                        consecutive_hits: 1,
                        confirmed: confirm_n <= 1,
                    },
                );
                id
            };

            let trk = &self.tracks[&local_id];
            let quality = ((trk.consecutive_hits as f64) / (confirm_n as f64)).min(1.0);
            observations.push(Observation2D {
                main_frame_ref: Some(batch.main_frame_ref.clone()),
                capture_time: Some(batch.capture_time.clone()),
                centroid_px: Some(Vec2 {
                    x: cand.centroid_px[0],
                    y: cand.centroid_px[1],
                }),
                centroid_covariance_px2: None,
                detection_score: cand.detection_score,
                detection_score_definition: Some(score_def.clone()),
                local_track_id: Some(local_id),
                track_quality: Some(quality),
                track_quality_definition: Some(quality_def.clone()),
            });
        }

        let tracks = self
            .tracks
            .values()
            .map(|trk| {
                let quality = ((trk.consecutive_hits as f64) / (confirm_n as f64)).min(1.0);
                let lifecycle = if trk.consecutive_hits == 0 {
                    "coasting"
                } else if trk.confirmed {
                    "confirmed"
                } else {
                    "tentative"
                };
                Track2DState {
                    local_track_id: trk.local_id.clone(),
                    centroid_px: trk.centroid,
                    velocity_pxps: trk.velocity,
                    lifecycle: lifecycle.to_string(),
                    track_quality: quality,
                }
            })
            .collect();

        Ok(Tracking2DResult {
            observations,
            tracks,
        })
    }
}

#[derive(Debug, Clone)]
struct Kalman2D {
    state: [f64; 4],
    cov: [[f64; 4]; 4],
    q: [f64; 4],
    r_var: f64,
    cov_floor_sq: f64,
}

impl Kalman2D {
    fn new(pos: [f64; 2], policy: &KalmanTrackingPolicy) -> Self {
        let mut cov = [[0.0; 4]; 4];
        for (i, row) in cov.iter_mut().enumerate() {
            row[i] = 100.0;
        }
        Self {
            state: [pos[0], pos[1], 0.0, 0.0],
            cov,
            q: [
                policy.process_noise_position,
                policy.process_noise_position,
                policy.process_noise_velocity,
                policy.process_noise_velocity,
            ],
            r_var: policy.measurement_noise_px * policy.measurement_noise_px,
            cov_floor_sq: policy.covariance_floor_px * policy.covariance_floor_px,
        }
    }

    fn predict(&mut self, dt: f64) -> [f64; 2] {
        let f = [
            [1.0, 0.0, dt, 0.0],
            [0.0, 1.0, 0.0, dt],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ];
        let mut next_state = [0.0; 4];
        for i in 0..4 {
            next_state[i] = f[i].iter().zip(self.state.iter()).map(|(a, b)| a * b).sum();
        }
        self.state = next_state;

        let mut fp = [[0.0; 4]; 4];
        for (i, fp_row) in fp.iter_mut().enumerate() {
            for (j, cell) in fp_row.iter_mut().enumerate() {
                *cell = (0..4).map(|k| f[i][k] * self.cov[k][j]).sum();
            }
        }
        let mut next_cov = [[0.0; 4]; 4];
        for (i, cov_row) in next_cov.iter_mut().enumerate() {
            for (j, cell) in cov_row.iter_mut().enumerate() {
                *cell = (0..4).map(|k| fp[i][k] * f[j][k]).sum();
            }
            cov_row[i] = (cov_row[i] + self.q[i]).max(self.cov_floor_sq);
        }
        self.cov = next_cov;
        [self.state[0], self.state[1]]
    }

    fn update(&mut self, meas: [f64; 2]) {
        let y = [meas[0] - self.state[0], meas[1] - self.state[1]];
        let s: Mat2 = [
            [self.cov[0][0] + self.r_var, self.cov[0][1]],
            [self.cov[1][0], self.cov[1][1] + self.r_var],
        ];
        let Ok(s_inv) = invert_2x2(&s) else {
            return;
        };
        let mut k = [[0.0; 2]; 4];
        for (i, k_row) in k.iter_mut().enumerate() {
            k_row[0] = self.cov[i][0] * s_inv[0][0] + self.cov[i][1] * s_inv[1][0];
            k_row[1] = self.cov[i][0] * s_inv[0][1] + self.cov[i][1] * s_inv[1][1];
        }
        for (state_val, k_row) in self.state.iter_mut().zip(k.iter()) {
            *state_val += k_row[0] * y[0] + k_row[1] * y[1];
        }
        let mut new_cov = [[0.0; 4]; 4];
        for (i, new_row) in new_cov.iter_mut().enumerate() {
            for (j, cell) in new_row.iter_mut().enumerate() {
                let mut val = self.cov[i][j];
                val -= k[i][0] * self.cov[0][j] + k[i][1] * self.cov[1][j];
                *cell = val;
            }
        }
        self.cov = new_cov;
    }
}

#[derive(Debug, Clone)]
struct WorkbenchTrack {
    track_id: u64,
    kalman: Kalman2D,
    lifecycle: String,
    consecutive_hits: i64,
    frames_since_update: i64,
    last_measurement: Option<([f64; 2], f64)>,
}

pub struct KalmanTracker2D {
    station_id: String,
    station_session_id: String,
    policy: KalmanTrackingPolicy,
    image_scale: f64,
    tracks: Vec<WorkbenchTrack>,
    next_id: u64,
    last_time_ns: Option<i64>,
}

impl KalmanTracker2D {
    pub fn new(
        station_id: String,
        station_session_id: String,
        policy: KalmanTrackingPolicy,
        image_scale: f64,
    ) -> Self {
        Self {
            station_id,
            station_session_id,
            policy,
            image_scale,
            tracks: Vec::new(),
            next_id: 1,
            last_time_ns: None,
        }
    }

    pub fn update(&mut self, batch: &Candidate2DBatch) -> Result<Tracking2DResult, DetectionError> {
        let r = &batch.main_frame_ref;
        if r.station_id != self.station_id
            || r.station_session_id != self.station_session_id
            || r.camera_role != CameraRole::Main as i32
        {
            return Err(DetectionError::InvalidInput(
                "tracker station/session/main frame mismatch".to_string(),
            ));
        }
        let midpoint = capture_midpoint_ns(&batch.capture_time)?;
        if let Some(prev) = self.last_time_ns {
            if midpoint <= prev {
                return Err(DetectionError::InvalidInput(
                    "Tracking timestamps must increase strictly".to_string(),
                ));
            }
        }
        let dt = self
            .last_time_ns
            .map(|prev| ((midpoint - prev) as f64) / 1e9)
            .unwrap_or(1.0 / 30.0);
        self.last_time_ns = Some(midpoint);

        for trk in &mut self.tracks {
            trk.last_measurement = None;
            trk.kalman.predict(dt);
            trk.frames_since_update += 1;
        }

        let mut pairs: Vec<(f64, usize, usize)> = Vec::new();
        for (ti, trk) in self.tracks.iter().enumerate() {
            let pred = [trk.kalman.state[0], trk.kalman.state[1]];
            for (bi, cand) in batch.candidates.iter().enumerate() {
                let bx = cand.centroid_px[0] / self.image_scale;
                let by = cand.centroid_px[1] / self.image_scale;
                let dist = (bx - pred[0]).hypot(by - pred[1]);
                if dist <= self.policy.association_gate_px {
                    pairs.push((dist, ti, bi));
                }
            }
        }
        pairs.sort_by(|a, b| a.0.total_cmp(&b.0));

        let mut matched_t = vec![false; self.tracks.len()];
        let mut matched_b = vec![false; batch.candidates.len()];
        for (_, ti, bi) in pairs {
            if matched_t[ti] || matched_b[bi] {
                continue;
            }
            matched_t[ti] = true;
            matched_b[bi] = true;
            let cand = &batch.candidates[bi];
            let mx = cand.centroid_px[0] / self.image_scale;
            let my = cand.centroid_px[1] / self.image_scale;
            let trk = &mut self.tracks[ti];
            trk.kalman.update([mx, my]);
            trk.last_measurement = Some(([mx, my], cand.detection_score));
            trk.frames_since_update = 0;
            trk.consecutive_hits += 1;
            if (trk.lifecycle == "tentative"
                && trk.consecutive_hits >= self.policy.confirmation_frame_count)
                || trk.lifecycle == "coasting"
            {
                trk.lifecycle = "confirmed".to_string();
            }
        }

        for (ti, trk) in self.tracks.iter_mut().enumerate() {
            if !matched_t[ti] {
                trk.consecutive_hits = 0;
                if trk.lifecycle == "confirmed" {
                    trk.lifecycle = "coasting".to_string();
                }
            }
        }

        let max_lost = self.policy.maximum_lost_frame_count;
        self.tracks
            .retain(|trk| trk.frames_since_update <= max_lost);

        for (bi, cand) in batch.candidates.iter().enumerate() {
            if matched_b[bi] {
                continue;
            }
            let id = self.next_id;
            self.next_id += 1;
            let mx = cand.centroid_px[0] / self.image_scale;
            let my = cand.centroid_px[1] / self.image_scale;
            self.tracks.push(WorkbenchTrack {
                track_id: id,
                kalman: Kalman2D::new([mx, my], &self.policy),
                lifecycle: if self.policy.confirmation_frame_count <= 1 {
                    "confirmed".to_string()
                } else {
                    "tentative".to_string()
                },
                consecutive_hits: 1,
                frames_since_update: 0,
                last_measurement: Some(([mx, my], cand.detection_score)),
            });
        }

        let score_def = fill_ratio_score()
            .to_proto()
            .expect("validated score_definition");
        let mut observations = Vec::new();
        for trk in &self.tracks {
            if trk.lifecycle == "confirmed" {
                if let Some(([mx, my], score)) = trk.last_measurement {
                    observations.push(Observation2D {
                        main_frame_ref: Some(batch.main_frame_ref.clone()),
                        capture_time: Some(batch.capture_time.clone()),
                        centroid_px: Some(Vec2 {
                            x: mx * self.image_scale,
                            y: my * self.image_scale,
                        }),
                        centroid_covariance_px2: None,
                        detection_score: score,
                        detection_score_definition: Some(score_def.clone()),
                        local_track_id: Some(trk.track_id.to_string()),
                        track_quality: None,
                        track_quality_definition: None,
                    });
                }
            }
        }

        let confirm_n = self.policy.confirmation_frame_count as f64;
        let tracks = self
            .tracks
            .iter()
            .map(|trk| Track2DState {
                local_track_id: trk.track_id.to_string(),
                centroid_px: [
                    trk.kalman.state[0] * self.image_scale,
                    trk.kalman.state[1] * self.image_scale,
                ],
                velocity_pxps: Some([
                    trk.kalman.state[2] * self.image_scale,
                    trk.kalman.state[3] * self.image_scale,
                ]),
                lifecycle: trk.lifecycle.clone(),
                track_quality: ((trk.consecutive_hits as f64) / confirm_n).min(1.0),
            })
            .collect();

        Ok(Tracking2DResult {
            observations,
            tracks,
        })
    }
}

pub struct MainDetectionModule {
    detector: MotionDetectorImpl,
    tracker: Tracker2DImpl,
}

impl MainDetectionModule {
    pub fn new(
        station_id: String,
        station_session_id: String,
        det_policy: DetectionPolicyConfig,
        trk_policy: TrackingPolicyConfig,
    ) -> Result<Self, DetectionError> {
        let (detector, tracker) = match (det_policy, trk_policy) {
            (DetectionPolicyConfig::Standard(dp), TrackingPolicyConfig::Standard(tp)) => (
                MotionDetectorImpl::Standard(StandardMog2Detector::new(dp)),
                Tracker2DImpl::Standard(NearestNeighborTracker2D::new(
                    station_id,
                    station_session_id,
                    tp,
                )),
            ),
            (DetectionPolicyConfig::Workbench(dp), TrackingPolicyConfig::Workbench(tp)) => {
                let scale = dp.downsample_factor as f64;
                (
                    MotionDetectorImpl::Workbench(WorkbenchDetector::new(dp)),
                    Tracker2DImpl::Workbench(KalmanTracker2D::new(
                        station_id,
                        station_session_id,
                        tp,
                        scale,
                    )),
                )
            }
            _ => {
                return Err(DetectionError::InvalidInput(
                    "detector and tracker algorithm families must agree".to_string(),
                ));
            }
        };
        Ok(Self { detector, tracker })
    }

    pub fn process(&mut self, frame: &CameraFrame) -> Result<MainDetectionResult, DetectionError> {
        let batch = self.detector.detect(frame)?;
        let tracking = self.tracker.update(&batch)?;
        Ok(MainDetectionResult {
            main_frame_ref: batch.main_frame_ref,
            capture_time: batch.capture_time,
            processing_status: batch.processing_status,
            observations: tracking.observations,
            reason_codes: batch.reason_codes,
            tracks: tracking.tracks,
        })
    }
}
