//! Measured main-image centroids to conditional, exposure-compatible ENU rays.

use crate::calibration::CalibrationSnapshot;
use crate::detection::capture_midpoint_ns;
use crate::math::{
    covariance_factor_2x2, cross3, dot3, invert_2x2, is_proper_rotation, mat2_mul, mat2_transpose,
    mat3_from_slice, mat3_vec, norm3, normalize_intrinsic_matrix, scale3, solve_3x3, sub3,
    undistort_point_iter, DistortionCoeffs, Mat2, Vec3,
};
use crate::policy::{RayGenerationPolicy, RAY_UNCERTAINTY_MODEL_ID};
use crate::proto::{
    bus_message, CalibrationHealth, CalibrationSolution, CalibrationUsage, CalibrationUseEvidence,
    CameraRole, CaptureTime, DiagnosticEvent, EventEnvelope, EventType, Mat2 as ProtoMat2,
    Mat3 as ProtoMat3, Observation2D, RayEvent, RayUncertainty, StationConfig, TimeInterval,
    Vec3 as ProtoVec3,
};

fn contains_interval(interval: &TimeInterval, capture: &CaptureTime) -> bool {
    let Ok(midpoint) = capture_midpoint_ns(capture) else {
        return false;
    };
    interval.clock_domain_id == capture.clock_domain_id
        && interval.start_ns <= midpoint
        && interval.end_ns.is_none_or(|e| midpoint < e)
}

fn calibration_reason(
    observation: &Observation2D,
    config: &StationConfig,
    snapshot: &CalibrationSnapshot,
) -> Option<&'static str> {
    let frame = observation.main_frame_ref.as_ref()?;
    let obs_ct = observation.capture_time.as_ref()?;
    if &snapshot.main_capture_time != obs_ct {
        return Some("calibration_exposure_mismatch");
    }
    let solution = snapshot.solution.as_ref()?;
    let usage = snapshot.usage?;
    let state = &snapshot.state;
    let sol_ref = solution.solution_ref.as_ref()?;
    if sol_ref.station_id != frame.station_id
        || sol_ref.station_session_id != frame.station_session_id
        || state.station_id != frame.station_id
        || state.station_session_id != frame.station_session_id
        || solution.station_config_digest != config.config_digest
        || state.station_config_digest != config.config_digest
        || solution.world_frame_id != config.world_frame_id
        || state.solution_ref.as_ref() != Some(sol_ref)
        || state.state_version == 0
    {
        return Some("calibration_scope_mismatch");
    }
    let health = CalibrationHealth::try_from(state.health).ok()?;
    let expected_usage = match health {
        CalibrationHealth::Valid => CalibrationUsage::Valid,
        CalibrationHealth::Frozen | CalibrationHealth::Recovering => CalibrationUsage::Frozen,
        CalibrationHealth::Stale => CalibrationUsage::Stale,
        _ => return Some("calibration_usage_mismatch"),
    };
    if usage != expected_usage {
        return Some("calibration_usage_mismatch");
    }
    let last_trusted = state.last_trusted_time.as_ref()?;
    let usable_interval = state.usable_interval.as_ref()?;
    let accepted_time = solution.accepted_time.as_ref()?;
    let observed_at = state.observed_at.as_ref()?;
    let ref_ct = solution.reference_capture_time.as_ref()?;

    let midpoint = capture_midpoint_ns(obs_ct).ok()?;
    let ref_mid = capture_midpoint_ns(ref_ct).ok()?;
    for t in [accepted_time, observed_at, last_trusted] {
        if t.clock_domain_id != obs_ct.clock_domain_id || t.time_ns > midpoint {
            return Some("calibration_not_known_at_exposure");
        }
    }
    if ref_ct.clock_domain_id != obs_ct.clock_domain_id || ref_mid > midpoint {
        return Some("calibration_not_known_at_exposure");
    }
    let valid_interval = solution.valid_interval.as_ref()?;
    if !contains_interval(valid_interval, obs_ct) || !contains_interval(usable_interval, obs_ct) {
        return Some("calibration_exposure_outside_validity");
    }
    if solution.main_camera_position_enu_m != config.main_camera_position_enu_m {
        return Some("calibration_fixed_optical_center_mismatch");
    }
    None
}

