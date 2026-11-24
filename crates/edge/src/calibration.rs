//! Pure-Rust auxiliary checkerboard recognition, pose estimation, and calibration state machine.

use std::collections::{BTreeMap, HashMap, HashSet};

use crate::detection::{capture_midpoint_ns, frame_ref, image_to_gray};
use crate::frame_source::CameraFrame;
use crate::math::{
    add3, cross3, mat3_from_slice, mat3_mul, mat3_transpose, mat3_vec, norm3,
    normalize_intrinsic_matrix, project_point, rodrigues_vec_to_mat, rotation_angle, scale3,
    solve_least_squares_3, sub3, DistortionCoeffs, Mat3, Vec3,
};
use crate::policy::CalibrationPolicy;
use crate::proto::{
    CalibrationHealth, CalibrationQuality, CalibrationSolution, CalibrationSolutionRef,
    CalibrationState, CalibrationStateEvent, CalibrationUsage, CameraModel, CameraRole,
    CaptureTime, DiagnosticEvent, EventEnvelope, EventType, FrameRef, Mat3 as ProtoMat3,
    ProcessingStatus, ProcessingTime, StationConfig, SurveyedReference, TimeInterval,
    Vec3 as ProtoVec3,
};

const EPS: f64 = 1e-10;
const DEFAULT_MAX_CORRECTION_RAD: f64 = std::f64::consts::PI / 180.0; // 1.0 deg
type IndexedPoints = (Vec<[f64; 2]>, Vec<(usize, usize)>);

#[derive(Debug, Clone, Copy)]
pub struct CheckerboardGeometry {
    pub columns: usize,
    pub rows: usize,
    pub board_width_m: f64,
    pub board_height_m: f64,
    pub square_pitch_x_m: f64,
    pub square_pitch_y_m: f64,
}

impl Default for CheckerboardGeometry {
    fn default() -> Self {
        Self {
            columns: 6,
            rows: 4,
            board_width_m: 0.297,
            board_height_m: 0.210,
            square_pitch_x_m: 0.035,
            square_pitch_y_m: 0.035,
        }
    }
}

impl CheckerboardGeometry {
    pub fn expected_count(&self) -> usize {
        self.columns * self.rows
    }
}

#[derive(Debug, Clone)]
pub struct ReferenceObservation {
    pub reference_id: String,
    pub centroid_px: [f64; 2],
}

#[derive(Debug, Clone)]
pub struct ReferenceObservationBatch {
    pub auxiliary_frame_ref: FrameRef,
    pub capture_time: CaptureTime,
    pub processing_status: ProcessingStatus,
    pub observations: Vec<ReferenceObservation>,
    pub reason_codes: Vec<String>,
}

fn sample_gray_bilinear(gray: &[u8], w: usize, h: usize, x: f64, y: f64) -> f64 {
    let x0 = (x.floor() as isize).clamp(0, w as isize - 1) as usize;
    let y0 = (y.floor() as isize).clamp(0, h as isize - 1) as usize;
    let x1 = (x0 + 1).min(w - 1);
    let y1 = (y0 + 1).min(h - 1);
    let fx = (x - x0 as f64).clamp(0.0, 1.0);
    let fy = (y - y0 as f64).clamp(0.0, 1.0);
    let v00 = gray[y0 * w + x0] as f64;
    let v10 = gray[y0 * w + x1] as f64;
    let v01 = gray[y1 * w + x0] as f64;
    let v11 = gray[y1 * w + x1] as f64;
    (1.0 - fx) * (1.0 - fy) * v00 + fx * (1.0 - fy) * v10 + (1.0 - fx) * fy * v01 + fx * fy * v11
}

/// Refine a checkerboard X-junction corner using iterative gradient-orthogonality (`cornerSubPix` parity).
fn refine_corner_subpix(gray: &[u8], w: usize, h: usize, init: [f64; 2]) -> [f64; 2] {
    let mut cx = init[0];
    let mut cy = init[1];
    let win = 4isize;
    for _ in 0..30 {
        let ix0 = cx.round() as isize;
        let iy0 = cy.round() as isize;
        if ix0 - win - 1 < 0
            || ix0 + win + 1 >= w as isize
            || iy0 - win - 1 < 0
            || iy0 + win + 1 >= h as isize
        {
            break;
        }
        let mut a00 = 0.0_f64;
        let mut a01 = 0.0_f64;
        let mut a11 = 0.0_f64;
        let mut b0 = 0.0_f64;
        let mut b1 = 0.0_f64;
        for dy in -win..=win {
            for dx in -win..=win {
                let px = (ix0 + dx) as usize;
                let py = (iy0 + dy) as usize;
                let gx = 0.5 * (gray[py * w + px + 1] as f64 - gray[py * w + px - 1] as f64);
                let gy = 0.5 * (gray[(py + 1) * w + px] as f64 - gray[(py - 1) * w + px] as f64);
                let wgt = (-((dx * dx + dy * dy) as f64) / (2.0 * 2.5 * 2.5)).exp();
                let gxx = gx * gx * wgt;
                let gxy = gx * gy * wgt;
                let gyy = gy * gy * wgt;
                a00 += gxx;
                a01 += gxy;
                a11 += gyy;
                let fx = px as f64;
                let fy = py as f64;
                b0 += gxx * fx + gxy * fy;
                b1 += gxy * fx + gyy * fy;
            }
        }
        let det = a00 * a11 - a01 * a01;
        if det.abs() <= 1e-9 {
            break;
        }
        let nx = (a11 * b0 - a01 * b1) / det;
        let ny = (a00 * b1 - a01 * b0) / det;
        if !nx.is_finite() || !ny.is_finite() || (nx - init[0]).hypot(ny - init[1]) > 4.0 {
            break;
        }
        let step = (nx - cx).hypot(ny - cy);
        cx = nx;
        cy = ny;
        if step < 0.01 {
            break;
        }
    }
    [cx, cy]
}

