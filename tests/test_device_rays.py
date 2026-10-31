"""Independent geometry checks; all uncertainty values here are test fixtures."""

from __future__ import annotations

import unittest
from dataclasses import replace

import cv2
import numpy as np

from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.device import EventSequencerModule, RayGenerationModule
from tests.test_calibration import IDENTITY, SCORE, ZERO_COVARIANCE, _config

ZERO2: m.Matrix2x2 = ((0.0, 0.0), (0.0, 0.0))
PIXEL_COVARIANCE: m.Matrix2x2 = ((4.0, 1.0), (1.0, 9.0))
SESSION = m.SessionId("session-test")


def capture(time: int = 100) -> m.CaptureTime:
    return m.CaptureTime(
        clock_domain_id="test-clock",
        exposure_start_ns=time,
        exposure_end_ns=time,
        clock_uncertainty_ns=0,
        rolling_shutter_model="none",
    )


def policy() -> d.RayGenerationPolicy:
    return d.RayGenerationPolicy(
        uncertainty_model_id="tangent_gaussian_v1",
        fallback_centroid_covariance_px2=None,
        unmodelled_high_frequency_jitter_covariance_rad2=ZERO2,
        unmodelled_relative_flex_covariance_rad2=ZERO2,
    )


def observation(config: m.StationConfig) -> m.Observation2D:
    return m.Observation2D(
        main_frame_ref=m.FrameRef(
            station_id=config.station_id,
            station_session_id=SESSION,
            camera_role=m.CameraRole.MAIN,
            frame_id=m.FrameId("main-test"),
        ),
        capture_time=capture(),
        centroid_px=(320.0, 240.0),
        centroid_covariance_px2=PIXEL_COVARIANCE,
        detection_score=0.75,
        detection_score_definition=SCORE,
        local_track_id=m.LocalTrackId("local-measured"),
        track_quality=0.5,
        track_quality_definition=SCORE,
    )


def snapshot(
    config: m.StationConfig, observed: m.Observation2D, rotation: m.Matrix3x3 = IDENTITY
) -> m.CalibrationSnapshot:
    accepted = m.ProcessingTime(clock_domain_id="test-clock", time_ns=1)
    interval = m.TimeInterval(clock_domain_id="test-clock", start_ns=0, end_ns=None)
    solution = m.CalibrationSolution(
        solution_ref=m.CalibrationSolutionRef(
            station_id=config.station_id,
            station_session_id=SESSION,
            solution_id=m.CalibrationSolutionId("geometry-test"),
        ),
        station_config_digest=config.config_digest,
        world_frame_id=config.world_frame_id,
        reference_capture_time=capture(0),
        accepted_time=accepted,
        valid_interval=interval,
        main_camera_position_enu_m=config.main_camera_position_enu_m,
        rotation_enu_from_main=rotation,
        correction_covariance_enu_rad2=ZERO_COVARIANCE,
        quality=m.CalibrationQuality(
            source="test-fixture",
            matched_reference_count=24,
            reprojection_rmse_px=0.0,
            angular_residual_rad=0.0,
            reason_codes=(),
        ),
    )
    return m.CalibrationSnapshot(
        main_capture_time=observed.capture_time,
        state=m.CalibrationState(
            station_id=config.station_id,
            station_session_id=SESSION,
            station_config_digest=config.config_digest,
            state_version=1,
            health=m.CalibrationHealth.VALID,
            solution_ref=solution.solution_ref,
            observed_at=accepted,
            last_trusted_time=accepted,
            usable_interval=interval,
            reason_codes=(),
        ),
        solution=solution,
        usage=m.CalibrationUsage.VALID,
        reason_codes=(),
    )


def builder(
    config: m.StationConfig, ray_policy: d.RayGenerationPolicy | None = None
) -> RayGenerationModule:
    return RayGenerationModule(
        policy=ray_policy or policy(),
        sequencer=EventSequencerModule(
            station_id=config.station_id,
            station_session_id=SESSION,
            schema_version="device-test-v1",
        ),
    )


