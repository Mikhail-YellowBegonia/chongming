"""Ray error distributions and held calibration; these tests never start Godot."""

from __future__ import annotations

import itertools
import math
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.modules.device import EventSequencerModule
from chongming.modules.scene import VirtualObservationModule, _tangent_basis
from chongming.scenarios.demo import demo_source
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import load_scenario, validate_scenario_spec

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
ANGULAR: m.Matrix2x2 = ((4e-6, 1e-6), (1e-6, 9e-6))
ORIGIN: m.Matrix3x3 = ((0.04, 0.01, 0.0), (0.01, 0.09, 0.0), (0.0, 0.0, 0.0))


def generate(request: s.VirtualObservationRequest) -> s.VirtualObservationBatch:
    sequencers = tuple(
        EventSequencerModule(
            station_id=b.station_id,
            station_session_id=b.virtual_session_id,
            schema_version="event-v1",
        )
        for b in request.station_sessions
    )
    return VirtualObservationModule(sequencers).generate(request)[0]


def fixture() -> tuple[s.ScenarioSpec, s.VirtualObservationRequest]:
    spec = load_scenario(SCENARIOS / "center_clean.json")
    source = demo_source(spec, video_enabled=False)
    step = next(iter(source.generate(spec)))
    return spec, s.VirtualObservationRequest(
        spec.scenario_id,
        spec.random_seed,
        spec.virtual_observations,
        spec.station_configs,
        step.truth.camera_frames,
        source.station_sessions,
    )


def angle(left: m.Vec3, right: m.Vec3) -> float:
    return math.atan2(float(np.linalg.norm(np.cross(left, right))), float(np.dot(left, right)))