fn chess_corner_candidates(gray: &[u8], w: usize, h: usize, radius: f64) -> Vec<([f64; 2], f64)> {
    let margin = (radius.ceil() as usize) + 2;
    if w <= margin * 2 || h <= margin * 2 {
        return Vec::new();
    }
    let mut ring_offsets = [(0.0_f64, 0.0_f64); 16];
    for (k, slot) in ring_offsets.iter_mut().enumerate() {
        let angle = (k as f64) * std::f64::consts::PI / 8.0;
        *slot = (radius * angle.cos(), radius * angle.sin());
    }

    let mut resp = vec![0.0_f64; w * h];
    let mut max_resp = 0.0_f64;
    for y in margin..(h - margin) {
        for x in margin..(w - margin) {
            let mut p = [0.0_f64; 16];
            let mut ring_sum = 0.0;
            for (k, &(ox, oy)) in ring_offsets.iter().enumerate() {
                let v = sample_gray_bilinear(gray, w, h, x as f64 + ox, y as f64 + oy);
                p[k] = v;
                ring_sum += v;
            }
            let ring_mean = ring_sum / 16.0;
            let mut local_sum = 0.0;
            for dy in -1isize..=1 {
                for dx in -1isize..=1 {
                    local_sum += gray
                        [((y as isize + dy) as usize) * w + ((x as isize + dx) as usize)]
                        as f64;
                }
            }
            let local_mean = local_sum / 9.0;
            let mean_diff = (ring_mean - local_mean).abs();

            let mut best_k = f64::NEG_INFINITY;
            for k in 0..4 {
                let q0 = 0.5 * (p[(k + 1) % 16] + p[(k + 2) % 16]);
                let q1 = 0.5 * (p[(k + 5) % 16] + p[(k + 6) % 16]);
                let q2 = 0.5 * (p[(k + 9) % 16] + p[(k + 10) % 16]);
                let q3 = 0.5 * (p[(k + 13) % 16] + p[(k + 14) % 16]);
                let sr = ((q0 + q2) - (q1 + q3)).abs();
                let dr = (q0 - q2).abs() + (q1 - q3).abs();
                let score = sr - dr - mean_diff;
                if score > best_k {
                    best_k = score;
                }
            }
            if best_k > 25.0 {
                resp[y * w + x] = best_k;
                if best_k > max_resp {
                    max_resp = best_k;
                }
            }
        }
    }
    if max_resp <= 25.0 {
        return Vec::new();
    }

    let min_thresh = (max_resp * 0.25).max(25.0);
    let nms_r = (radius.round() as usize).max(3);
    let mut raw_peaks = Vec::new();
    for y in margin..(h - margin) {
        for x in margin..(w - margin) {
            let val = resp[y * w + x];
            if val >= min_thresh {
                raw_peaks.push((x, y, val));
            }
        }
    }
    raw_peaks.sort_by(|a, b| b.2.total_cmp(&a.2));

    let min_dist_sq = ((nms_r * nms_r) as f64).max(9.0);
    let mut peaks: Vec<([f64; 2], f64)> = Vec::new();
    for (x, y, val) in raw_peaks {
        let fx = x as f64;
        let fy = y as f64;
        if peaks
            .iter()
            .any(|(p, _)| (p[0] - fx).powi(2) + (p[1] - fy).powi(2) < min_dist_sq)
        {
            continue;
        }
        let refined = refine_corner_subpix(gray, w, h, [fx, fy]);
        if peaks
            .iter()
            .any(|(p, _)| (p[0] - refined[0]).powi(2) + (p[1] - refined[1]).powi(2) < min_dist_sq)
        {
            continue;
        }
        peaks.push((refined, val));
        if peaks.len() >= 64 {
            break;
        }
    }
    peaks
}

fn find_nearest_candidate(
    pts: &[[f64; 2]],
    target: [f64; 2],
    max_dist: f64,
    used: &HashSet<usize>,
) -> Option<usize> {
    let mut best: Option<(usize, f64)> = None;
    for (idx, &p) in pts.iter().enumerate() {
        if used.contains(&idx) {
            continue;
        }
        let d = (p[0] - target[0]).hypot(p[1] - target[1]);
        if d <= max_dist && best.is_none_or(|(_, bd)| d < bd) {
            best = Some((idx, d));
        }
    }
    best.map(|(i, _)| i)
}

fn trace_grid_from_seed(
    pts: &[[f64; 2]],
    seed_idx: usize,
    u0: [f64; 2],
    v0: [f64; 2],
    rows: usize,
    cols: usize,
) -> Option<(Vec<[f64; 2]>, f64)> {
    let step_scale = (u0[0].hypot(u0[1]) + v0[0].hypot(v0[1])) * 0.5;
    let snap_tol = (step_scale * 0.35).max(3.0);
    let mut grid = vec![vec![None::<usize>; cols]; rows];
    let mut used = HashSet::new();
    grid[0][0] = Some(seed_idx);
    used.insert(seed_idx);
    let mut total_err = 0.0;

    for r in 0..rows {
        for c in 0..cols {
            if r == 0 && c == 0 {
                continue;
            }
            let pred = if r == 0 {
                let left = pts[grid[0][c - 1]?];
                let du = if c >= 2 {
                    let prev = pts[grid[0][c - 2]?];
                    [left[0] - prev[0], left[1] - prev[1]]
                } else {
                    u0
                };
                [left[0] + du[0], left[1] + du[1]]
            } else if c == 0 {
                let up = pts[grid[r - 1][0]?];
                let dv = if r >= 2 {
                    let prev = pts[grid[r - 2][0]?];
                    [up[0] - prev[0], up[1] - prev[1]]
                } else {
                    v0
                };
                [up[0] + dv[0], up[1] + dv[1]]
            } else {
                let left = pts[grid[r][c - 1]?];
                let up = pts[grid[r - 1][c]?];
                let diag = pts[grid[r - 1][c - 1]?];
                [left[0] + up[0] - diag[0], left[1] + up[1] - diag[1]]
            };
            let idx = find_nearest_candidate(pts, pred, snap_tol, &used)?;
            let found = pts[idx];
            total_err += (found[0] - pred[0]).hypot(found[1] - pred[1]);
            grid[r][c] = Some(idx);
            used.insert(idx);
        }
    }

    let mut ordered = Vec::with_capacity(rows * cols);
    for row in grid {
        for cell in row {
            ordered.push(pts[cell?]);
        }
    }
    Some((ordered, total_err))
}

fn assemble_checkerboard_grid(pts: &[[f64; 2]], cols: usize, rows: usize) -> Option<Vec<[f64; 2]>> {
    if pts.len() < cols * rows {
        return None;
    }
    let mut best: Option<(Vec<[f64; 2]>, f64)> = None;
    for (seed_idx, &p0) in pts.iter().enumerate() {
        let mut neighbors: Vec<(usize, f64, [f64; 2])> = pts
            .iter()
            .enumerate()
            .filter(|&(j, _)| j != seed_idx)
            .map(|(j, &pj)| {
                let d = (pj[0] - p0[0]).hypot(pj[1] - p0[1]);
                (j, d, [pj[0] - p0[0], pj[1] - p0[1]])
            })
            .filter(|&(_, d, _)| d >= 5.0)
            .collect();
        neighbors.sort_by(|a, b| a.1.total_cmp(&b.1));
        neighbors.truncate(6);

        for a in 0..neighbors.len() {
            for b in (a + 1)..neighbors.len() {
                let (_, du, u) = neighbors[a];
                let (_, dv, v) = neighbors[b];
                let ratio = (du / dv).max(dv / du);
                if ratio > 2.2 {
                    continue;
                }
                let cos_ang = (u[0] * v[0] + u[1] * v[1]) / (du * dv);
                if cos_ang.abs() > 0.65 {
                    continue;
                }
                // Try both (u as col, v as row) and (v as col, u as row)
                for (col_v, row_v) in [(u, v), (v, u)] {
                    if let Some((ordered, err)) =
                        trace_grid_from_seed(pts, seed_idx, col_v, row_v, rows, cols)
                    {
                        if best.as_ref().is_none_or(|(_, be)| err < *be) {
                            best = Some((ordered, err));
                        }
                    }
                }
            }
        }
    }
    best.map(|(g, _)| g)
}