struct RayGeometry {
    direction: Vec3,
    tangent_u: Vec3,
    tangent_v: Vec3,
    jacobian: Mat2,
}

fn compute_ray_geometry(
    observation: &Observation2D,
    config: &StationConfig,
    solution: &CalibrationSolution,
) -> Result<RayGeometry, String> {
    let centroid = observation
        .centroid_px
        .as_ref()
        .ok_or_else(|| "observation missing centroid_px".to_string())?;
    let px = [centroid.x, centroid.y];
    let main_cam = config
        .main_camera
        .as_ref()
        .ok_or_else(|| "missing main_camera".to_string())?;
    let intr = main_cam
        .intrinsics
        .as_ref()
        .ok_or_else(|| "missing main_camera.intrinsics".to_string())?;
    let w = intr.image_width_px as f64;
    let h = intr.image_height_px as f64;
    if !px[0].is_finite()
        || !px[1].is_finite()
        || px[0] < 0.0
        || px[0] >= w
        || px[1] < 0.0
        || px[1] >= h
    {
        return Err("centroid must be finite and inside the main image".to_string());
    }
    let k_raw_pb = intr
        .matrix_px
        .as_ref()
        .ok_or_else(|| "missing matrix_px".to_string())?;
    let k_raw = mat3_from_slice(&k_raw_pb.values, "main matrix_px").map_err(|e| e.to_string())?;
    let k_norm = normalize_intrinsic_matrix(&k_raw).map_err(|e| e.to_string())?;
    let dist_pb = main_cam
        .distortion
        .as_ref()
        .ok_or_else(|| "missing main distortion".to_string())?;
    let dist = DistortionCoeffs::from_proto(&dist_pb.model, &dist_pb.coefficients)
        .map_err(|e| e.to_string())?;

    let distorted_norm = solve_3x3(k_norm, [px[0], px[1], 1.0]).map_err(|e| e.to_string())?;
    let [xn, yn] = undistort_point_iter(distorted_norm[0], distorted_norm[1], &dist, 50, 1e-12)
        .map_err(|e| e.to_string())?;
    let ([xd, yd], d_dist_d_norm) = dist
        .distort_with_jacobian(xn, yn)
        .map_err(|e| e.to_string())?;
    let proj_u = k_norm[0][0] * xd + k_norm[0][1] * yd + k_norm[0][2];
    let proj_v = k_norm[1][1] * yd + k_norm[1][2];
    if (proj_u - px[0]).abs() > 1e-7 || (proj_v - px[1]).abs() > 1e-7 {
        return Err("main lens inverse did not converge".to_string());
    }

    let k2x2: Mat2 = [[k_norm[0][0], k_norm[0][1]], [0.0, k_norm[1][1]]];
    let lens_jacobian = mat2_mul(&k2x2, &d_dist_d_norm);
    let inv_lens_jac = invert_2x2(&lens_jacobian).map_err(|e| e.to_string())?;

    let rot_pb = solution
        .rotation_enu_from_main
        .as_ref()
        .ok_or_else(|| "solution missing rotation_enu_from_main".to_string())?;
    let rotation =
        mat3_from_slice(&rot_pb.values, "rotation_enu_from_main").map_err(|e| e.to_string())?;
    if !is_proper_rotation(&rotation, 1e-10) {
        return Err("calibration requires a proper rotation".to_string());
    }
    let origin_pb = solution
        .main_camera_position_enu_m
        .as_ref()
        .ok_or_else(|| "solution missing main_camera_position_enu_m".to_string())?;
    if !origin_pb.x.is_finite() || !origin_pb.y.is_finite() || !origin_pb.z.is_finite() {
        return Err("calibration requires a finite origin".to_string());
    }

    let optical = [xn, yn, 1.0];
    let length = norm3(optical);
    let local_dir = scale3(optical, 1.0 / length);
    let direction = mat3_vec(&rotation, local_dir);

    let cam_right = [rotation[0][0], rotation[1][0], rotation[2][0]];
    let tangent_u_raw = sub3(cam_right, scale3(direction, dot3(direction, cam_right)));
    let tu_norm = norm3(tangent_u_raw);
    if !tu_norm.is_finite() || tu_norm <= 1e-12 {
        return Err("degenerate ray tangent_u".to_string());
    }
    let tangent_u = scale3(tangent_u_raw, 1.0 / tu_norm);
    let tangent_v = cross3(direction, tangent_u);

    // unit_jacobian[:, j] = (e_j - local_dir * local_dir[j]) / length for j = 0, 1
    let col0_local = [
        (1.0 - local_dir[0] * local_dir[0]) / length,
        (-local_dir[1] * local_dir[0]) / length,
        (-local_dir[2] * local_dir[0]) / length,
    ];
    let col1_local = [
        (-local_dir[0] * local_dir[1]) / length,
        (1.0 - local_dir[1] * local_dir[1]) / length,
        (-local_dir[2] * local_dir[1]) / length,
    ];
    let col0_enu = mat3_vec(&rotation, col0_local);
    let col1_enu = mat3_vec(&rotation, col1_local);
    let d_tang_d_norm: Mat2 = [
        [dot3(tangent_u, col0_enu), dot3(tangent_u, col1_enu)],
        [dot3(tangent_v, col0_enu), dot3(tangent_v, col1_enu)],
    ];
    let jacobian = mat2_mul(&d_tang_d_norm, &inv_lens_jac);
    if direction.iter().any(|x| !x.is_finite())
        || tangent_u.iter().any(|x| !x.is_finite())
        || tangent_v.iter().any(|x| !x.is_finite())
        || jacobian.iter().flatten().any(|x| !x.is_finite())
    {
        return Err("main ray geometry is nonfinite".to_string());
    }
    Ok(RayGeometry {
        direction,
        tangent_u,
        tangent_v,
        jacobian,
    })
}