class RayErrorTests(unittest.TestCase):
    def test_clean_baseline_matches_truth_across_the_whole_interval(self) -> None:
        spec = load_scenario(SCENARIOS / "center_clean.json")
        self.assertFalse(spec.perturbations.camera_jitter)
        self.assertEqual(spec.virtual_observations.detection_probability, 1)
        self.assertEqual(spec.virtual_observations.false_alarms_per_camera_hour, 0)
        self.assertFalse(np.any(spec.virtual_observations.independent_angular_covariance_rad2))
        self.assertFalse(np.any(spec.virtual_observations.independent_origin_covariance_m2))
        source = demo_source(spec, video_enabled=False)
        count = 0
        for step in source.generate(spec):
            truth = {(r.station_id, r.truth_object_id): r for r in step.truth.rays}
            correspondences = {c.ray_event_id: c for c in step.truth.online_correspondences}
            for ray in step.packets.rays:
                e = ray.envelope
                correspondence = correspondences[e.station_id, e.station_session_id, e.sequence_no]
                assert correspondence.truth_object_id is not None
                expected = truth[e.station_id, correspondence.truth_object_id]
                self.assertEqual(ray.ray_origin_enu_m, expected.origin_enu_m)
                self.assertLess(
                    angle(ray.ray_direction_enu_unit, expected.direction_enu_unit), 1e-12
                )
                self.assertIs(ray.calibration_usage, m.CalibrationUsage.VALID)
                count += 1
        self.assertGreater(count, 1000)

    def test_noise_changes_geometry_repeats_and_ignores_frame_order(self) -> None:
        _, request = fixture()
        clean = generate(request)
        noisy = replace(
            request,
            generation_spec=replace(
                request.generation_spec,
                independent_angular_covariance_rad2=ANGULAR,
                independent_origin_covariance_m2=ORIGIN,
            ),
        )
        first = generate(noisy)
        self.assertEqual(first, generate(noisy))
        reversed_batch = generate(replace(noisy, frames=tuple(reversed(noisy.frames))))
        by_station = {r.envelope.station_id: r for r in reversed_batch.rays}
        for baseline, ray in zip(clean.rays, first.rays, strict=True):
            self.assertNotEqual(baseline.ray_origin_enu_m, ray.ray_origin_enu_m)
            self.assertGreater(
                angle(baseline.ray_direction_enu_unit, ray.ray_direction_enu_unit), 1e-8
            )
            self.assertEqual(ray, by_station[ray.envelope.station_id])
            changed_seed = generate(replace(noisy, random_seed=request.random_seed + 1))
            self.assertNotEqual(
                ray.ray_direction_enu_unit, changed_seed.rays[0].ray_direction_enu_unit
            )
        # Independent streams: changing origins cannot change angular samples.
        angular_only = generate(
            replace(
                noisy,
                generation_spec=replace(
                    noisy.generation_spec,
                    independent_origin_covariance_m2=((0.0, 0.0, 0.0),) * 3,
                ),
            )
        )
        self.assertEqual(
            [r.ray_direction_enu_unit for r in first.rays],
            [r.ray_direction_enu_unit for r in angular_only.rays],
        )
        self.assertEqual(clean.calibration_solutions, first.calibration_solutions)

    def test_sample_distribution_in_tangent_radians_and_world_metres(self) -> None:
        _, request = fixture()
        frame = next(f for f in request.frames if f.camera_role is m.CameraRole.MAIN)
        clean = generate(replace(request, frames=(frame,))).rays[0]
        count = 3000
        frames = tuple(
            replace(
                frame,
                frame_id=m.FrameId(f"sample-{index}"),
                capture_time=replace(
                    frame.capture_time,
                    exposure_start_ns=index * 1_000_000,
                    exposure_end_ns=index * 1_000_000 + 1000,
                ),
            )
            for index in range(count)
        )
        batch = generate(
            replace(
                request,
                frames=frames,
                generation_spec=replace(
                    request.generation_spec,
                    independent_angular_covariance_rad2=ANGULAR,
                    independent_origin_covariance_m2=ORIGIN,
                ),
            )
        )
        self.assertEqual(len(batch.rays), count)
        nominal = np.array(clean.ray_direction_enu_unit)
        basis = np.array(_tangent_basis(clean.ray_direction_enu_unit))
        directions = np.array([r.ray_direction_enu_unit for r in batch.rays])
        theta = np.arctan2(
            np.linalg.norm(np.cross(nominal, directions), axis=1), directions @ nominal
        )
        tangent = (directions @ basis.T) * (theta / np.sin(theta))[:, None]
        origins = np.array([r.ray_origin_enu_m for r in batch.rays]) - clean.ray_origin_enu_m
        for samples, covariance in ((tangent, np.array(ANGULAR)), (origins, np.array(ORIGIN))):
            np.testing.assert_allclose(
                samples.mean(axis=0), 0, atol=5 * math.sqrt(float(covariance.max()) / count)
            )
            np.testing.assert_allclose(
                np.cov(samples.T), covariance, atol=float(covariance.max()) * 0.08, rtol=0.08
            )
        self.assertTrue(np.all(origins[:, 2] == 0))  # rank-deficient covariance
        for ray in batch.rays[:20]:
            u, v = ray.uncertainty.tangent_u_enu_unit, ray.uncertainty.tangent_v_enu_unit
            np.testing.assert_allclose(np.cross(u, v), ray.ray_direction_enu_unit, atol=1e-12)
            self.assertAlmostEqual(float(np.dot(u, v)), 0)

    def test_invalid_covariances_fail_before_scene_generation(self) -> None:
        spec, request = fixture()
        invalid = (
            ((1e-8, 1e-9), (0.0, 1e-8)),
            ((-1e-30, 0.0), (0.0, 0.0)),
            ((1e-8, 2e-8), (2e-8, 1e-8)),
            ((math.nan, 0.0), (0.0, 0.0)),
        )
        for covariance in invalid:
            observations = replace(
                request.generation_spec, independent_angular_covariance_rad2=covariance
            )
            with self.subTest(covariance=covariance):
                with self.assertRaises(ValueError):
                    validate_scenario_spec(replace(spec, virtual_observations=observations))
                with self.assertRaisesRegex(ValueError, "covariance"):
                    generate(replace(request, generation_spec=observations))
        observations = replace(
            request.generation_spec, independent_origin_covariance_m2=((-1.0, 0.0, 0.0),) * 3
        )
        with self.assertRaises(ValueError):
            validate_scenario_spec(replace(spec, virtual_observations=observations))

    def test_jitter_holds_solution_with_shared_bound_and_frozen_evidence(self) -> None:
        spec = load_scenario(SCENARIOS / "photography.json")
        source = demo_source(spec, video_enabled=False)
        steps = list(itertools.islice(source.generate(spec), 50))
        solutions = [solution for step in steps for solution in step.packets.calibration_solutions]
        self.assertEqual(len(solutions), 1)
        solution = solutions[0]
        bound = float(np.linalg.eigvalsh(solution.correction_covariance_enu_rad2).min())
        self.assertGreater(bound, 0)
        errors = []
        for step in steps:
            for ray, truth in zip(step.packets.rays, step.truth.rays, strict=True):
                error = angle(ray.ray_direction_enu_unit, truth.direction_enu_unit)
                errors.append(error)
                self.assertLessEqual(error * error, bound + 1e-15)
                self.assertIs(ray.calibration_usage, m.CalibrationUsage.FROZEN)
                self.assertIn("virtual_untracked_camera_jitter", ray.quality_flags)
                self.assertNotIn("virtual_clean_baseline", ray.quality_flags)
                self.assertFalse(np.any(ray.uncertainty.independent_angular_covariance_rad2))
                self.assertEqual(ray.calibration_solution_ref, solution.solution_ref)
                self.assertEqual(ray.calibration_evidence.last_trusted_time, solution.accepted_time)
        self.assertGreater(max(errors), 1e-5)
        clean = replace(spec, perturbations=replace(spec.perturbations, camera_jitter=()))
        clean_steps = itertools.islice(demo_source(clean, video_enabled=False).generate(clean), 10)
        for step in clean_steps:
            for ray, truth in zip(step.packets.rays, step.truth.rays, strict=True):
                self.assertLess(angle(ray.ray_direction_enu_unit, truth.direction_enu_unit), 1e-12)
                self.assertIs(ray.calibration_usage, m.CalibrationUsage.VALID)

    def test_auxiliary_only_and_zero_amplitude_jitter_keep_clean_main_calibration(self) -> None:
        spec = load_scenario(SCENARIOS / "photography.json")
        auxiliary_only = replace(
            spec,
            perturbations=replace(
                spec.perturbations,
                camera_jitter=tuple(
                    replace(jitter, camera_role=m.CameraRole.AUXILIARY)
                    for jitter in spec.perturbations.camera_jitter
                    if jitter.camera_role is m.CameraRole.MAIN
                ),
            ),
        )
        zero_amplitude = replace(
            spec,
            perturbations=replace(
                spec.perturbations,
                camera_jitter=tuple(
                    replace(
                        jitter,
                        angular_amplitude_rad=(0.0, 0.0, 0.0),
                        pixel_amplitude_px=(0.0, 0.0),
                    )
                    for jitter in spec.perturbations.camera_jitter
                    if jitter.camera_role is m.CameraRole.MAIN
                ),
            ),
        )
        for candidate in (auxiliary_only, zero_amplitude):
            with self.subTest(jitter=candidate.perturbations.camera_jitter):
                steps = itertools.islice(
                    demo_source(candidate, video_enabled=False).generate(candidate), 10
                )
                for step in steps:
                    for ray, truth in zip(step.packets.rays, step.truth.rays, strict=True):
                        self.assertLess(
                            angle(ray.ray_direction_enu_unit, truth.direction_enu_unit), 1e-12
                        )
                        self.assertIs(ray.calibration_usage, m.CalibrationUsage.VALID)
                        self.assertNotIn("virtual_untracked_camera_jitter", ray.quality_flags)

    def test_truth_and_jitter_configuration_stay_out_of_center_input_events(self) -> None:
        spec = load_scenario(SCENARIOS / "photography.json")
        steps = itertools.islice(demo_source(spec, video_enabled=False).generate(spec), 10)
        for step in steps:
            self.assertTrue(step.truth.camera_frames)
            self.assertTrue(step.packets.rays)
            event_json = _json(
                {
                    "solutions": step.packets.calibration_solutions,
                    "calibration_events": step.packets.calibration_events,
                    "rays": step.packets.rays,
                    "diagnostics": step.packets.diagnostics,
                }
            )
            self.assertNotIn("truth_object_id", event_json)
            self.assertNotIn("actual_camera", event_json)
            self.assertNotIn('"camera_jitter"', event_json)
            self.assertNotIn("angular_amplitude_rad", event_json)


if __name__ == "__main__":
    unittest.main()