class MeasuredRayTests(unittest.TestCase):
    def test_principal_point_skew_rotation_and_explicit_residual_covariance(self) -> None:
        config = replace(
            _config(camera_matrix=((600.0, 70.0, 320.0), (0.0, 900.0, 240.0), (0.0, 0.0, 1.0))),
            main_camera_position_enu_m=(17.0, -5.0, 9.0),
        )
        rotation: m.Matrix3x3 = ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        observed = observation(config)
        jitter: m.Matrix2x2 = ((2e-7, 1e-8), (1e-8, 1e-7))
        flex: m.Matrix2x2 = ((1e-8, -2e-9), (-2e-9, 3e-8))
        ray_policy = replace(
            policy(),
            unmodelled_high_frequency_jitter_covariance_rad2=jitter,
            unmodelled_relative_flex_covariance_rad2=flex,
            residual_independence_basis="test-only independent pixel/jitter/flex model",
        )
        result = builder(config, ray_policy).build(
            observed, config, snapshot(config, observed, rotation)
        )
        self.assertIsInstance(result, m.RayEvent)
        assert isinstance(result, m.RayEvent)
        np.testing.assert_array_equal(result.ray_direction_enu_unit, (1.0, 0.0, 0.0))
        np.testing.assert_array_equal(result.uncertainty.tangent_u_enu_unit, (0.0, 1.0, 0.0))
        np.testing.assert_array_equal(result.uncertainty.tangent_v_enu_unit, (0.0, 0.0, 1.0))
        jacobian = np.array(((1 / 600, -70 / (600 * 900)), (0, 1 / 900)))
        expected = jacobian @ PIXEL_COVARIANCE @ jacobian.T + jitter + np.asarray(flex)
        np.testing.assert_allclose(
            result.uncertainty.independent_angular_covariance_rad2, expected, rtol=0, atol=1e-19
        )
        self.assertEqual(result.ray_origin_enu_m, config.main_camera_position_enu_m)
        self.assertEqual(result.uncertainty.independent_origin_covariance_m2, ZERO_COVARIANCE)
        self.assertEqual(result.local_track_id, observed.local_track_id)
        self.assertEqual(result.detection_score, observed.detection_score)
        self.assertIs(result.detection_score_definition, observed.detection_score_definition)
        self.assertEqual(result.track_quality, observed.track_quality)
        self.assertIs(result.track_quality_definition, observed.track_quality_definition)
        self.assertIn("conditional_fixed_optical_center", result.quality_flags)
        self.assertIn(
            f"residual_independence_basis:{ray_policy.residual_independence_basis}",
            result.quality_flags,
        )

    def test_brown_conrady_direction_and_covariance_match_analytic_forward_model(self) -> None:
        matrix = np.array(((800.0, 35.0, 320.0), (0.0, 650.0, 240.0), (0.0, 0.0, 1.0)))
        config = _config(camera_matrix=tuple(map(tuple, matrix)))  # type: ignore[arg-type]
        coefficients = (0.16, -0.04, 0.007, -0.009, 0.01)
        config = replace(
            config,
            main_camera=replace(
                config.main_camera,
                distortion=m.CameraDistortion(model="brown_conrady", coefficients=coefficients),
            ),
        )
        x, y = 0.25, -0.15
        k1, k2, p1, p2, k3 = coefficients
        r2 = x * x + y * y
        radial = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
        distorted = np.array(
            (
                x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
                y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y,
            )
        )
        pixel = matrix[:2, :2] @ distorted + matrix[:2, 2]
        observed = replace(observation(config), centroid_px=(float(pixel[0]), float(pixel[1])))
        rotation: m.Matrix3x3 = ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        result = builder(config).build(observed, config, snapshot(config, observed, rotation))
        self.assertIsInstance(result, m.RayEvent)
        assert isinstance(result, m.RayEvent)
        q = np.array((x, y, 1.0))
        length = np.linalg.norm(q)
        expected_direction = np.asarray(rotation) @ q / length
        np.testing.assert_allclose(
            result.ray_direction_enu_unit, expected_direction, rtol=0, atol=1e-13
        )
        radial_derivative = k1 + 2 * k2 * r2 + 3 * k3 * r2**2
        dx, dy = 2 * x * radial_derivative, 2 * y * radial_derivative
        forward = np.array(
            (
                (radial + x * dx + 2 * p1 * y + 6 * p2 * x, x * dy + 2 * p1 * x + 2 * p2 * y),
                (y * dx + 2 * p1 * x + 2 * p2 * y, radial + y * dy + 6 * p1 * y + 2 * p2 * x),
            )
        )
        unit_jacobian = (np.eye(3) / length - np.outer(q, q) / length**3)[:, :2]
        jacobian = np.asarray(rotation) @ unit_jacobian @ np.linalg.inv(matrix[:2, :2] @ forward)
        expected_covariance_enu = jacobian @ PIXEL_COVARIANCE @ jacobian.T
        basis = np.column_stack(
            (result.uncertainty.tangent_u_enu_unit, result.uncertainty.tangent_v_enu_unit)
        )
        actual_covariance_enu = (
            basis @ result.uncertainty.independent_angular_covariance_rad2 @ basis.T
        )
        np.testing.assert_allclose(
            actual_covariance_enu, expected_covariance_enu, rtol=0, atol=1e-18
        )
        np.testing.assert_allclose(basis.T @ basis, np.eye(2), rtol=0, atol=1e-14)
        np.testing.assert_allclose(
            np.cross(basis[:, 0], basis[:, 1]), expected_direction, rtol=0, atol=1e-13
        )

    def test_main_camera_supports_existing_opencv_coefficient_layouts(self) -> None:
        base = _config()
        optical = np.array((0.2, -0.1, 1.0))
        for coefficients in (
            (0.1, -0.03, 0.002, -0.004),
            (0.1, -0.03, 0.002, -0.004, 0.005),
            (0.1, -0.03, 0.002, -0.004, 0.005, 0.01, -0.02, 0.003),
            (0.1, -0.03, 0.002, -0.004, 0.005, 0.01, -0.02, 0.003, 0.001, -0.002, 0.003, -0.004),
            (
                0.1,
                -0.03,
                0.002,
                -0.004,
                0.005,
                0.01,
                -0.02,
                0.003,
                0.001,
                -0.002,
                0.003,
                -0.004,
                0.01,
                -0.02,
            ),
        ):
            with self.subTest(coefficient_count=len(coefficients)):
                config = replace(
                    base,
                    main_camera=replace(
                        base.main_camera,
                        distortion=m.CameraDistortion(model="opencv", coefficients=coefficients),
                    ),
                )
                pixel, _ = cv2.projectPoints(
                    optical.reshape(1, 3),
                    np.zeros(3),
                    np.zeros(3),
                    np.asarray(config.main_camera.intrinsics.matrix_px),
                    np.asarray(coefficients),
                )
                observed = replace(
                    observation(config), centroid_px=(float(pixel[0, 0, 0]), float(pixel[0, 0, 1]))
                )
                result = builder(config).build(observed, config, snapshot(config, observed))
                self.assertIsInstance(result, m.RayEvent)
                assert isinstance(result, m.RayEvent)
                np.testing.assert_allclose(
                    result.ray_direction_enu_unit,
                    optical / np.linalg.norm(optical),
                    rtol=0,
                    atol=1e-13,
                )

    def test_shared_covariances_are_referenced_not_added_to_independent_error(self) -> None:
        config = _config()
        observed = observation(config)
        original = snapshot(config, observed)
        first = builder(config).build(observed, config, original)
        large: m.Matrix3x3 = ((9.0, 0.0, 0.0), (0.0, 16.0, 0.0), (0.0, 0.0, 25.0))
        config = replace(
            config,
            installation_uncertainty=replace(
                config.installation_uncertainty,
                position_covariance_enu_m2=large,
                orientation_covariance_enu_rad2=large,
                fixed_extrinsic_covariance_aux_rad2=large,
            ),
        )
        assert original.solution is not None
        changed = replace(
            original, solution=replace(original.solution, correction_covariance_enu_rad2=large)
        )
        second = builder(config).build(observed, config, changed)
        assert isinstance(first, m.RayEvent) and isinstance(second, m.RayEvent)
        self.assertEqual(first.uncertainty, second.uncertainty)
        self.assertEqual(
            second.uncertainty.shared_installation_state_ref,
            config.installation_uncertainty.state_ref,
        )
        self.assertEqual(
            second.uncertainty.shared_calibration_solution_ref, original.solution.solution_ref
        )

    def test_unknown_terms_never_become_zero_and_fallback_is_explicit(self) -> None:
        config = _config()
        observed = observation(config)
        for term, candidate, ray_policy in (
            ("centroid_covariance_px2", replace(observed, centroid_covariance_px2=None), policy()),
            (
                "residual_jitter_rad2",
                observed,
                replace(policy(), unmodelled_high_frequency_jitter_covariance_rad2=None),
            ),
            (
                "residual_flex_rad2",
                observed,
                replace(policy(), unmodelled_relative_flex_covariance_rad2=None),
            ),
        ):
            with self.subTest(term=term):
                result = builder(config, ray_policy).build(
                    candidate, config, snapshot(config, candidate)
                )
                self.assertIsInstance(result, m.DiagnosticEvent)
                assert isinstance(result, m.DiagnosticEvent)
                self.assertEqual(result.category, "ray_uncertainty_unavailable")
                self.assertEqual(result.reason_codes, (f"unknown_{term}",))
        unknown = replace(observed, centroid_covariance_px2=None)
        result = builder(
            config, replace(policy(), fallback_centroid_covariance_px2=PIXEL_COVARIANCE)
        ).build(unknown, config, snapshot(config, unknown))
        assert isinstance(result, m.RayEvent)
        self.assertIn("explicit_fallback_pixel_covariance", result.quality_flags)

    def test_observation_covariance_wins_and_explicit_zero_has_no_noise_floor(self) -> None:
        config = _config()
        observed = replace(observation(config), centroid_covariance_px2=ZERO2)
        result = builder(
            config, replace(policy(), fallback_centroid_covariance_px2=PIXEL_COVARIANCE)
        ).build(observed, config, snapshot(config, observed))
        assert isinstance(result, m.RayEvent)
        self.assertEqual(result.uncertainty.independent_angular_covariance_rad2, ZERO2)
        self.assertIn("observation_pixel_covariance", result.quality_flags)

    def test_invalid_known_covariance_is_not_replaced_with_fallback(self) -> None:
        config = _config()
        for covariance in (
            ((-1.0, 0.0), (0.0, 1.0)),
            ((1.0, 1.0), (0.0, 1.0)),
            ((float("nan"), 0.0), (0.0, 1.0)),
        ):
            with self.subTest(covariance=covariance):
                observed = replace(observation(config), centroid_covariance_px2=covariance)
                result = builder(
                    config, replace(policy(), fallback_centroid_covariance_px2=ZERO2)
                ).build(observed, config, snapshot(config, observed))
                self.assertIsInstance(result, m.DiagnosticEvent)

    def test_future_incompatible_or_expired_calibration_is_not_used(self) -> None:
        config = _config()
        observed = observation(config)
        original = snapshot(config, observed)
        state, solution = original.state, original.solution
        assert solution is not None and state.last_trusted_time is not None
        future = m.ProcessingTime(clock_domain_id="test-clock", time_ns=101)
        expired = m.TimeInterval(clock_domain_id="test-clock", start_ns=0, end_ns=100)
        alternatives = (
            replace(original, solution=None, usage=None),
            replace(original, main_capture_time=capture(99)),
            replace(original, solution=replace(solution, accepted_time=future)),
            replace(original, solution=replace(solution, reference_capture_time=capture(101))),
            replace(original, state=replace(state, observed_at=future)),
            replace(original, state=replace(state, last_trusted_time=future)),
            replace(original, solution=replace(solution, valid_interval=expired)),
            replace(original, state=replace(state, usable_interval=expired)),
            replace(original, state=replace(state, last_trusted_time=None)),
            replace(original, state=replace(state, health=m.CalibrationHealth.INVALID)),
            replace(original, state=replace(state, station_session_id=m.SessionId("other"))),
            replace(original, solution=replace(solution, world_frame_id=m.WorldFrameId("other"))),
            replace(
                original,
                solution=replace(
                    solution, accepted_time=replace(solution.accepted_time, clock_domain_id="other")
                ),
            ),
            replace(
                original, solution=replace(solution, main_camera_position_enu_m=(1.0, 0.0, 0.0))
            ),
        )
        for index, alternative in enumerate(alternatives):
            with self.subTest(index=index):
                result = builder(config).build(observed, config, alternative)
                self.assertIsInstance(result, m.DiagnosticEvent)
                assert isinstance(result, m.DiagnosticEvent)
                self.assertEqual(result.category, "calibration_unavailable")

    def test_frozen_stale_and_recovering_keep_exposure_evidence(self) -> None:
        config = _config()
        observed = observation(config)
        original = snapshot(config, observed)
        for health, usage in (
            (m.CalibrationHealth.FROZEN, m.CalibrationUsage.FROZEN),
            (m.CalibrationHealth.STALE, m.CalibrationUsage.STALE),
            (m.CalibrationHealth.RECOVERING, m.CalibrationUsage.FROZEN),
        ):
            with self.subTest(health=health):
                state = replace(
                    original.state, health=health, state_version=2, reason_codes=(health.value,)
                )
                result = builder(config).build(
                    observed, config, replace(original, state=state, usage=usage)
                )
                assert isinstance(result, m.RayEvent)
                self.assertEqual(result.calibration_usage, usage)
                self.assertEqual(result.calibration_evidence.state_version, 2)
                self.assertEqual(result.calibration_evidence.reason_codes, (health.value,))

    def test_bad_geometry_and_unsupported_models_produce_diagnostics(self) -> None:
        config = _config()
        observed = observation(config)
        for pixel in ((float("nan"), 240.0), (-1.0, 240.0), (640.0, 240.0), (320.0, 480.0)):
            with self.subTest(pixel=pixel):
                candidate = replace(observed, centroid_px=pixel)
                self.assertIsInstance(
                    builder(config).build(candidate, config, snapshot(config, candidate)),
                    m.DiagnosticEvent,
                )
        unsupported = replace(
            config,
            main_camera=replace(
                config.main_camera, distortion=m.CameraDistortion(model="unknown", coefficients=())
            ),
        )
        self.assertIsInstance(
            builder(unsupported).build(observed, unsupported, snapshot(unsupported, observed)),
            m.DiagnosticEvent,
        )
        for field in ("unknown", "rolling-shutter-v1"):
            candidate = replace(
                observed, capture_time=replace(observed.capture_time, rolling_shutter_model=field)
            )
            self.assertIsInstance(
                builder(config).build(candidate, config, snapshot(config, candidate)),
                m.DiagnosticEvent,
            )
        self.assertIsInstance(
            builder(config, replace(policy(), uncertainty_model_id="unknown")).build(
                observed, config, snapshot(config, observed)
            ),
            m.DiagnosticEvent,
        )

    def test_nonzero_residuals_require_an_explicit_independence_basis(self) -> None:
        config = _config()
        observed = observation(config)
        for basis in (None, "", "  "):
            with self.subTest(basis=basis):
                ray_policy = replace(
                    policy(),
                    unmodelled_high_frequency_jitter_covariance_rad2=((1e-7, 0.0), (0.0, 1e-7)),
                    residual_independence_basis=basis,
                )
                result = builder(config, ray_policy).build(
                    observed, config, snapshot(config, observed)
                )
                self.assertIsInstance(result, m.DiagnosticEvent)
                assert isinstance(result, m.DiagnosticEvent)
                self.assertEqual(result.reason_codes, ("residual_independence_basis_missing",))

    def test_ray_and_diagnostic_share_one_session_sequence(self) -> None:
        config = _config()
        observed = observation(config)
        rays = builder(config)
        outputs = (
            rays.build(observed, config, snapshot(config, observed)),
            rays.build(
                replace(observed, centroid_covariance_px2=None), config, snapshot(config, observed)
            ),
            rays.build(observed, config, snapshot(config, observed)),
        )
        self.assertEqual([event.envelope.sequence_no for event in outputs], [1, 2, 3])
        self.assertEqual(
            [event.envelope.event_type for event in outputs],
            [m.EventType.RAY, m.EventType.DIAGNOSTIC, m.EventType.RAY],
        )


if __name__ == "__main__":
    unittest.main()
