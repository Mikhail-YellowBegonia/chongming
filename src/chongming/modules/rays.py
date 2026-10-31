"""Measured main-image centroids to conditional, exposure-compatible ENU rays."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import cv2
import numpy as np
from numpy.typing import NDArray

from chongming.calibration import _camera_matrix, _distortion_coefficients
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.modules.detection import _midpoint
from chongming.ports.device import EventSequencer
from chongming.uncertainty import covariance_factor

logger = logging.getLogger(__name__)
FloatArray = NDArray[np.float64]
ZERO_ORIGIN_COVARIANCE: m.Matrix3x3 = ((0.0, 0.0, 0.0),) * 3


def _contains(interval: m.TimeInterval, capture: m.CaptureTime) -> bool:
    midpoint = _midpoint(capture)
    return (
        interval.clock_domain_id == capture.clock_domain_id
        and interval.start_ns <= midpoint
        and (interval.end_ns is None or midpoint < interval.end_ns)
    )


def _calibration_reason(
    observation: m.Observation2D, config: m.StationConfig, snapshot: m.CalibrationSnapshot
) -> str | None:
    solution, state = snapshot.solution, snapshot.state
    frame = observation.main_frame_ref
    if snapshot.main_capture_time != observation.capture_time:
        return "calibration_exposure_mismatch"
    if solution is None or snapshot.usage is None:
        return "calibration_unavailable"
    if (
        solution.solution_ref.station_id != frame.station_id
        or solution.solution_ref.station_session_id != frame.station_session_id
        or state.station_id != frame.station_id
        or state.station_session_id != frame.station_session_id
        or solution.station_config_digest != config.config_digest
        or state.station_config_digest != config.config_digest
        or solution.world_frame_id != config.world_frame_id
        or state.solution_ref != solution.solution_ref
        or state.state_version <= 0
    ):
        return "calibration_scope_mismatch"
    expected_usage = {
        m.CalibrationHealth.VALID: m.CalibrationUsage.VALID,
        m.CalibrationHealth.FROZEN: m.CalibrationUsage.FROZEN,
        m.CalibrationHealth.STALE: m.CalibrationUsage.STALE,
        m.CalibrationHealth.RECOVERING: m.CalibrationUsage.FROZEN,
    }.get(state.health)
    if snapshot.usage is not expected_usage:
        return "calibration_usage_mismatch"
    if state.last_trusted_time is None or state.usable_interval is None:
        return "calibration_evidence_missing"
    capture = observation.capture_time
    midpoint = _midpoint(capture)
    if any(
        time.clock_domain_id != capture.clock_domain_id or time.time_ns > midpoint
        for time in (solution.accepted_time, state.observed_at, state.last_trusted_time)
    ) or (
        solution.reference_capture_time.clock_domain_id != capture.clock_domain_id
        or _midpoint(solution.reference_capture_time) > midpoint
    ):
        return "calibration_not_known_at_exposure"
    if not _contains(solution.valid_interval, capture) or not _contains(
        state.usable_interval, capture
    ):
        return "calibration_exposure_outside_validity"
    if solution.main_camera_position_enu_m != config.main_camera_position_enu_m:
        return "calibration_fixed_optical_center_mismatch"
    return None


def _geometry(
    observation: m.Observation2D, config: m.StationConfig, solution: m.CalibrationSolution
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Return direction, right/down tangent basis, and its 2x2 pixel Jacobian.

    Invert K before OpenCV so skew is not silently ignored. The forward lens
    Jacobian provides the inverse derivative, avoiding a finite-difference step
    or an invented pixel noise floor. Convergence checks are numerical accuracy
    checks, not measurement-error assumptions.
    """
    pixel = np.asarray(observation.centroid_px, dtype=np.float64)
    intrinsics = config.main_camera.intrinsics
    if (
        pixel.shape != (2,)
        or not np.isfinite(pixel).all()
        or not 0 <= pixel[0] < intrinsics.image_width_px
        or not 0 <= pixel[1] < intrinsics.image_height_px
    ):
        raise ValueError("centroid must be finite and inside the main image")
    matrix = _camera_matrix(config, auxiliary=False)
    matrix = matrix / matrix[2, 2]
    if (
        not np.array_equal(matrix[2], (0.0, 0.0, 1.0))
        or matrix[1, 0] != 0
        or min(matrix[0, 0], matrix[1, 1]) <= 0
    ):
        raise ValueError("main camera requires positive focal lengths and an intrinsic matrix")
    distortion = _distortion_coefficients(config, auxiliary=False)
    if not np.isfinite(distortion).all():
        raise ValueError("distortion coefficients must be finite")
    distorted = np.linalg.solve(matrix, np.append(pixel, 1.0))[:2]
    normalized = cv2.undistortPointsIter(
        distorted.reshape(1, 1, 2),
        np.eye(3),
        distortion,
        np.eye(3),
        np.eye(3),
        (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-12),
    ).reshape(2)
    optical = np.append(normalized, 1.0)
    projected, projection_jacobian = cv2.projectPoints(
        optical.reshape(1, 3), np.zeros(3), np.zeros(3), np.eye(3), distortion
    )
    projected_pixel = matrix[:2, :2] @ projected.reshape(2) + matrix[:2, 2]
    if not np.isfinite(normalized).all() or not np.allclose(
        projected_pixel, pixel, rtol=0, atol=1e-7
    ):
        raise ValueError("main lens inverse did not converge")
    lens_jacobian = matrix[:2, :2] @ projection_jacobian[:, 3:5]
    rotation = np.asarray(solution.rotation_enu_from_main, dtype=np.float64)
    origin = np.asarray(solution.main_camera_position_enu_m, dtype=np.float64)
    if (
        rotation.shape != (3, 3)
        or not np.isfinite(rotation).all()
        or not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0, atol=1e-10)
        or np.linalg.det(rotation) <= 0
        or origin.shape != (3,)
        or not np.isfinite(origin).all()
    ):
        raise ValueError("calibration requires a finite origin and proper rotation")
    length = float(np.linalg.norm(optical))
    local_direction = optical / length
    direction = rotation @ local_direction
    # u is calibrated camera-right projected onto the ray's tangent plane;
    # v = direction x u preserves camera-down at the principal point.
    tangent_u = rotation[:, 0] - direction * float(direction @ rotation[:, 0])
    tangent_u /= np.linalg.norm(tangent_u)
    tangent_v = np.cross(direction, tangent_u)
    basis = np.column_stack((tangent_u, tangent_v))
    unit_jacobian = (np.eye(3) - np.outer(local_direction, local_direction))[:, :2] / length
    jacobian = basis.T @ rotation @ unit_jacobian @ np.linalg.solve(lens_jacobian, np.eye(2))
    if not np.isfinite([direction, tangent_u, tangent_v]).all() or not np.isfinite(jacobian).all():
        raise ValueError("main ray geometry is nonfinite")
    return direction, basis, jacobian