pub struct RayGenerationModule {
    pub policy: RayGenerationPolicy,
}

impl RayGenerationModule {
    pub fn new(policy: RayGenerationPolicy) -> Self {
        Self { policy }
    }

    pub fn build(
        &self,
        observation: &Observation2D,
        config: &StationConfig,
        calibration: &CalibrationSnapshot,
        next_seq: &mut impl FnMut(EventType, &str) -> EventEnvelope,
    ) -> bus_message::Payload {
        let mut envelope = next_seq(EventType::Ray, &config.config_digest);
        let obs_ct = observation.capture_time.clone();
        let obs_frame = observation.main_frame_ref.clone();
        let cal_ref = calibration
            .solution
            .as_ref()
            .and_then(|s| s.solution_ref.clone());

        let diagnostic =
            |env: EventEnvelope, category: &str, reasons: Vec<String>| -> bus_message::Payload {
                let mut diag_env = env;
                diag_env.event_type = EventType::Diagnostic as i32;
                bus_message::Payload::DiagnosticEvent(DiagnosticEvent {
                    envelope: Some(diag_env),
                    related_frame_ref: obs_frame.clone(),
                    capture_time: obs_ct.clone(),
                    category: category.to_string(),
                    reason_codes: reasons,
                    related_config_ref: Some(config.config_digest.clone()),
                    related_calibration_ref: cal_ref.clone(),
                })
            };

        let Some(frame) = &observation.main_frame_ref else {
            return diagnostic(
                envelope,
                "ray_input_invalid",
                vec!["main_observation_scope_mismatch".to_string()],
            );
        };
        if frame.camera_role != CameraRole::Main as i32
            || frame.station_id != config.station_id
            || frame.station_id != envelope.station_id
            || frame.station_session_id != envelope.station_session_id
            || frame.frame_id.is_empty()
        {
            return diagnostic(
                envelope,
                "ray_input_invalid",
                vec!["main_observation_scope_mismatch".to_string()],
            );
        }
        let Some(capture) = &observation.capture_time else {
            return diagnostic(
                envelope,
                "ray_input_invalid",
                vec!["invalid_capture_time".to_string()],
            );
        };
        if let Err(err) = capture_midpoint_ns(capture) {
            return diagnostic(
                envelope,
                "ray_input_invalid",
                vec!["invalid_capture_time".to_string(), err.to_string()],
            );
        }
        if calibration.solution.is_none() || calibration.usage.is_none() {
            let mut reasons = vec!["calibration_unavailable".to_string()];
            reasons.extend(calibration.reason_codes.iter().cloned());
            return diagnostic(envelope, "calibration_unavailable", reasons);
        }
        if let Some(reason) = calibration_reason(observation, config, calibration) {
            let mut reasons = vec![reason.to_string()];
            reasons.extend(calibration.reason_codes.iter().cloned());
            return diagnostic(envelope, "calibration_unavailable", reasons);
        }
        if capture.rolling_shutter_model != "none" {
            return diagnostic(
                envelope,
                "ray_uncertainty_unavailable",
                vec!["rolling_shutter_model_unsupported".to_string()],
            );
        }
        if self.policy.uncertainty_model_id != RAY_UNCERTAINTY_MODEL_ID {
            return diagnostic(
                envelope,
                "ray_uncertainty_unavailable",
                vec!["uncertainty_model_unsupported".to_string()],
            );
        }

        let (pixel_cov_opt, pixel_source) = match &observation.centroid_covariance_px2 {
            Some(mat) if mat.values.len() == 4 => (
                Some([
                    [mat.values[0], mat.values[1]],
                    [mat.values[2], mat.values[3]],
                ]),
                "observation_pixel_covariance",
            ),
            _ => (
                self.policy.fallback_centroid_covariance_px2,
                "explicit_fallback_pixel_covariance",
            ),
        };
        let mut missing = Vec::new();
        if pixel_cov_opt.is_none() {
            missing.push("unknown_centroid_covariance_px2".to_string());
        }
        if self
            .policy
            .unmodelled_high_frequency_jitter_covariance_rad2
            .is_none()
        {
            missing.push("unknown_residual_jitter_rad2".to_string());
        }
        if self
            .policy
            .unmodelled_relative_flex_covariance_rad2
            .is_none()
        {
            missing.push("unknown_residual_flex_rad2".to_string());
        }
        if !missing.is_empty() {
            return diagnostic(envelope, "ray_uncertainty_unavailable", missing);
        }

        let px_cov = pixel_cov_opt.unwrap();
        let jitter_cov = self
            .policy
            .unmodelled_high_frequency_jitter_covariance_rad2
            .unwrap();
        let flex_cov = self
            .policy
            .unmodelled_relative_flex_covariance_rad2
            .unwrap();

        let px_factor = match covariance_factor_2x2(px_cov, "centroid_covariance_px2") {
            Ok(f) => f,
            Err(e) => {
                return diagnostic(
                    envelope,
                    "ray_geometry_invalid",
                    vec!["invalid_geometry_or_covariance".to_string(), e.to_string()],
                );
            }
        };
        let jitter_factor = match covariance_factor_2x2(jitter_cov, "residual_jitter_rad2") {
            Ok(f) => f,
            Err(e) => {
                return diagnostic(
                    envelope,
                    "ray_geometry_invalid",
                    vec!["invalid_geometry_or_covariance".to_string(), e.to_string()],
                );
            }
        };
        let flex_factor = match covariance_factor_2x2(flex_cov, "residual_flex_rad2") {
            Ok(f) => f,
            Err(e) => {
                return diagnostic(
                    envelope,
                    "ray_geometry_invalid",
                    vec!["invalid_geometry_or_covariance".to_string(), e.to_string()],
                );
            }
        };

        let has_residual = jitter_factor.iter().flatten().any(|&x| x != 0.0)
            || flex_factor.iter().flatten().any(|&x| x != 0.0);
        let independence_basis = self
            .policy
            .residual_independence_basis
            .as_deref()
            .map(str::trim)
            .unwrap_or("");
        if has_residual && independence_basis.is_empty() {
            return diagnostic(
                envelope,
                "ray_uncertainty_unavailable",
                vec!["residual_independence_basis_missing".to_string()],
            );
        }

        let solution = calibration.solution.as_ref().unwrap();
        let geom = match compute_ray_geometry(observation, config, solution) {
            Ok(g) => g,
            Err(e) => {
                return diagnostic(
                    envelope,
                    "ray_geometry_invalid",
                    vec!["invalid_geometry_or_covariance".to_string(), e],
                );
            }
        };

        let pf = mat2_mul(&geom.jacobian, &px_factor);
        let cov_px = mat2_mul(&pf, &mat2_transpose(&pf));
        let cov_j = mat2_mul(&jitter_factor, &mat2_transpose(&jitter_factor));
        let cov_f = mat2_mul(&flex_factor, &mat2_transpose(&flex_factor));
        let total_cov = [
            [
                cov_px[0][0] + cov_j[0][0] + cov_f[0][0],
                cov_px[0][1] + cov_j[0][1] + cov_f[0][1],
            ],
            [
                cov_px[1][0] + cov_j[1][0] + cov_f[1][0],
                cov_px[1][1] + cov_j[1][1] + cov_f[1][1],
            ],
        ];
        if total_cov.iter().flatten().any(|x| !x.is_finite()) {
            return diagnostic(
                envelope,
                "ray_geometry_invalid",
                vec![
                    "invalid_geometry_or_covariance".to_string(),
                    "propagated covariance is nonfinite".to_string(),
                ],
            );
        }

        let state = &calibration.state;
        let usage = calibration.usage.unwrap();
        let mut quality_flags = vec![
            pixel_source.to_string(),
            "first_order_pixel_propagation".to_string(),
        ];
        if has_residual {
            quality_flags.push("residual_jitter_flex_assumed_independent".to_string());
            quality_flags.push(format!("residual_independence_basis:{independence_basis}"));
        } else {
            quality_flags.push("explicit_zero_residual_jitter_and_flex".to_string());
        }
        quality_flags.push("conditional_fixed_optical_center".to_string());
        quality_flags.extend(state.reason_codes.iter().cloned());

        envelope.event_type = EventType::Ray as i32;
        let inst_ref = config
            .installation_uncertainty
            .as_ref()
            .and_then(|u| u.state_ref.clone());

        bus_message::Payload::RayEvent(RayEvent {
            envelope: Some(envelope),
            frame_id: frame.frame_id.clone(),
            local_track_id: observation.local_track_id.clone(),
            capture_time: Some(capture.clone()),
            world_frame_id: config.world_frame_id.clone(),
            calibration_solution_ref: solution.solution_ref.clone(),
            calibration_usage: usage as i32,
            calibration_evidence: Some(CalibrationUseEvidence {
                state_version: state.state_version,
                last_trusted_time: state.last_trusted_time.clone(),
                solution_accepted_time: solution.accepted_time.clone(),
                solution_reference_capture_time: solution.reference_capture_time.clone(),
                solution_valid_interval: solution.valid_interval.clone(),
                usable_interval: state.usable_interval.clone(),
                reason_codes: state.reason_codes.clone(),
            }),
            ray_origin_enu_m: solution.main_camera_position_enu_m,
            ray_direction_enu_unit: Some(ProtoVec3 {
                x: geom.direction[0],
                y: geom.direction[1],
                z: geom.direction[2],
            }),
            uncertainty: Some(RayUncertainty {
                model: self.policy.uncertainty_model_id.clone(),
                tangent_u_enu_unit: Some(ProtoVec3 {
                    x: geom.tangent_u[0],
                    y: geom.tangent_u[1],
                    z: geom.tangent_u[2],
                }),
                tangent_v_enu_unit: Some(ProtoVec3 {
                    x: geom.tangent_v[0],
                    y: geom.tangent_v[1],
                    z: geom.tangent_v[2],
                }),
                independent_angular_covariance_rad2: Some(ProtoMat2 {
                    values: vec![
                        total_cov[0][0],
                        total_cov[0][1],
                        total_cov[1][0],
                        total_cov[1][1],
                    ],
                }),
                independent_origin_covariance_m2: Some(ProtoMat3 {
                    values: vec![0.0; 9],
                }),
                shared_installation_state_ref: inst_ref,
                shared_calibration_solution_ref: solution.solution_ref.clone(),
            }),
            detection_score: observation.detection_score,
            detection_score_definition: observation.detection_score_definition.clone(),
            track_quality: observation.track_quality,
            track_quality_definition: observation.track_quality_definition.clone(),
            quality_flags,
        })
    }
}