/// Detect `cols x rows` inner checkerboard corners in an auxiliary grayscale image.
pub fn detect_checkerboard_corners(
    gray: &[u8],
    w: usize,
    h: usize,
    cols: usize,
    rows: usize,
) -> Option<Vec<[f64; 2]>> {
    for radius in [5.0_f64, 3.5_f64, 7.0_f64] {
        let peaks = chess_corner_candidates(gray, w, h, radius);
        let pts: Vec<[f64; 2]> = peaks.into_iter().map(|(p, _)| p).collect();
        if let Some(grid) = assemble_checkerboard_grid(&pts, cols, rows) {
            return Some(grid);
        }
    }
    None
}

pub struct CheckerboardReferenceRecognizer {
    pub geometry: CheckerboardGeometry,
}

impl CheckerboardReferenceRecognizer {
    pub fn new(geometry: CheckerboardGeometry) -> Self {
        Self { geometry }
    }

    pub fn recognize(
        &self,
        frame: &CameraFrame,
        config: &StationConfig,
    ) -> ReferenceObservationBatch {
        let failed = |reason: &str| ReferenceObservationBatch {
            auxiliary_frame_ref: frame_ref(frame),
            capture_time: frame.capture_time.clone(),
            processing_status: ProcessingStatus::Processed,
            observations: Vec::new(),
            reason_codes: vec![reason.to_string()],
        };

        if frame.camera_role != CameraRole::Auxiliary {
            return failed("auxiliary_frame_required");
        }
        if frame.station_id != config.station_id {
            return failed("station_mismatch");
        }
        if frame.camera_config_ref != config.config_digest {
            return failed("camera_configuration_mismatch");
        }
        let Some(ref_anchor) = &config.reference_anchor else {
            return failed("checkerboard_requires_surveyed_reference");
        };
        let gray = match image_to_gray(&frame.image) {
            Ok(g) => g,
            Err(err) => {
                return ReferenceObservationBatch {
                    auxiliary_frame_ref: frame_ref(frame),
                    capture_time: frame.capture_time.clone(),
                    processing_status: ProcessingStatus::Failed,
                    observations: Vec::new(),
                    reason_codes: vec!["image_invalid".to_string(), err.to_string()],
                };
            }
        };

        let Some(corners) = detect_checkerboard_corners(
            &gray,
            frame.image.width_px as usize,
            frame.image.height_px as usize,
            self.geometry.columns,
            self.geometry.rows,
        ) else {
            return failed("checkerboard_not_found");
        };
        if corners.len() != self.geometry.expected_count() {
            return failed("checkerboard_not_found");
        }

        let mut observations = Vec::with_capacity(corners.len());
        let mut idx = 0usize;
        for r in 0..self.geometry.rows {
            for c in 0..self.geometry.columns {
                observations.push(ReferenceObservation {
                    reference_id: format!("{}:inner-r{r}-c{c}", ref_anchor.reference_id),
                    centroid_px: corners[idx],
                });
                idx += 1;
            }
        }
        ReferenceObservationBatch {
            auxiliary_frame_ref: frame_ref(frame),
            capture_time: frame.capture_time.clone(),
            processing_status: ProcessingStatus::Processed,
            observations,
            reason_codes: vec![
                "rust_chess_checkerboard_corners".to_string(),
                "subpixel_corners".to_string(),
            ],
        }
    }
}

fn parse_inner_index(reference_id: &str) -> Option<(usize, usize)> {
    let lower = reference_id.to_ascii_lowercase();
    let r_pos = lower.rfind("-r").or_else(|| lower.rfind("_r"))?;
    let rest = &lower[r_pos + 2..];
    let c_sep = rest.find("-c").or_else(|| rest.find("_c"))?;
    let row: usize = rest[..c_sep].parse().ok()?;
    let col_str = &rest[c_sep + 2..];
    let col_digits: String = col_str
        .chars()
        .take_while(|ch| ch.is_ascii_digit())
        .collect();
    let col: usize = col_digits.parse().ok()?;
    Some((row, col))
}

fn landmark_corners(reference: &SurveyedReference) -> Result<[Vec3; 4], String> {
    let mut indexed: BTreeMap<usize, Vec3> = BTreeMap::new();
    for lm in &reference.landmarks {
        let Some(p) = &lm.position_enu_m else {
            return Err("surveyed landmark missing position_enu_m".to_string());
        };
        let pt = [p.x, p.y, p.z];
        if pt.iter().any(|x| !x.is_finite()) {
            return Err("surveyed checkerboard corners must be finite ENU points".to_string());
        }
        if let Some(pos) = lm.landmark_id.rfind(|c: char| !c.is_ascii_digit()) {
            if let Ok(idx) = lm.landmark_id[pos + 1..].parse::<usize>() {
                indexed.insert(idx, pt);
            }
        }
    }
    let selected = if (0..4).all(|i| indexed.contains_key(&i)) {
        [indexed[&0], indexed[&1], indexed[&2], indexed[&3]]
    } else if reference.landmarks.len() == 4 {
        let mut arr = [[0.0; 3]; 4];
        for (i, lm) in reference.landmarks.iter().enumerate() {
            let p = lm.position_enu_m.as_ref().unwrap();
            arr[i] = [p.x, p.y, p.z];
        }
        arr
    } else {
        return Err("a surveyed checkerboard needs four ordered outer corners".to_string());
    };

    let [p00, p10, p11, p01] = selected;
    let first = sub3(p10, p00);
    let second = sub3(p01, p00);
    if norm3(cross3(first, second)) < EPS {
        return Err("surveyed checkerboard corners are geometrically degenerate".to_string());
    }
    let expected_p11 = sub3(add3(p10, p01), p00);
    if norm3(sub3(p11, expected_p11)) > 0.001 {
        return Err("surveyed checkerboard must be a planar parallelogram".to_string());
    }
    Ok(selected)
}

