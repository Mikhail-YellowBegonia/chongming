"""Algorithm adapters and read-only truth sampling, without a renderer or scoring."""

from __future__ import annotations

import json
import math
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from chongming.composition.center import declare_center
from chongming.composition.development import development_run
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts.development import (
    CenterAlgorithmResult,
    CenterInputStep,
    CenterRegistration,
)
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.visualization import AlgorithmOutputUpdate
from chongming.reference import FusionEngineAdapter
from chongming.scenarios.demo import demo_source, optical_rotation
from chongming.scenarios.serialization import load_scenario
from chongming.visualization import (
    center_fusion_event,
    diagnostic_summary,
    edge_station_event,
    measurement_visualization,
    ray_visualization,
    station_visualization_state,
    track_visualization,
    truth_comparison_snapshot,
    truth_snapshots,
    truth_times,
)

ROOT = Path(__file__).resolve().parents[1]


class VisualizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_scenario(ROOT / "scenarios/center_clean.json")
        self.source = demo_source(self.spec, video_enabled=False)
        self.step = next(iter(self.source.generate(self.spec)))
        self.ray = self.step.packets.rays[0]
        run = development_run(self.spec, Path("unused-visualization-test-output"))
        center, _ = declare_center(
            center_session_id=run.center_session_id,
            policy=run.fusion_policy,
            clock_mapping=run.processing_to_capture_clock,
        )
        adapter = FusionEngineAdapter(center.fusion)
        self.now = m.ProcessingTime(
            clock_domain_id=self.step.packets.sample_time.clock_domain_id,
            time_ns=self.step.packets.sample_time.time_ns,
        )
        adapter.start(
            CenterRegistration(
                stations=self.spec.station_configs,
                center_session_id=str(run.center_session_id),
                policy=run.fusion_policy,
                clock_mapping=run.processing_to_capture_clock,
                started_at=self.now,
            )
        )
        self.addCleanup(adapter.close)
        events: tuple[m.StationEvent, ...] = (
            *self.step.packets.calibration_events,
            *self.step.packets.rays,
        )
        self.result = adapter.process(
            CenterInputStep(
                step_no=0,
                time=self.now,
                solutions=self.step.packets.calibration_solutions,
                events=tuple(
                    m.ReceivedEvent(event=event, arrival_time=self.now) for event in events
                ),
                progress=tuple(
                    StationEventTimeProgress(
                        station_id=binding.station_id,
                        station_session_id=binding.virtual_session_id,
                        station_config_digest=config.config_digest,
                        completed_capture_time=self.ray.capture_time,
                        received_at=self.now,
                        stream_ended=True,
                    )
                    for binding, config in zip(
                        self.source.station_sessions, self.spec.station_configs, strict=True
                    )
                ),
                stream_ended=True,
            )
        )
        self.measurement = self.result.measurements[0]
        self.track = self.result.tracks[0]

    def update(self) -> AlgorithmOutputUpdate:
        return AlgorithmOutputUpdate(
            update_id=7,
            scenario_time_ns=2_000_000_000,
            step_no=11,
            edge_events=(
                edge_station_event(
                    station_id=self.ray.envelope.station_id,
                    station_session_id=self.ray.envelope.station_session_id,
                    events=(self.ray,),
                ),
            ),
            center_events=center_fusion_event(self.result),
            station_states=None,
        )

    def diagnostic(self) -> m.DiagnosticEvent:
        return m.DiagnosticEvent(
            envelope=replace(self.ray.envelope, event_type=m.EventType.DIAGNOSTIC, sequence_no=91),
            related_frame_ref=None,
            capture_time=self.ray.capture_time,
            category="calibration_lost",
            reason_codes=("reference_unavailable", "no_usable_solution"),
            related_config_ref=self.ray.envelope.station_config_digest,
            related_calibration_ref=None,
        )

    def test_ray_fields_midpoint_and_major_independent_angular_std(self) -> None:
        ray = replace(
            self.ray,
            capture_time=replace(self.ray.capture_time, exposure_start_ns=10, exposure_end_ns=15),
            uncertainty=replace(
                self.ray.uncertainty,
                independent_angular_covariance_rad2=((5e-6, 4e-6), (4e-6, 5e-6)),
            ),
        )
        for usage in m.CalibrationUsage:
            with self.subTest(usage=usage):
                output = ray_visualization(replace(ray, calibration_usage=usage))
                self.assertEqual(
                    output.event_id,
                    (
                        ray.envelope.station_id,
                        ray.envelope.station_session_id,
                        ray.envelope.sequence_no,
                    ),
                )
                self.assertEqual(output.capture_time_ns, 12)
                self.assertEqual(output.origin_enu_m, ray.ray_origin_enu_m)
                self.assertEqual(output.direction_enu_unit, ray.ray_direction_enu_unit)
                self.assertIs(output.calibration_usage, usage)
                self.assertEqual(output.detection_score, ray.detection_score)
                assert output.angular_std_rad is not None
                self.assertAlmostEqual(output.angular_std_rad, 0.003)
        zero = replace(
            ray,
            uncertainty=replace(
                ray.uncertainty, independent_angular_covariance_rad2=((0.0, 0.0), (0.0, 0.0))
            ),
        )
        self.assertEqual(ray_visualization(zero).angular_std_rad, 0.0)

    def test_invalid_ray_score_and_covariance_are_not_clamped(self) -> None:
        for score in (-0.1, 1.1, math.nan):
            with self.subTest(score=score), self.assertRaisesRegex(ValueError, "detection_score"):
                ray_visualization(replace(self.ray, detection_score=score))
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            ray_visualization(
                replace(
                    self.ray,
                    uncertainty=replace(
                        self.ray.uncertainty,
                        independent_angular_covariance_rad2=((-1.0, 0.0), (0.0, 1.0)),
                    ),
                )
            )

    def test_edge_diagnostics_empty_output_and_station_session_scope(self) -> None:
        diagnostic = self.diagnostic()
        output = edge_station_event(
            station_id=self.ray.envelope.station_id,
            station_session_id=self.ray.envelope.station_session_id,
            events=(self.ray, diagnostic, self.step.packets.calibration_events[0]),
        )
        self.assertEqual(len(output.rays), 1)
        self.assertEqual(output.diagnostics[0].category, diagnostic.category)
        self.assertEqual(output.diagnostics[0].reason_codes, diagnostic.reason_codes)
        self.assertEqual(output.diagnostics[0].capture_time_ns, output.rays[0].capture_time_ns)
        empty = edge_station_event(
            station_id=output.station_id, station_session_id=output.station_session_id, events=()
        )
        self.assertEqual((empty.rays, empty.diagnostics), ((), ()))
        with self.assertRaisesRegex(ValueError, "without capture_time"):
            diagnostic_summary(replace(diagnostic, capture_time=None))
        with self.assertRaisesRegex(ValueError, "without capture_time"):
            edge_station_event(
                station_id=output.station_id,
                station_session_id=output.station_session_id,
                events=(self.ray, replace(diagnostic, capture_time=None)),
            )
        for other in (
            replace(self.ray.envelope, station_id=m.StationId("other")),
            replace(self.ray.envelope, station_session_id=m.SessionId("other")),
        ):
            with (
                self.subTest(envelope=other),
                self.assertRaisesRegex(ValueError, "one station/session"),
            ):
                edge_station_event(
                    station_id=output.station_id,
                    station_session_id=output.station_session_id,
                    events=(replace(self.ray, envelope=other),),
                )

    def test_measurement_units_covariance_references_and_unknowns(self) -> None:
        measurement = replace(
            self.measurement,
            conditional_position_covariance_enu_m2=(
                (4.0, 1.0, 0.0),
                (1.0, 9.0, 0.0),
                (0.0, 0.0, 0.0),
            ),
            minimum_intersection_angle_rad=math.pi / 6,
            geometry_condition=12.5,
        )
        output = measurement_visualization(measurement)
        self.assertEqual(output.measurement_id, measurement.measurement_id)
        self.assertEqual(output.reference_time_ns, measurement.reference_time_ns)
        self.assertEqual(output.position_enu_m, measurement.position_enu_m)
        self.assertEqual(output.position_std_enu_m, (2.0, 3.0, 0.0))
        self.assertEqual(output.supporting_ray_ids, measurement.supporting_ray_event_ids)
        self.assertEqual(output.supporting_station_ids, measurement.supporting_station_ids)
        assert output.minimum_intersection_angle_deg is not None
        self.assertAlmostEqual(output.minimum_intersection_angle_deg, 30)
        self.assertEqual(output.geometry_condition, 12.5)
        unknown = measurement_visualization(
            replace(
                measurement,
                conditional_position_covariance_enu_m2=None,
                minimum_intersection_angle_rad=None,
                geometry_condition=None,
            )
        )
        self.assertIsNone(unknown.position_std_enu_m)
        self.assertIsNone(unknown.minimum_intersection_angle_deg)
        self.assertIsNone(unknown.geometry_condition)
        missing = measurement_visualization(
            replace(measurement, position_enu_m=None, conditional_position_covariance_enu_m2=None)
        )
        self.assertIsNone(missing.position_enu_m)
        self.assertIsNone(missing.position_std_enu_m)
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            measurement_visualization(
                replace(
                    measurement,
                    conditional_position_covariance_enu_m2=(
                        (-1.0, 0.0, 0.0),
                        (0.0, 1.0, 0.0),
                        (0.0, 0.0, 1.0),
                    ),
                )
            )
        for status in m.FusionStatus:
            if status is m.FusionStatus.LOCATED:
                continue
            with self.subTest(status=status):
                failed = measurement_visualization(
                    replace(
                        measurement,
                        status=status,
                        position_enu_m=None,
                        conditional_position_covariance_enu_m2=None,
                    )
                )
                self.assertIs(failed.status, status)
                self.assertIsNone(failed.position_enu_m)
                self.assertIsNone(failed.position_std_enu_m)
                self.assertEqual(failed.supporting_ray_ids, output.supporting_ray_ids)

    def test_track_covariance_blocks_lifecycles_and_history_point(self) -> None:
        covariance: m.Matrix6x6 = (
            (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            (0.0, 4.0, 0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 9.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 16.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0, 25.0, 0.0),
            (0.0, 0.0, 0.0, 0.0, 0.0, 36.0),
        )
        for lifecycle in m.TrackLifecycle:
            track = replace(
                self.track,
                lifecycle=lifecycle,
                velocity_enu_mps=(2.0, 3.0, 4.0),
                conditional_state_covariance=covariance,
            )
            with self.subTest(lifecycle=lifecycle):
                output = track_visualization(track)
                self.assertEqual(output.track_id, track.track_id)
                self.assertIs(output.lifecycle, lifecycle)
                self.assertEqual(output.event_time_ns, track.event_time_ns)
                self.assertEqual(output.position_enu_m, track.position_enu_m)
                self.assertEqual(output.velocity_enu_mps, track.velocity_enu_mps)
                self.assertEqual(output.position_std_enu_m, (1.0, 2.0, 3.0))
                self.assertEqual(output.velocity_std_enu_mps, (4.0, 5.0, 6.0))
                self.assertEqual(output.recent_position_enu_m, track.position_enu_m)
                self.assertEqual(output.recent_time_ns, track.event_time_ns)
        tentative = track_visualization(self.track)
        self.assertIsNone(tentative.velocity_enu_mps)
        self.assertIsNone(tentative.position_std_enu_m)
        self.assertIsNone(tentative.velocity_std_enu_mps)
        absent = track_visualization(replace(self.track, position_enu_m=None))
        self.assertIsNone(absent.recent_position_enu_m)
        self.assertIsNone(absent.recent_time_ns)

    def test_real_center_outputs_are_not_repeated_from_stage_batches(self) -> None:
        before = asdict(self.result)
        output = center_fusion_event(self.result)
        self.assertEqual(len(output.measurements), len(self.result.measurements))
        self.assertEqual(len(output.tracks), len(self.result.tracks))
        self.assertEqual(output.window_count, 1)
        self.assertGreater(output.candidate_count, 0)
        self.assertEqual(output.association_count, 1)
        self.assertIsNone(output.voxel_space)
        self.assertEqual(asdict(self.result), before)
        # Even with stage outputs present, never append them to flattened results.
        stages_only = center_fusion_event(replace(self.result, measurements=(), tracks=()))
        self.assertEqual((stages_only.measurements, stages_only.tracks), ((), ()))
        doubled = center_fusion_event(replace(self.result, batches=self.result.batches * 2))
        self.assertEqual(doubled.measurements, output.measurements)
        self.assertEqual(doubled.tracks, output.tracks)
        self.assertEqual(
            (doubled.window_count, doubled.candidate_count, doubled.association_count),
            (2, output.candidate_count * 2, 2),
        )
        empty = center_fusion_event(CenterAlgorithmResult(measurements=(), tracks=(), batches=()))
        self.assertEqual(
            (empty.window_count, empty.candidate_count, empty.association_count), (0, 0, 0)
        )
        encoded = json.dumps(asdict(self.update()), allow_nan=False)
        self.assertIn('"calibration_usage": "valid"', encoded)
        self.assertIn('"status": "located"', encoded)
        self.assertNotIn("truth_object_id", encoded)
        self.assertNotIn("local_track_id", encoded)

    def test_multiple_windows_keep_measurements_and_repeated_track_ids_in_order(self) -> None:
        later_measurement = replace(
            self.measurement,
            measurement_id=m.MeasurementId("later"),
            reference_time_ns=self.measurement.reference_time_ns + 10,
        )
        later_track = replace(self.track, event_time_ns=self.track.event_time_ns + 10)
        result = replace(
            self.result,
            measurements=(self.measurement, later_measurement),
            tracks=(self.track, later_track),
        )
        output = center_fusion_event(result)
        self.assertEqual(
            tuple(item.measurement_id for item in output.measurements),
            (self.measurement.measurement_id, later_measurement.measurement_id),
        )
        self.assertEqual(
            tuple(item.reference_time_ns for item in output.measurements),
            (self.measurement.reference_time_ns, later_measurement.reference_time_ns),
        )
        self.assertEqual(
            tuple(item.track_id for item in output.tracks),
            (self.track.track_id, self.track.track_id),
        )
        self.assertEqual(
            tuple(item.event_time_ns for item in output.tracks),
            (self.track.event_time_ns, later_track.event_time_ns),
        )

    def test_station_pose_uses_configured_camera_axes_and_preserves_unknowns(self) -> None:
        config = self.spec.station_configs[0]
        camera = next(
            camera
            for camera in self.step.truth.logical_scene.cameras
            if camera.station_id == config.station_id and camera.camera_role is m.CameraRole.MAIN
        )
        configured = optical_rotation(0.7, 0.3)
        angle = 0.02
        local_rotation = np.array(
            [
                [math.cos(angle), -math.sin(angle), 0],
                [math.sin(angle), math.cos(angle), 0],
                [0, 0, 1],
            ]
        )
        actual: m.Rotation3x3 = tuple(
            tuple(float(x) for x in row) for row in np.asarray(configured) @ local_rotation
        )  # type: ignore[assignment]
        camera = replace(
            camera, optical_state=replace(camera.optical_state, rotation_enu_from_body=actual)
        )
        config = replace(config, baseline_orientation_enu_from_main=configured)
        solution = replace(
            next(
                item
                for item in self.step.packets.calibration_solutions
                if item.solution_ref.station_id == config.station_id
            ),
            rotation_enu_from_main=actual,
        )
        calibration = m.CalibrationSnapshot(
            main_capture_time=self.ray.capture_time,
            state=next(
                item
                for item in self.step.packets.calibration_states
                if item.station_id == config.station_id
            ),
            solution=solution,
            usage=m.CalibrationUsage.FROZEN,
            reason_codes=(),
        )
        output = station_visualization_state(config, actual_camera=camera, calibration=calibration)
        self.assertEqual(output.configured_orientation_enu_from_main, configured)
        self.assertEqual(output.actual_orientation_enu_from_main, actual)
        self.assertEqual(output.calibrated_orientation_enu_from_main, actual)
        self.assertIs(output.calibration_usage, m.CalibrationUsage.FROZEN)
        assert output.truth_orientation_error_rad is not None
        assert output.calibration_orientation_error_rad is not None
        np.testing.assert_allclose(
            np.asarray(output.truth_orientation_error_rad),
            np.asarray((0, 0, angle)),
            rtol=0,
            atol=1e-14,
        )
        np.testing.assert_allclose(
            np.asarray(output.calibration_orientation_error_rad),
            np.asarray((0, 0, angle)),
            rtol=0,
            atol=1e-14,
        )
        unknown = station_visualization_state(config, actual_camera=None, calibration=None)
        self.assertIsNone(unknown.actual_orientation_enu_from_main)
        self.assertIsNone(unknown.calibrated_orientation_enu_from_main)
        self.assertIsNone(unknown.truth_orientation_error_rad)
        self.assertIsNone(unknown.calibration_orientation_error_rad)
        self.assertIsNone(unknown.calibration_usage)
        initializing = station_visualization_state(
            config,
            actual_camera=camera,
            calibration=replace(calibration, solution=None, usage=None),
        )
        self.assertEqual(initializing.actual_orientation_enu_from_main, actual)
        self.assertIsNone(initializing.calibrated_orientation_enu_from_main)
        self.assertIsNone(initializing.calibration_orientation_error_rad)
        with self.assertRaisesRegex(ValueError, "another station"):
            station_visualization_state(
                config,
                actual_camera=camera,
                calibration=replace(
                    calibration, state=replace(calibration.state, station_id=m.StationId("other"))
                ),
            )
        with self.assertRaisesRegex(ValueError, "main camera truth"):
            station_visualization_state(
                config,
                actual_camera=replace(camera, camera_role=m.CameraRole.AUXILIARY),
                calibration=None,
            )

    def test_station_pose_retains_sub_arcsecond_drift_and_half_turn(self) -> None:
        config = replace(
            self.spec.station_configs[0],
            baseline_orientation_enu_from_main=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )
        camera = self.step.truth.logical_scene.cameras[1]
        for angle in (0.0, 1e-8, math.pi):
            rotation: m.Rotation3x3 = (
                (1.0, 0.0, 0.0),
                (0.0, math.cos(angle), -math.sin(angle)),
                (0.0, math.sin(angle), math.cos(angle)),
            )
            actual = replace(
                camera,
                station_id=config.station_id,
                camera_role=m.CameraRole.MAIN,
                optical_state=replace(camera.optical_state, rotation_enu_from_body=rotation),
            )
            output = station_visualization_state(config, actual_camera=actual, calibration=None)
            assert output.truth_orientation_error_rad is not None
            np.testing.assert_allclose(
                np.asarray(output.truth_orientation_error_rad),
                np.asarray((angle, 0, 0)),
                rtol=0,
                atol=1e-14,
            )

    def test_truth_uses_reference_point_velocity_main_visibility_and_geometry(self) -> None:
        scene = self.step.truth.logical_scene
        target = scene.flying_objects[0]
        shifted = replace(
            target,
            geometry=s.BoxGeometry((1.0, 2.0, 2.0)),
            center_reference_point_enu_m=(
                target.state.position_enu_m[0] + 1,
                *target.state.position_enu_m[1:],
            ),
            state=replace(
                target.state,
                rotation_enu_from_body=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                velocity_enu_mps=(1.0, 2.0, 3.0),
                angular_velocity_body_radps=(0.0, 0.0, 2.0),
            ),
        )
        scene = replace(
            scene,
            target_visibility=tuple(
                replace(
                    visibility,
                    projection=replace(visibility.projection, center_in_field_of_view=True),
                    capture_eligible=False,
                )
                if visibility.camera_role is m.CameraRole.MAIN
                else visibility
                for visibility in scene.target_visibility
            ),
        )
        snapshot = truth_comparison_snapshot(replace(scene, flying_objects=(shifted,)))
        output = snapshot.targets[0]
        self.assertEqual(output.position_enu_m, shifted.center_reference_point_enu_m)
        self.assertEqual(output.velocity_enu_mps, (1.0, 4.0, 3.0))
        self.assertEqual(output.geometry_type, "box")
        self.assertAlmostEqual(output.bounding_radius_m, math.sqrt(12))
        self.assertEqual(output.time_ns, scene.sample_time.time_ns)
        self.assertEqual(output.target_id, str(shifted.truth_object_id))
        main = [
            visibility
            for visibility in scene.target_visibility
            if visibility.truth_object_id == target.truth_object_id
            and visibility.camera_role is m.CameraRole.MAIN
        ]
        self.assertEqual(len(output.visibility_by_station), len(main))
        for actual, expected in zip(output.visibility_by_station, main, strict=True):
            self.assertEqual(actual.station_id, expected.station_id)
            self.assertEqual(actual.in_field_of_view, expected.projection.center_in_field_of_view)
            self.assertEqual(actual.capture_eligible, expected.capture_eligible)
        for geometry, kind, radius in (
            (s.SphereGeometry(3), "sphere", 3),
            (s.TriangleMeshGeometry(((0.0, 3.0, 4.0), (0.0, 0.0, 0.0)), ()), "aircraft", 5),
        ):
            item = replace(
                shifted,
                geometry=geometry,
                center_reference_point_enu_m=shifted.state.position_enu_m,
            )
            converted = truth_comparison_snapshot(replace(scene, flying_objects=(item,))).targets[0]
            self.assertEqual(converted.geometry_type, kind)
            self.assertEqual(converted.bounding_radius_m, radius)
        aircraft = replace(shifted, geometry=s.ExternalAssetGeometry("f86"))
        external = truth_comparison_snapshot(replace(scene, flying_objects=(aircraft,))).targets[0]
        self.assertEqual(external.geometry_type, "aircraft")
        self.assertGreater(external.bounding_radius_m, 0)
        self.assertEqual(truth_comparison_snapshot(replace(scene, flying_objects=())).targets, ())

    def test_truth_times_are_sorted_deduplicated_and_ignore_other_event_times(self) -> None:
        update = self.update()
        assert update.edge_events is not None and update.center_events is not None
        station = update.edge_events[0]
        ray = replace(station.rays[0], capture_time_ns=100)
        measurement = replace(update.center_events.measurements[0], reference_time_ns=200)
        update = replace(
            update,
            scenario_time_ns=300,
            edge_events=(
                replace(
                    station,
                    rays=(ray, ray),
                    diagnostics=(
                        replace(diagnostic_summary(self.diagnostic()), capture_time_ns=50),
                    ),
                ),
            ),
            center_events=replace(
                update.center_events,
                measurements=(measurement, measurement),
                tracks=(replace(update.center_events.tracks[0], event_time_ns=400),),
            ),
        )
        self.assertEqual(truth_times(update), (100, 200, 300))
        self.assertEqual(truth_times(replace(update, edge_events=None, center_events=None)), (300,))

    def test_truth_sampling_is_time_aligned_read_only_and_preserves_empty_slots(self) -> None:
        update = self.update()
        assert update.center_events is not None
        update = replace(
            update,
            center_events=replace(
                update.center_events,
                measurements=(
                    replace(update.center_events.measurements[0], reference_time_ns=1_000_000_000),
                ),
            ),
        )
        before = self.source.clock.read()
        with (
            patch.object(
                self.source.clock, "advance_to", side_effect=AssertionError("must not advance")
            ),
            patch.object(self.source.logic, "sample", wraps=self.source.logic.sample) as sample,
            patch.object(
                self.source.imager, "render", side_effect=AssertionError("must not render")
            ),
        ):
            snapshots = truth_snapshots(
                update, scene_logic=self.source.logic, clock_domain_id=before.clock_domain_id
            )
        self.assertEqual(self.source.clock.read(), before)
        self.assertEqual(
            tuple(call.args[0].time_ns for call in sample.call_args_list), truth_times(update)
        )
        self.assertEqual(len(snapshots), 3)
        for time_ns, snapshot in zip(truth_times(update), snapshots, strict=True):
            self.assertTrue(snapshot.targets)
            self.assertEqual(snapshot.targets[0].time_ns, time_ns)
        self.assertNotEqual(
            snapshots[0].targets[0].position_enu_m, snapshots[-1].targets[0].position_enu_m
        )
        scene = self.step.truth.logical_scene

        def sample_with_gap(time: s.SimulationTime) -> s.LogicalSceneSnapshot:
            return replace(
                scene,
                sample_time=time,
                flying_objects=() if time.time_ns == 1_000_000_000 else scene.flying_objects,
            )

        with patch.object(self.source.logic, "sample", side_effect=sample_with_gap):
            snapshots = truth_snapshots(
                update, scene_logic=self.source.logic, clock_domain_id=before.clock_domain_id
            )
        self.assertEqual(len(snapshots), 3)
        self.assertTrue(snapshots[0].targets)
        self.assertEqual(snapshots[1].targets, ())
        self.assertTrue(snapshots[2].targets)


if __name__ == "__main__":
    unittest.main()