@dataclass(frozen=True, kw_only=True)
class RayGenerationModule(ModuleShell):
    """Conditional first-order model with explicit pixel and residual covariances.

    Residual jitter/flex matrices are supplied in the reported right/down tangent
    basis, with an explicit caller-declared mutual/pixel-independence basis for
    nonzero terms. They are not installation or low-frequency calibration
    covariances. The fixed optical centre has no independent translation term;
    its installation error remains shared.
    """

    business_implemented = True
    policy: d.RayGenerationPolicy
    sequencer: EventSequencer

    def build(
        self,
        observation: m.Observation2D,
        config: m.StationConfig,
        calibration: m.CalibrationSnapshot,
    ) -> m.RayEvent | m.DiagnosticEvent:
        envelope = self.sequencer.next_envelope(m.EventType.RAY, config.config_digest)

        def diagnostic(category: str, *reasons: str) -> m.DiagnosticEvent:
            return m.DiagnosticEvent(
                envelope=replace(envelope, event_type=m.EventType.DIAGNOSTIC),
                related_frame_ref=observation.main_frame_ref,
                capture_time=observation.capture_time,
                category=category,
                reason_codes=reasons,
                related_config_ref=config.config_digest,
                related_calibration_ref=(
                    calibration.solution.solution_ref if calibration.solution is not None else None
                ),
            )

        frame = observation.main_frame_ref
        if (
            frame.camera_role is not m.CameraRole.MAIN
            or frame.station_id != config.station_id
            or frame.station_id != envelope.station_id
            or frame.station_session_id != envelope.station_session_id
            or not frame.frame_id
        ):
            return diagnostic("ray_input_invalid", "main_observation_scope_mismatch")
        try:
            _midpoint(observation.capture_time)
            reason = _calibration_reason(observation, config, calibration)
        except (TypeError, ValueError) as error:
            return diagnostic("ray_input_invalid", "invalid_capture_time", str(error))
        if reason is not None:
            return diagnostic("calibration_unavailable", reason, *calibration.reason_codes)
        if observation.capture_time.rolling_shutter_model != "none":
            return diagnostic("ray_uncertainty_unavailable", "rolling_shutter_model_unsupported")
        if self.policy.uncertainty_model_id != "tangent_gaussian_v1":
            return diagnostic("ray_uncertainty_unavailable", "uncertainty_model_unsupported")
        pixel_covariance = observation.centroid_covariance_px2
        pixel_source = "observation_pixel_covariance"
        if pixel_covariance is None:
            pixel_covariance = self.policy.fallback_centroid_covariance_px2
            pixel_source = "explicit_fallback_pixel_covariance"
        terms = (
            (pixel_covariance, "centroid_covariance_px2"),
            (self.policy.unmodelled_high_frequency_jitter_covariance_rad2, "residual_jitter_rad2"),
            (self.policy.unmodelled_relative_flex_covariance_rad2, "residual_flex_rad2"),
        )
        missing = tuple(f"unknown_{label}" for covariance, label in terms if covariance is None)
        if missing:
            return diagnostic("ray_uncertainty_unavailable", *missing)
        try:
            factors = tuple(
                covariance_factor(covariance, 2, label)
                for covariance, label in terms
                if covariance is not None
            )
            has_residual = any(np.any(factor) for factor in factors[1:])
            independence_basis = self.policy.residual_independence_basis
            if has_residual and (
                not isinstance(independence_basis, str) or not independence_basis.strip()
            ):
                return diagnostic(
                    "ray_uncertainty_unavailable", "residual_independence_basis_missing"
                )
            assert calibration.solution is not None
            direction, basis, jacobian = _geometry(observation, config, calibration.solution)
            pixel_factor = jacobian @ factors[0]
            covariance = pixel_factor @ pixel_factor.T + sum(
                factor @ factor.T for factor in factors[1:]
            )
            if not np.isfinite(covariance).all():
                raise ValueError("propagated covariance is nonfinite")
        except cv2.error as error:
            logger.exception("Main-image ray geometry failed in OpenCV")
            return diagnostic("ray_geometry_invalid", "opencv_ray_geometry_failed", str(error))
        except (TypeError, ValueError, np.linalg.LinAlgError) as error:
            return diagnostic("ray_geometry_invalid", "invalid_geometry_or_covariance", str(error))
        state, solution = calibration.state, calibration.solution
        assert state.last_trusted_time is not None and state.usable_interval is not None
        assert calibration.usage is not None
        return m.RayEvent(
            envelope=envelope,
            frame_id=frame.frame_id,
            local_track_id=observation.local_track_id,
            capture_time=observation.capture_time,
            world_frame_id=config.world_frame_id,
            calibration_solution_ref=solution.solution_ref,
            calibration_usage=calibration.usage,
            calibration_evidence=m.CalibrationUseEvidence(
                state_version=state.state_version,
                last_trusted_time=state.last_trusted_time,
                solution_accepted_time=solution.accepted_time,
                solution_reference_capture_time=solution.reference_capture_time,
                solution_valid_interval=solution.valid_interval,
                usable_interval=state.usable_interval,
                reason_codes=state.reason_codes,
            ),
            ray_origin_enu_m=solution.main_camera_position_enu_m,
            ray_direction_enu_unit=tuple(float(value) for value in direction),  # type: ignore[arg-type]
            uncertainty=m.RayUncertainty(
                model=self.policy.uncertainty_model_id,
                tangent_u_enu_unit=tuple(float(value) for value in basis[:, 0]),  # type: ignore[arg-type]
                tangent_v_enu_unit=tuple(float(value) for value in basis[:, 1]),  # type: ignore[arg-type]
                independent_angular_covariance_rad2=(
                    (float(covariance[0, 0]), float(covariance[0, 1])),
                    (float(covariance[1, 0]), float(covariance[1, 1])),
                ),
                independent_origin_covariance_m2=ZERO_ORIGIN_COVARIANCE,
                shared_installation_state_ref=config.installation_uncertainty.state_ref,
                shared_calibration_solution_ref=solution.solution_ref,
            ),
            detection_score=observation.detection_score,
            detection_score_definition=observation.detection_score_definition,
            track_quality=observation.track_quality,
            track_quality_definition=observation.track_quality_definition,
            quality_flags=(
                pixel_source,
                "first_order_pixel_propagation",
                *(
                    (
                        "residual_jitter_flex_assumed_independent",
                        f"residual_independence_basis:{independence_basis}",
                    )
                    if has_residual
                    else ("explicit_zero_residual_jitter_and_flex",)
                ),
                "conditional_fixed_optical_center",
                *state.reason_codes,
            ),
        )