fn bilinear_corner(
    corners: &[Vec3; 4],
    row: usize,
    col: usize,
    geom: &CheckerboardGeometry,
) -> Vec3 {
    let [p00, p10, p11, p01] = *corners;
    let alpha = 0.5
        + ((col as f64) - (geom.columns as f64 - 1.0) * 0.5) * geom.square_pitch_x_m
            / geom.board_width_m;
    let beta = 0.5
        + ((row as f64) - (geom.rows as f64 - 1.0) * 0.5) * geom.square_pitch_y_m
            / geom.board_height_m;
    let w00 = (1.0 - alpha) * (1.0 - beta);
    let w10 = alpha * (1.0 - beta);
    let w11 = alpha * beta;
    let w01 = (1.0 - alpha) * beta;
    [
        w00 * p00[0] + w10 * p10[0] + w11 * p11[0] + w01 * p01[0],
        w00 * p00[1] + w10 * p10[1] + w11 * p11[1] + w01 * p01[1],
        w00 * p00[2] + w10 * p10[2] + w11 * p11[2] + w01 * p01[2],
    ]
}

fn parse_camera_model(
    model: &CameraModel,
    label: &str,
) -> Result<(Mat3, DistortionCoeffs), String> {
    let intr = model
        .intrinsics
        .as_ref()
        .ok_or_else(|| format!("{label} missing intrinsics"))?;
    let k_raw = intr
        .matrix_px
        .as_ref()
        .ok_or_else(|| format!("{label} missing matrix_px"))?;
    let k_mat = mat3_from_slice(&k_raw.values, label).map_err(|e| e.to_string())?;
    let k_norm = normalize_intrinsic_matrix(&k_mat).map_err(|e| e.to_string())?;
    let dist_pb = model
        .distortion
        .as_ref()
        .ok_or_else(|| format!("{label} missing distortion"))?;
    let dist = DistortionCoeffs::from_proto(&dist_pb.model, &dist_pb.coefficients)
        .map_err(|e| e.to_string())?;
    Ok((k_norm, dist))
}

#[derive(Debug, Clone)]
pub enum CalibrationAttemptStatus {
    Estimated,
    ReferenceUnavailable,
    GeometryInvalid,
    Failed,
}

#[derive(Debug, Clone)]
pub struct CalibrationEstimate {
    pub reference_capture_time: CaptureTime,
    pub rotation_enu_from_main: Mat3,
    pub correction_covariance_enu_rad2: Mat3,
    pub quality: CalibrationQuality,
}

#[derive(Debug, Clone)]
pub struct CalibrationAttempt {
    pub status: CalibrationAttemptStatus,
    pub estimate: Option<CalibrationEstimate>,
    pub reference_frame_ref: FrameRef,
    pub reference_capture_time: CaptureTime,
    pub completed_at: ProcessingTime,
    pub reason_codes: Vec<String>,
}

pub struct CheckerboardCalibrationEstimator {
    pub policy: CalibrationPolicy,
    pub geometry: CheckerboardGeometry,
    pub maximum_correction_rad: f64,
}

impl CheckerboardCalibrationEstimator {
    pub fn new(policy: CalibrationPolicy, geometry: CheckerboardGeometry) -> Self {
        Self {
            policy,
            geometry,
            maximum_correction_rad: DEFAULT_MAX_CORRECTION_RAD,
        }
    }

    pub fn estimate(
        &self,
        references: &ReferenceObservationBatch,
        config: &StationConfig,
        now: &ProcessingTime,
    ) -> CalibrationAttempt {
        let attempt =
            |status: CalibrationAttemptStatus,
             reasons: Vec<String>,
             estimate: Option<CalibrationEstimate>| CalibrationAttempt {
                status,
                estimate,
                reference_frame_ref: references.auxiliary_frame_ref.clone(),
                reference_capture_time: references.capture_time.clone(),
                completed_at: now.clone(),
                reason_codes: reasons,
            };

        if capture_midpoint_ns(&references.capture_time).is_err() {
            return attempt(
                CalibrationAttemptStatus::Failed,
                vec!["invalid_capture_time".to_string()],
                None,
            );
        }
        if references.auxiliary_frame_ref.station_id != config.station_id {
            return attempt(
                CalibrationAttemptStatus::Failed,
                vec!["station_mismatch".to_string()],
                None,
            );
        }
        if references.auxiliary_frame_ref.camera_role != CameraRole::Auxiliary as i32 {
            return attempt(
                CalibrationAttemptStatus::FAILED_COMPAT(),
                vec!["auxiliary_frame_required".to_string()],
                None,
            );
        }
        if references.processing_status != ProcessingStatus::Processed {
            let mut reasons = vec!["reference_processing_failed".to_string()];
            reasons.extend(references.reason_codes.iter().cloned());
            return attempt(CalibrationAttemptStatus::Failed, reasons, None);
        }
        let minimum = self.policy.minimum_reference_count.max(4) as usize;
        if references.observations.len() < minimum {
            return attempt(
                CalibrationAttemptStatus::ReferenceUnavailable,
                vec!["insufficient_reference_observations".to_string()],
                None,
            );
        }
        if config.reference_anchor.is_none() {
            return attempt(
                CalibrationAttemptStatus::ReferenceUnavailable,
                vec!["checkerboard_requires_surveyed_reference".to_string()],
                None,
            );
        }

        match self.solve(references, config) {
            Err(err) => attempt(
                CalibrationAttemptStatus::GeometryInvalid,
                vec!["calibration_geometry_invalid".to_string(), err],
                None,
            ),
            Ok(None) => attempt(
                CalibrationAttemptStatus::GeometryInvalid,
                vec!["no_pnp_candidate_within_installation_prior".to_string()],
                None,
            ),
            Ok(Some(cand)) => {
                if cand.rmse_px > self.policy.maximum_reprojection_rmse_px {
                    return attempt(
                        CalibrationAttemptStatus::Failed,
                        vec![
                            "reprojection_rmse_exceeded".to_string(),
                            format!("rmse_px={:.6}", cand.rmse_px),
                        ],
                        None,
                    );
                }
                let reason_codes = vec![
                    "rotation_only".to_string(),
                    "installation_prior_selected".to_string(),
                    "positive_depth".to_string(),
                ];
                let aux_model = config.auxiliary_camera.as_ref().unwrap();
                let (k_norm, _) = parse_camera_model(aux_model, "auxiliary_camera").unwrap();
                let focal_px = 0.5 * (k_norm[0][0] + k_norm[1][1]);
                let sigma_rad = (cand.rmse_px / focal_px.max(EPS)).max(1e-9);
                let variance =
                    sigma_rad * sigma_rad / (references.observations.len().max(1) as f64);
                let cov = [
                    [variance, 0.0, 0.0],
                    [0.0, variance, 0.0],
                    [0.0, 0.0, variance],
                ];
                let quality = CalibrationQuality {
                    source: "opencv_checkerboard_pose".to_string(),
                    matched_reference_count: references.observations.len() as u32,
                    reprojection_rmse_px: Some(cand.rmse_px),
                    angular_residual_rad: Some(cand.correction_angle_rad),
                    reason_codes: reason_codes.clone(),
                    rotation_only_parallax_px: cand.parallax_px,
                };
                let estimate = CalibrationEstimate {
                    reference_capture_time: references.capture_time.clone(),
                    rotation_enu_from_main: cand.main_rotation_enu,
                    correction_covariance_enu_rad2: cov,
                    quality,
                };
                attempt(
                    CalibrationAttemptStatus::Estimated,
                    reason_codes,
                    Some(estimate),
                )
            }
        }
    }

    fn solve(
        &self,
        references: &ReferenceObservationBatch,
        config: &StationConfig,
    ) -> Result<Option<SolvedCandidate>, String> {
        let ref_anchor = config
            .reference_anchor
            .as_ref()
            .ok_or_else(|| "missing reference_anchor".to_string())?;
        let corners = landmark_corners(ref_anchor)?;
        let (image_points, board_indices) = self.image_points(&references.observations)?;
        let aux_model = config
            .auxiliary_camera
            .as_ref()
            .ok_or_else(|| "missing auxiliary_camera".to_string())?;
        let (k_norm, dist) = parse_camera_model(aux_model, "auxiliary_camera")?;
        let baseline_main_pb = config
            .baseline_orientation_enu_from_main
            .as_ref()
            .ok_or_else(|| "missing baseline_orientation_enu_from_main".to_string())?;
        let fixed_aux_pb = config
            .fixed_rotation_aux_from_main
            .as_ref()
            .ok_or_else(|| "missing fixed_rotation_aux_from_main".to_string())?;
        let baseline_main = mat3_from_slice(
            &baseline_main_pb.values,
            "baseline_orientation_enu_from_main",
        )
        .map_err(|e| e.to_string())?;
        let fixed_aux_from_main =
            mat3_from_slice(&fixed_aux_pb.values, "fixed_rotation_aux_from_main")
                .map_err(|e| e.to_string())?;
        let baseline_aux = mat3_mul(&baseline_main, &mat3_transpose(&fixed_aux_from_main));

        let rigid = config
            .full_rigid_transform_aux_from_main
            .as_ref()
            .ok_or_else(|| {
                "full_rigid_transform_aux_from_main required for rotation fit".to_string()
            })?;
        let trans_aux_pb = rigid
            .translation_a_m
            .as_ref()
            .ok_or_else(|| "missing rigid translation_a_m".to_string())?;
        let translation_aux = [trans_aux_pb.x, trans_aux_pb.y, trans_aux_pb.z];
        let main_center_pb = config
            .main_camera_position_enu_m
            .as_ref()
            .ok_or_else(|| "missing main_camera_position_enu_m".to_string())?;
        let main_center = [main_center_pb.x, main_center_pb.y, main_center_pb.z];
        if translation_aux.iter().any(|x| !x.is_finite())
            || main_center.iter().any(|x| !x.is_finite())
        {
            return Err("installation positions must be finite 3-vectors".to_string());
        }

        let eps = 1e-6_f64;
        let fd_rots: [(Mat3, Mat3); 3] = std::array::from_fn(|axis| {
            let mut d = [0.0; 3];
            d[axis] = eps;
            (
                rodrigues_vec_to_mat(d),
                rodrigues_vec_to_mat([-d[0], -d[1], -d[2]]),
            )
        });

        let mut best: Option<SolvedCandidate> = None;
        let flips = [(false, false), (true, false), (false, true), (true, true)];
        for (flip_idx, (flip_r, flip_c)) in flips.into_iter().enumerate() {
            let world_points: Vec<Vec3> = board_indices
                .iter()
                .map(|&(r, c)| {
                    let rr = if flip_r {
                        self.geometry.rows - 1 - r
                    } else {
                        r
                    };
                    let cc = if flip_c {
                        self.geometry.columns - 1 - c
                    } else {
                        c
                    };
                    bilinear_corner(&corners, rr, cc, &self.geometry)
                })
                .collect();

            let Ok((r_cam_from_enu, t_cam)) = fit_rotation_with_fixed_main_center(
                &world_points,
                &image_points,
                &k_norm,
                &dist,
                &baseline_aux,
                main_center,
                translation_aux,
                &fd_rots,
                eps,
            ) else {
                continue;
            };

            let mut all_positive_z = true;
            let mut sum_sq_err = 0.0;
            for (wp, ip) in world_points.iter().zip(image_points.iter()) {
                let cam_pt = add3(mat3_vec(&r_cam_from_enu, *wp), t_cam);
                if cam_pt.iter().any(|x| !x.is_finite()) || cam_pt[2] <= EPS {
                    all_positive_z = false;
                    break;
                }
                let Ok(proj) = project_point(*wp, &r_cam_from_enu, t_cam, &k_norm, &dist) else {
                    all_positive_z = false;
                    break;
                };
                let dx = proj[0] - ip[0];
                let dy = proj[1] - ip[1];
                sum_sq_err += dx * dx + dy * dy;
            }
            if !all_positive_z {
                continue;
            }
            let rmse = (sum_sq_err / (world_points.len() as f64)).sqrt();
            let rotation_enu_from_aux = mat3_transpose(&r_cam_from_enu);
            let correction = mat3_mul(&mat3_transpose(&baseline_aux), &rotation_enu_from_aux);
            let correction_angle = rotation_angle(&correction);
            if correction_angle > self.maximum_correction_rad + 1e-7 {
                continue;
            }
            let main_rotation =
                mat3_mul(&mat3_mul(&baseline_aux, &correction), &fixed_aux_from_main);
            let main_error =
                rotation_angle(&mat3_mul(&mat3_transpose(&baseline_main), &main_rotation));
            if main_error > self.maximum_correction_rad + 1e-7 {
                continue;
            }
            let cand = SolvedCandidate {
                rmse_px: rmse,
                parallax_px: Some(rmse),
                correction_angle_rad: correction_angle,
                main_rotation_enu: main_rotation,
            };
            let better = best.as_ref().is_none_or(|b| {
                (cand.rmse_px, cand.correction_angle_rad) < (b.rmse_px, b.correction_angle_rad)
            });
            if better {
                best = Some(cand);
                if flip_idx == 0 && rmse <= self.policy.maximum_reprojection_rmse_px {
                    break;
                }
            }
        }
        Ok(best)
    }

    fn image_points(&self, observations: &[ReferenceObservation]) -> Result<IndexedPoints, String> {
        let mut indices = Vec::with_capacity(observations.len());
        let mut points = Vec::with_capacity(observations.len());
        let mut seen = HashSet::new();
        for obs in observations {
            let Some((r, c)) = parse_inner_index(&obs.reference_id) else {
                return Err("unlabelled checkerboard observation".to_string());
            };
            if r >= self.geometry.rows || c >= self.geometry.columns {
                return Err("checkerboard inner-corner index out of range".to_string());
            }
            if !seen.insert((r, c)) {
                return Err("duplicate checkerboard inner-corner index".to_string());
            }
            if obs.centroid_px.iter().any(|x| !x.is_finite()) {
                return Err("reference centroids must be finite pixel coordinates".to_string());
            }
            indices.push((r, c));
            points.push(obs.centroid_px);
        }
        Ok((points, indices))
    }
}

trait FailedCompat {
    const FAILED_COMPAT: fn() -> CalibrationAttemptStatus = || CalibrationAttemptStatus::Failed;
}
impl FailedCompat for CalibrationAttemptStatus {}

#[derive(Debug, Clone)]
struct SolvedCandidate {
    rmse_px: f64,
    parallax_px: Option<f64>,
    correction_angle_rad: f64,
    main_rotation_enu: Mat3,
}

#[allow(clippy::too_many_arguments)]
fn fit_rotation_with_fixed_main_center(
    world_points: &[Vec3],
    image_points: &[[f64; 2]],
    k_norm: &Mat3,
    dist: &DistortionCoeffs,
    baseline_aux: &Mat3,
    main_center: Vec3,
    translation_aux: Vec3,
    fd_rots: &[(Mat3, Mat3); 3],
    eps: f64,
) -> Result<(Mat3, Vec3), String> {
    let mut rotation = mat3_transpose(baseline_aux);
    let project_all = |cur: &Mat3| -> Result<Vec<[f64; 2]>, String> {
        let t = add3(scale3(mat3_vec(cur, main_center), -1.0), translation_aux);
        world_points
            .iter()
            .map(|&wp| project_point(wp, cur, t, k_norm, dist).map_err(|e| e.to_string()))
            .collect()
    };

    for _ in 0..8 {
        let projected = project_all(&rotation)?;
        let n = world_points.len();
        let mut neg_residual = Vec::with_capacity(n * 2);
        for (proj, obs) in projected.iter().zip(image_points.iter()) {
            neg_residual.push(obs[0] - proj[0]);
            neg_residual.push(obs[1] - proj[1]);
        }

        let mut j_cols = [Vec::new(), Vec::new(), Vec::new()];
        for axis in 0..3 {
            let plus = mat3_mul(&fd_rots[axis].0, &rotation);
            let minus = mat3_mul(&fd_rots[axis].1, &rotation);
            let p_plus = project_all(&plus)?;
            let p_minus = project_all(&minus)?;
            j_cols[axis].reserve(n * 2);
            for (pp, pm) in p_plus.iter().zip(p_minus.iter()) {
                j_cols[axis].push((pp[0] - pm[0]) / (2.0 * eps));
                j_cols[axis].push((pp[1] - pm[1]) / (2.0 * eps));
            }
        }
        let j_rows: Vec<[f64; 3]> = (0..(n * 2))
            .map(|row| [j_cols[0][row], j_cols[1][row], j_cols[2][row]])
            .collect();
        let mut step = solve_least_squares_3(&j_rows, &neg_residual).map_err(|e| e.to_string())?;
        let step_norm = norm3(step);
        if !step_norm.is_finite() {
            return Err("non-finite Gauss-Newton step".to_string());
        }
        if step_norm > 0.05 {
            step = scale3(step, 0.05 / step_norm);
        }
        if step_norm < 1e-10 {
            break;
        }
        let update = rodrigues_vec_to_mat(step);
        rotation = mat3_mul(&update, &rotation);
    }

    let final_t = add3(
        scale3(mat3_vec(&rotation, main_center), -1.0),
        translation_aux,
    );
    Ok((rotation, final_t))
}

pub struct CalibrationSnapshot {
    pub main_capture_time: CaptureTime,
    pub state: CalibrationState,
    pub solution: Option<CalibrationSolution>,
    pub usage: Option<CalibrationUsage>,
    pub reason_codes: Vec<String>,
}

pub struct CalibrationHistoryStore {
    pub station_id: String,
    pub station_session_id: String,
    pub station_config_digest: String,
    solutions: HashMap<String, CalibrationSolution>,
    solution_order: Vec<String>,
    states: Vec<CalibrationState>,
}

impl CalibrationHistoryStore {
    pub fn new(
        station_id: String,
        station_session_id: String,
        station_config_digest: String,
    ) -> Self {
        Self {
            station_id,
            station_session_id,
            station_config_digest,
            solutions: HashMap::new(),
            solution_order: Vec::new(),
            states: Vec::new(),
        }
    }

    pub fn record_solution(&mut self, solution: CalibrationSolution) {
        let sol_id = solution
            .solution_ref
            .as_ref()
            .map(|r| r.solution_id.clone())
            .unwrap_or_default();
        if !self.solutions.contains_key(&sol_id) {
            self.solution_order.push(sol_id.clone());
            self.solutions.insert(sol_id, solution);
        }
    }

    pub fn record_state(&mut self, state: CalibrationState) {
        self.states.push(state);
    }

    pub fn snapshot_at(&self, capture_time: &CaptureTime) -> CalibrationSnapshot {
        let midpoint = capture_midpoint_ns(capture_time).unwrap_or(0);
        let mut best_sol: Option<(&CalibrationSolution, i64, i64)> = None;
        for sol in self.solutions.values() {
            let Some(ref_ct) = &sol.reference_capture_time else {
                continue;
            };
            let Some(acc) = &sol.accepted_time else {
                continue;
            };
            let Some(valid) = &sol.valid_interval else {
                continue;
            };
            if ref_ct.clock_domain_id != capture_time.clock_domain_id
                || acc.clock_domain_id != capture_time.clock_domain_id
                || valid.clock_domain_id != capture_time.clock_domain_id
            {
                continue;
            }
            let ref_mid = capture_midpoint_ns(ref_ct).unwrap_or(i64::MAX);
            if ref_mid > midpoint || acc.time_ns > midpoint {
                continue;
            }
            if valid.start_ns > midpoint || valid.end_ns.is_some_and(|e| midpoint >= e) {
                continue;
            }
            if best_sol
                .as_ref()
                .is_none_or(|(_, br, ba)| (ref_mid, acc.time_ns) > (*br, *ba))
            {
                best_sol = Some((sol, ref_mid, acc.time_ns));
            }
        }

        let Some((solution, _, _)) = best_sol else {
            let state = self
                .state_at(midpoint, &capture_time.clock_domain_id, None)
                .cloned()
                .unwrap_or_else(|| self.empty_state(capture_time));
            return CalibrationSnapshot {
                main_capture_time: capture_time.clone(),
                state,
                solution: None,
                usage: None,
                reason_codes: vec!["no_calibration_solution_known_at_exposure".to_string()],
            };
        };

        let sol_ref = solution.solution_ref.as_ref();
        let Some(state) = self.state_at(midpoint, &capture_time.clock_domain_id, sol_ref) else {
            return CalibrationSnapshot {
                main_capture_time: capture_time.clone(),
                state: self.empty_state(capture_time),
                solution: None,
                usage: None,
                reason_codes: vec!["no_calibration_state_known_at_exposure".to_string()],
            };
        };

        let health =
            CalibrationHealth::try_from(state.health).unwrap_or(CalibrationHealth::Unspecified);
        let usage = match health {
            CalibrationHealth::Valid => Some(CalibrationUsage::Valid),
            CalibrationHealth::Frozen | CalibrationHealth::Recovering => {
                Some(CalibrationUsage::Frozen)
            }
            CalibrationHealth::Stale => Some(CalibrationUsage::Stale),
            _ => None,
        };
        let Some(usage) = usage else {
            return CalibrationSnapshot {
                main_capture_time: capture_time.clone(),
                state: state.clone(),
                solution: None,
                usage: None,
                reason_codes: vec!["calibration_state_not_usable".to_string()],
            };
        };
        CalibrationSnapshot {
            main_capture_time: capture_time.clone(),
            state: state.clone(),
            solution: Some(solution.clone()),
            usage: Some(usage),
            reason_codes: state.reason_codes.clone(),
        }
    }

    fn state_at(
        &self,
        midpoint: i64,
        clock_domain_id: &str,
        sol_ref: Option<&CalibrationSolutionRef>,
    ) -> Option<&CalibrationState> {
        for state in self.states.iter().rev() {
            let Some(obs) = &state.observed_at else {
                continue;
            };
            if obs.clock_domain_id != clock_domain_id || obs.time_ns > midpoint {
                continue;
            }
            if let Some(expected_ref) = sol_ref {
                if state.solution_ref.as_ref() != Some(expected_ref) {
                    continue;
                }
            }
            return Some(state);
        }
        None
    }

    fn empty_state(&self, capture_time: &CaptureTime) -> CalibrationState {
        let mid = capture_midpoint_ns(capture_time).unwrap_or(0);
        CalibrationState {
            station_id: self.station_id.clone(),
            station_session_id: self.station_session_id.clone(),
            station_config_digest: self.station_config_digest.clone(),
            state_version: 0,
            health: CalibrationHealth::Initializing as i32,
            solution_ref: None,
            observed_at: Some(ProcessingTime {
                clock_domain_id: capture_time.clock_domain_id.clone(),
                time_ns: mid,
            }),
            last_trusted_time: None,
            usable_interval: None,
            reason_codes: vec!["no_calibration_state_known_at_exposure".to_string()],
        }
    }
}

pub struct CalibrationUpdate {
    pub state_event: CalibrationStateEvent,
    pub diagnostics: Vec<DiagnosticEvent>,
}

pub struct AuxiliaryCalibrationPipeline {
    pub policy: CalibrationPolicy,
    pub recognizer: CheckerboardReferenceRecognizer,
    pub estimator: CheckerboardCalibrationEstimator,
    pub history: CalibrationHistoryStore,
    state: Option<CalibrationState>,
    solution: Option<CalibrationSolution>,
    version: u64,
    solution_sequence: u64,
    recovery_successes: i64,
    last_update: Option<CalibrationUpdate>,
    last_reference_midpoint: Option<i64>,
    event_seq: u64,
}

impl AuxiliaryCalibrationPipeline {
    pub fn new(
        station_id: String,
        station_session_id: String,
        station_config_digest: String,
        policy: CalibrationPolicy,
    ) -> Self {
        let geom = CheckerboardGeometry::default();
        let recognizer = CheckerboardReferenceRecognizer::new(geom);
        let estimator = CheckerboardCalibrationEstimator::new(policy.clone(), geom);
        let history =
            CalibrationHistoryStore::new(station_id, station_session_id, station_config_digest);
        Self {
            policy,
            recognizer,
            estimator,
            history,
            state: None,
            solution: None,
            version: 0,
            solution_sequence: 0,
            recovery_successes: 0,
            last_update: None,
            last_reference_midpoint: None,
            event_seq: 0,
        }
    }

    pub fn process(
        &mut self,
        frame: &CameraFrame,
        config: &StationConfig,
        now: &ProcessingTime,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> CalibrationUpdate {
        let midpoint = capture_midpoint_ns(&frame.capture_time).unwrap_or(0);
        if let Some(prev) = self.last_reference_midpoint {
            if self.policy.update_period_ns > 0 && midpoint - prev < self.policy.update_period_ns {
                if let Some(last) = &self.last_update {
                    return CalibrationUpdate {
                        state_event: last.state_event.clone(),
                        diagnostics: last.diagnostics.clone(),
                    };
                }
            }
        }
        self.last_reference_midpoint = Some(midpoint);
        let refs = self.recognizer.recognize(frame, config);
        let attempt = self.estimator.estimate(&refs, config, now);
        self.consider(attempt, config, next_seq)
    }

    pub fn tick(
        &mut self,
        now: &ProcessingTime,
        config: &StationConfig,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> Option<CalibrationUpdate> {
        let state = self.state.as_ref()?;
        let sol = self.solution.as_ref()?;
        if state.health == CalibrationHealth::Stale as i32 {
            return None;
        }
        let acc = sol.accepted_time.as_ref()?;
        if now.time_ns - acc.time_ns <= self.policy.stale_after_ns {
            return None;
        }
        Some(self.publish(
            config,
            now.clone(),
            CalibrationHealth::Stale,
            sol.solution_ref.clone(),
            Some(acc.clone()),
            vec!["calibration_stale_after_timeout".to_string()],
            None,
            next_seq,
        ))
    }

    fn consider(
        &mut self,
        attempt: CalibrationAttempt,
        config: &StationConfig,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> CalibrationUpdate {
        let reason_fail: String;
        if let (CalibrationAttemptStatus::Estimated, Some(est)) = (attempt.status, attempt.estimate)
        {
            let q = &est.quality;
            let quality_ok = (q.matched_reference_count as i64)
                >= self.policy.minimum_reference_count.max(4)
                && q.reprojection_rmse_px
                    .is_some_and(|r| r <= self.policy.maximum_reprojection_rmse_px);
            let ref_mid = capture_midpoint_ns(&est.reference_capture_time).unwrap_or(0);
            let ref_is_new = self.solution.as_ref().is_none_or(|prev| {
                let prev_mid = prev
                    .reference_capture_time
                    .as_ref()
                    .and_then(|c| capture_midpoint_ns(c).ok())
                    .unwrap_or(0);
                ref_mid > prev_mid
            });
            if quality_ok && ref_is_new {
                let recovering = self.state.as_ref().is_some_and(|s| {
                    matches!(
                        CalibrationHealth::try_from(s.health),
                        Ok(CalibrationHealth::Frozen
                            | CalibrationHealth::Stale
                            | CalibrationHealth::Recovering)
                    )
                });
                if recovering {
                    self.recovery_successes += 1;
                } else {
                    self.recovery_successes = self.policy.recovery_success_count;
                }
                if self.recovery_successes >= self.policy.recovery_success_count.max(1) {
                    return self.accept(
                        est,
                        attempt.completed_at,
                        attempt.reason_codes,
                        config,
                        next_seq,
                    );
                }
                let mut reasons = vec!["recovery_observation".to_string()];
                reasons.extend(attempt.reason_codes);
                return self.publish(
                    config,
                    attempt.completed_at,
                    CalibrationHealth::Recovering,
                    self.solution.as_ref().and_then(|s| s.solution_ref.clone()),
                    self.solution.as_ref().and_then(|s| s.accepted_time.clone()),
                    reasons,
                    None,
                    next_seq,
                );
            }
            reason_fail = if !quality_ok {
                "calibration_quality_rejected".to_string()
            } else {
                "reference_capture_time_not_increasing".to_string()
            };
        } else {
            reason_fail = "calibration_attempt_failed".to_string();
        }

        let mut health = if self.solution.is_some() {
            CalibrationHealth::Frozen
        } else {
            CalibrationHealth::Initializing
        };
        if self
            .state
            .as_ref()
            .is_some_and(|s| s.health == CalibrationHealth::Stale as i32)
        {
            health = CalibrationHealth::Stale;
        }
        self.recovery_successes = 0;
        let mut reasons = vec![reason_fail];
        reasons.extend(attempt.reason_codes);
        self.publish(
            config,
            attempt.completed_at,
            health,
            self.solution.as_ref().and_then(|s| s.solution_ref.clone()),
            self.solution.as_ref().and_then(|s| s.accepted_time.clone()),
            reasons,
            None,
            next_seq,
        )
    }

    fn accept(
        &mut self,
        est: CalibrationEstimate,
        completed_at: ProcessingTime,
        reasons: Vec<String>,
        config: &StationConfig,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> CalibrationUpdate {
        self.solution_sequence += 1;
        let sol_ref = CalibrationSolutionRef {
            station_id: self.history.station_id.clone(),
            station_session_id: self.history.station_session_id.clone(),
            solution_id: format!("opencv-calibration-{}", self.solution_sequence),
        };
        let ref_mid = capture_midpoint_ns(&est.reference_capture_time).unwrap_or(0);
        let main_pos = config.main_camera_position_enu_m.unwrap_or(ProtoVec3 {
            x: 0.0,
            y: 0.0,
            z: 0.0,
        });
        let solution = CalibrationSolution {
            solution_ref: Some(sol_ref.clone()),
            station_config_digest: config.config_digest.clone(),
            world_frame_id: config.world_frame_id.clone(),
            reference_capture_time: Some(est.reference_capture_time.clone()),
            accepted_time: Some(completed_at.clone()),
            valid_interval: Some(TimeInterval {
                clock_domain_id: est.reference_capture_time.clock_domain_id.clone(),
                start_ns: ref_mid,
                end_ns: None,
            }),
            main_camera_position_enu_m: Some(main_pos),
            rotation_enu_from_main: Some(ProtoMat3 {
                values: est.rotation_enu_from_main.into_iter().flatten().collect(),
            }),
            correction_covariance_enu_rad2: Some(ProtoMat3 {
                values: est
                    .correction_covariance_enu_rad2
                    .into_iter()
                    .flatten()
                    .collect(),
            }),
            quality: Some(est.quality),
        };
        self.history.record_solution(solution.clone());
        self.solution = Some(solution.clone());
        self.recovery_successes = 0;
        self.publish(
            config,
            completed_at.clone(),
            CalibrationHealth::Valid,
            Some(sol_ref),
            Some(completed_at),
            reasons,
            Some(solution),
            next_seq,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn publish(
        &mut self,
        config: &StationConfig,
        observed_at: ProcessingTime,
        health: CalibrationHealth,
        solution_ref: Option<CalibrationSolutionRef>,
        last_trusted_time: Option<ProcessingTime>,
        reason_codes: Vec<String>,
        solution: Option<CalibrationSolution>,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> CalibrationUpdate {
        self.version += 1;
        let interval = self
            .solution
            .as_ref()
            .and_then(|s| s.valid_interval.clone());
        let state = CalibrationState {
            station_id: self.history.station_id.clone(),
            station_session_id: self.history.station_session_id.clone(),
            station_config_digest: config.config_digest.clone(),
            state_version: self.version,
            health: health as i32,
            solution_ref: solution_ref.clone(),
            observed_at: Some(observed_at),
            last_trusted_time,
            usable_interval: interval,
            reason_codes: reason_codes.clone(),
        };
        self.history.record_state(state.clone());
        let env = next_seq(EventType::CalibrationState, &config.config_digest);
        let state_event = CalibrationStateEvent {
            envelope: Some(env),
            state: Some(state.clone()),
            solution,
        };
        let mut diagnostics = Vec::new();
        let emit_diag = matches!(
            health,
            CalibrationHealth::Initializing
                | CalibrationHealth::Frozen
                | CalibrationHealth::Stale
                | CalibrationHealth::Recovering
        ) && !(health == CalibrationHealth::Recovering && self.solution.is_some());
        if emit_diag {
            let diag_env = next_seq(EventType::Diagnostic, &config.config_digest);
            diagnostics.push(DiagnosticEvent {
                envelope: Some(diag_env),
                related_frame_ref: None,
                capture_time: None,
                category: "calibration_unavailable".to_string(),
                reason_codes,
                related_config_ref: Some(config.config_digest.clone()),
                related_calibration_ref: solution_ref,
            });
        }
        self.state = Some(state);
        self.event_seq += 1;
        self.last_update = Some(CalibrationUpdate {
            state_event: state_event.clone(),
            diagnostics: diagnostics.clone(),
        });
        CalibrationUpdate {
            state_event,
            diagnostics,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_synthetic_6x4_checkerboard_and_subpixel_corners() {
        let w = 160;
        let h = 120;
        let sq = 16;
        let ox = 24;
        let oy = 20;
        let mut gray = vec![220u8; w * h];
        for r in 0..5 {
            for c in 0..7 {
                let val = if (r + c) % 2 == 0 { 20u8 } else { 235u8 };
                for dy in 0..sq {
                    for dx in 0..sq {
                        gray[(oy + r * sq + dy) * w + (ox + c * sq + dx)] = val;
                    }
                }
            }
        }
        let corners = detect_checkerboard_corners(&gray, w, h, 6, 4).expect("should detect 6x4");
        assert_eq!(corners.len(), 24);
        // Inner corners sit at ox + (c+1)*sq - 0.5, oy + (r+1)*sq - 0.5
        for p in &corners {
            assert!(p[0] > ox as f64 && p[0] < (ox + 7 * sq) as f64);
            assert!(p[1] > oy as f64 && p[1] < (oy + 5 * sq) as f64);
        }
    }
}
