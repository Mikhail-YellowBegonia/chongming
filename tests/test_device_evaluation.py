"""Analytical metric oracles and measured-only workbench device adaptation."""

from __future__ import annotations

import itertools
import json
import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.device import ImagePolicy
from chongming.contracts.json_codec import _decode
from chongming.modules.workbench_device import image_modules
from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.metrics import DetectionMetrics, assignment
from chongming_scorer.scorer import score_center_run, score_edge_run
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)
from tests.test_detection import frame

PROFILE = Path(__file__).resolve().parents[1] / "scenarios/policies/hannover_vision_start.json"


def policies() -> ImagePolicy:
    result: ImagePolicy = _decode(json.loads(PROFILE.read_text())["station-west"], ImagePolicy)
    return result


class EvaluationTests(unittest.TestCase):
    def test_assignment_matches_exhaustive_cardinality_then_cost(self) -> None:
        randomizer = random.Random(42)
        for _ in range(50):
            costs = [
                [randomizer.choice((math.inf, 0.0, 1.0, 3.0)) for _ in range(3)] for _ in range(3)
            ]
            choices = []
            for columns in itertools.product(range(-1, 3), repeat=3):
                used = [j for j in columns if j >= 0]
                if len(set(used)) == len(used):
                    cost = sum(costs[i][j] for i, j in enumerate(columns) if j >= 0)
                    if math.isfinite(cost):
                        choices.append((-len(used), cost))
            actual = assignment(costs, 3)
            self.assertEqual(
                (
                    -sum(j >= 0 for j in actual),
                    sum(costs[i][j] for i, j in enumerate(actual) if j >= 0),
                ),
                min(choices),
            )

    def test_duplicates_outliers_and_empty_predictions_have_separate_effects(self) -> None:
        metrics = DetectionMetrics(10, "px")
        row = metrics.add([(0.0, 0.0), (1.0, 0.0), (100.0, 0.0)], {"a": (0.0, 0.0)})
        self.assertEqual(
            (row["tp"], row["fp"], row["duplicates"], row["false_alarms"]), (1, 2, 1, 1)
        )
        metrics.add([], {"a": (0.0, 0.0), "b": (10.0, 0.0)})
        result = metrics.summary()
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (1, 2, 2))
        self.assertEqual(result["precision"], 1 / 3)
        self.assertEqual(result["recall"], 1 / 3)
        self.assertEqual(row["matched_errors"], [0])
        self.assertNotIn("frame_precision", result)
        self.assertNotIn("matched_error", result)
        self.assertEqual(result["per_target"]["b"]["recall"], 0)

    def test_unobservable_targets_are_ignored_and_zero_denominators_are_null(self) -> None:
        metrics = DetectionMetrics(1, "m")
        result = metrics.add([(0.0, 0.0, 0.0)], {}, ignored=[(0.0, 0.0, 0.0)])
        self.assertEqual(result["ignored_predictions"], 1)
        self.assertEqual(result["fp"], 0)
        summary = metrics.summary()
        self.assertIsNone(summary["recall"])
        self.assertIsNone(summary["precision"])
        self.assertEqual(result["matched_errors"], [])

    def test_gate_boundary_and_invalid_coordinates(self) -> None:
        metrics = DetectionMetrics(5, "px")
        self.assertEqual(metrics.add([(3.0, 4.0)], {"a": (0.0, 0.0)})["tp"], 1)
        self.assertEqual(metrics.summary()["tp"], 1)
        with self.assertRaises(ValueError):
            metrics.add([(math.nan, 0)], {})
        with self.assertRaises(ValueError):
            DetectionMetrics(0, "px")


class WorkbenchDeviceTests(unittest.TestCase):
    def test_explicit_policy_and_coordinate_scaling_match_workbench_stages(self) -> None:
        p = policies()
        assert isinstance(p.detection, d.FrameDifferencePolicy)
        assert isinstance(p.tracking, d.KalmanTrackingPolicy)
        detector, tracker = image_modules(
            replace(p.detection, minimum_component_area_px2=1, blob_dilation_size=0),
            replace(p.tracking, confirmation_frame_count=1),
            m.StationId("a"),
            m.SessionId("run-1"),
        )
        first = frame(time_ns=0)
        self.assertEqual(detector.detect(first).candidates, ())
        pixels = np.zeros((32, 32), dtype=np.uint8)
        pixels[10:16, 14:20] = 255
        batch = detector.detect(frame(pixels, time_ns=33_333_333))
        self.assertGreater(len(batch.candidates), 0)
        result = tracker.update(batch)
        self.assertEqual(len(result.observations), len(batch.candidates))
        self.assertEqual(result.observations[0].centroid_px, batch.candidates[0].centroid_px)
        self.assertLess(abs(result.observations[0].centroid_px[0] - 16.5), 2)
        empty = replace(
            batch,
            capture_time=replace(
                batch.capture_time, exposure_start_ns=66_666_666, exposure_end_ns=66_666_666
            ),
            candidates=(),
        )
        coast = tracker.update(empty)
        self.assertEqual(coast.observations, ())
        self.assertEqual(coast.tracks[0].lifecycle, "coasting")
        self.assertEqual(coast.tracks[0].track_quality, 0)
        with self.assertRaises(ValueError):
            detector.detect(first)

    def test_policy_families_cannot_be_mixed_and_parameters_are_required(self) -> None:
        from tests.test_detection import tracking_policy

        p = policies()
        with self.assertRaises(TypeError):
            image_modules(p.detection, tracking_policy(), m.StationId("a"), m.SessionId("s"))
        raw: dict[str, Any] = json.loads(PROFILE.read_text())["station-west"]
        del raw["detection"]["threshold"]
        with self.assertRaises(ValueError):
            _decode(raw, ImagePolicy)

    def test_bundled_policy_profiles_decode_and_construct_workbench_modules(self) -> None:
        from chongming.modules.workbench_device import WorkbenchMotionModule

        policies_dir = Path(__file__).resolve().parents[1] / "scenarios" / "policies"
        for policy_path in sorted(policies_dir.glob("*.json")):
            with self.subTest(policy=policy_path.name):
                decoded = _decode(json.loads(policy_path.read_text()), dict[str, ImagePolicy])
                self.assertTrue(decoded)
                for station_id, station_policy in decoded.items():
                    detector, _ = image_modules(
                        station_policy.detection,
                        station_policy.tracking,
                        m.StationId(station_id),
                        m.SessionId("policy-check"),
                    )
                    self.assertIsInstance(detector, WorkbenchMotionModule)


class RecordingEvaluationTests(unittest.TestCase):
    def test_warmup_trains_all_frames_but_fov_responsibility_scores_only_later_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"
            times = (0, 15_000_000_000, 16_000_000_000)

            with TestSceneWriter(
                scene_dir,
                scenario_id="edge-warmup-test",
                world_frame_id="demo-enu",
                clock_domain_id="fixture",
                start_time_ns=0,
                end_time_ns=16_000_000_000,
                stations=[],
            ) as scene_writer:
                for index, ns in enumerate(times):
                    capture = common_pb2.CaptureTime(
                        clock_domain_id="fixture",
                        exposure_start_ns=ns,
                        exposure_end_ns=ns,
                        clock_uncertainty_ns=0,
                        rolling_shutter_model="none",
                    )
                    frame_id = f"f-{index}"
                    scene_writer.add_frame_index(
                        truth_pb2.FrameIndexEntry(
                            station_id="station-west",
                            station_session_id="fixture",
                            camera_role=common_pb2.CAMERA_ROLE_MAIN,
                            frame_index=index,
                            frame_id=frame_id,
                            capture_time=capture,
                            station_config_digest="fixture",
                            video_relpath="video/station-west-main.mp4",
                            width_px=32,
                            height_px=32,
                        )
                    )
                    scene_writer.add_truth_frame(
                        truth_pb2.TruthFrame(
                            world_frame_id="demo-enu",
                            experiment_time_ns=ns,
                            step_index=index,
                            targets=[
                                truth_pb2.TargetTruth(
                                    target_id="target",
                                    target_class="sphere",
                                    position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                                    velocity_enu_mps=common_pb2.Vec3(),
                                    bounding_radius_m=1.0,
                                    body_aabb_diagonal_m=2.0,
                                )
                            ],
                            projections=[
                                truth_pb2.StationTargetProjection(
                                    station_id="station-west",
                                    target_id="target",
                                    occlusion_status=(
                                        truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV
                                        if index == 2
                                        else truth_pb2.OCCLUSION_STATUS_VISIBLE
                                    ),
                                    projected_centroid_px=common_pb2.Vec2(x=16.0, y=16.0),
                                    projected_diagonal_px=4.0,
                                    range_m=50.0,
                                    frame_id=frame_id,
                                )
                            ],
                        )
                    )

            scene = TestScenePackage.open(scene_dir)
            with RunOutputWriter(
                run_dir,
                run_id="run-edge-1",
                mode="edge",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as run_writer:
                for index, ns in enumerate(times):
                    capture = common_pb2.CaptureTime(
                        clock_domain_id="fixture",
                        exposure_start_ns=ns,
                        exposure_end_ns=ns,
                        clock_uncertainty_ns=0,
                        rolling_shutter_model="none",
                    )
                    run_writer.write_edge_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=index + 1,
                            sent_wall_time_ns=0,
                            detection_batch=online_pb2.DetectionBatch(
                                main_frame_ref=common_pb2.FrameRef(
                                    station_id="station-west",
                                    station_session_id="fixture",
                                    camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                    frame_id=f"f-{index}",
                                ),
                                capture_time=capture,
                                processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                                decode_duration_ns=1_000_000,
                                cv_duration_ns=500_000,
                            ),
                        )
                    )

            run = RunOutputPackage.open(run_dir)
            scores, result, _ = score_edge_run(scene, run, gate_px=10.0, size_k=None, warmup_s=15.0)
            self.assertEqual(len(scores), 2)
            self.assertEqual(result["stations"]["station-west"]["processed_frames"], 3)
            self.assertEqual(result["stations"]["station-west"]["warmup_frames"], 1)
            self.assertEqual(result["aggregate"]["frames"], 2)
            self.assertEqual(result["aggregate"]["fn"], 1)
            self.assertEqual(result["aggregate"]["recall"], 0)
            self.assertIsNone(result["aggregate"]["precision"])
            run.write_scores(scores, result)
            with self.assertRaises(FileExistsError):
                run.write_scores(scores, result)

    def test_center_empty_windows_count_misses_at_exact_reference_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"

            with TestSceneWriter(
                scene_dir,
                scenario_id="center-empty-window",
                world_frame_id="demo-enu",
                clock_domain_id="demo-clock",
                start_time_ns=0,
                end_time_ns=1_000_000,
                stations=[],
            ) as scene_writer:
                for step_index, ns in enumerate((500_000, 1_000_000)):
                    scene_writer.add_truth_frame(
                        truth_pb2.TruthFrame(
                            world_frame_id="demo-enu",
                            experiment_time_ns=ns,
                            step_index=step_index,
                            targets=[
                                truth_pb2.TargetTruth(
                                    target_id="target",
                                    target_class="sphere",
                                    position_enu_m=common_pb2.Vec3(x=5.0, y=0.0, z=30.0),
                                    velocity_enu_mps=common_pb2.Vec3(),
                                    bounding_radius_m=1.0,
                                    body_aabb_diagonal_m=2.0,
                                )
                            ],
                            projections=[
                                truth_pb2.StationTargetProjection(
                                    station_id=station,
                                    target_id="target",
                                    occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                                )
                                for station in ("s1", "s2")
                            ],
                        )
                    )

            scene = TestScenePackage.open(scene_dir)
            with RunOutputWriter(
                run_dir,
                run_id="run-center-1",
                mode="center",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as run_writer:
                run_writer.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        fusion_measurement=online_pb2.FusionMeasurement(
                            center_session_id="c1",
                            measurement_id="one",
                            clock_domain_id="demo-clock",
                            world_frame_id="demo-enu",
                            reference_time_ns=500_000,
                            support_exposure_span_ns=0,
                            status=common_pb2.FUSION_STATUS_LOCATED,
                            position_enu_m=common_pb2.Vec3(x=5.0, y=0.0, z=30.0),
                        ),
                    )
                )
                for seq, ns in enumerate((500_000, 1_000_000), start=2):
                    run_writer.write_center_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=seq,
                            progress_report=online_pb2.ProgressReport(
                                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                                source_id="center",
                                session_id="c1",
                                completed_time_ns=ns,
                                completed_sequence_no=seq - 1,
                                finite_end_reached=(seq == 3),
                            ),
                        )
                    )

            run = RunOutputPackage.open(run_dir)
            scores, report, _ = score_center_run(scene, run, gate_m=1.0)
            self.assertEqual(report["metrics"]["tp"], 1)
            self.assertEqual(report["metrics"]["fn"], 1)
            self.assertEqual(report["metrics"]["recall"], 0.5)
            self.assertEqual(report["metrics"]["precision"], 1)
            self.assertEqual(scores[0].matched_count, 1)
            self.assertEqual(scores[0].mean_error, 0.0)
            self.assertEqual(scores[1].matched_count, 0)
            self.assertFalse(scores[1].HasField("mean_error"))


class WorkbenchStateTests(unittest.TestCase):
    def test_pause_and_backward_seek_do_not_retrain_repeated_frames(self) -> None:
        from chongming.vision.interactive_tuning import InteractivePipeline
        from chongming.vision.pipeline import PipelineContext, Stage

        class Counter(Stage):
            def __init__(self) -> None:
                self.times: list[float] = []
                self.resets = 0

            def process(self, context: PipelineContext) -> None:
                self.times.append(context.timestamp)

            def reset(self) -> None:
                self.times.clear()
                self.resets += 1

        counter = Counter()
        workbench = InteractivePipeline.__new__(InteractivePipeline)
        workbench.frames = [np.zeros((4, 4, 3), dtype=np.uint8) for _ in range(4)]
        workbench.scoring = None
        workbench.fps = 2
        workbench.stages = [counter]
        workbench.last_processed = -1
        workbench.context = None
        first = workbench.process_until(2)
        self.assertEqual(counter.times, [0, 0.5, 1])
        self.assertIs(workbench.process_until(2), first)
        self.assertEqual(counter.times, [0, 0.5, 1])
        workbench.process_until(1)
        self.assertEqual(counter.times, [0, 0.5])
        self.assertEqual(counter.resets, 1)

    def test_kalman_confirmation_requires_consecutive_observations_and_real_time(self) -> None:
        from chongming.vision.pipeline import Blob, PipelineContext
        from chongming.vision.tracking import TrackingConfig, TrackingStage

        tracker = TrackingStage(TrackingConfig(confirm_frames=2))

        def context(time: float, present: bool) -> Any:
            return PipelineContext(
                frame=np.zeros((2, 2, 3), dtype=np.uint8),
                timestamp=time,
                blobs=[Blob(centroid_px=(10, 10), area_px2=1, bounding_box=(10, 10, 1, 1))]
                if present
                else [],
            )

        tracker.process(context(0, True))
        tracker.process(context(0.2, False))
        third = context(0.5, True)
        tracker.process(third)
        self.assertEqual(third.detections, [])
        fourth = context(0.7, True)
        tracker.process(fourth)
        self.assertEqual(len(fourth.detections), 1)
        self.assertAlmostEqual(float(tracker.tracks[0].kalman.F[0, 2]), 0.2, places=6)
        with self.assertRaises(ValueError):
            tracker.process(fourth)

    def test_composition_uses_workbench_modules_for_explicit_workbench_policy(self) -> None:
        from chongming.composition.station import declare_station
        from chongming.modules.workbench_device import (
            WorkbenchMotionModule,
            WorkbenchTrackingModule,
        )
        from tests.test_center_process import _station_config_and_aux

        config, _ = _station_config_and_aux("station-west", (-20.0, -40.0, 4.0))
        p = policies()
        station_policy = d.DevicePolicy(
            detection=p.detection,
            tracking=p.tracking,
            calibration=d.CalibrationPolicy(
                estimator_id="opencv_pnp_ransac_v1",
                update_period_ns=1_000_000_000,
                stale_after_ns=5_000_000_000,
                recovery_success_count=2,
                minimum_reference_count=4,
                maximum_reprojection_rmse_px=2.0,
                maximum_rotation_only_parallax_px=5.0,
            ),
            rays=d.RayGenerationPolicy(
                uncertainty_model_id="tangent_gaussian_v1",
                fallback_centroid_covariance_px2=((0.25, 0.0), (0.0, 0.25)),
                unmodelled_high_frequency_jitter_covariance_rad2=None,
                unmodelled_relative_flex_covariance_rad2=None,
                residual_independence_basis=None,
            ),
        )
        station, _ = declare_station(
            config=config,
            policy=station_policy,
            image_session_id=m.SessionId("sess-west"),
        )
        self.assertIsInstance(station.motion, WorkbenchMotionModule)
        self.assertIsInstance(station.tracking, WorkbenchTrackingModule)

    def test_explicit_profile_sets_workbench_parameters_without_opening_windows(self) -> None:
        from unittest.mock import patch

        from chongming.vision.interactive_tuning import InteractivePipeline

        p = policies()
        assert isinstance(p.detection, d.FrameDifferencePolicy)
        assert isinstance(p.tracking, d.KalmanTrackingPolicy)
        workbench = InteractivePipeline.__new__(InteractivePipeline)
        controls: dict[str, int] = {"Show": 0}
        with (
            patch(
                "cv2.setTrackbarPos",
                side_effect=lambda name, window, value: controls.update({name: value}),
            ),
            patch("cv2.getTrackbarPos", side_effect=lambda name, window: controls[name]),
        ):
            workbench.configure(p.detection, p.tracking)
            params = workbench._get_params()
        self.assertEqual(params["mode"], "two_frame")
        self.assertEqual(params["threshold"], 2)
        self.assertEqual(params["downsample"], 2)
        self.assertEqual(params["score_inc"], 4)
        self.assertEqual(params["score_decay"], 0.1)
        self.assertEqual(params["occ_dilate"], 20)

    def test_device_color_and_padding_preserve_workbench_preprocessing_order(self) -> None:
        import cv2

        from chongming.modules.workbench_device import _image_to_bgr
        from chongming.vision.pipeline import PipelineContext

        bgr = np.random.default_rng(42).integers(0, 256, (32, 32, 3), dtype=np.uint8)
        rgb = np.asarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), dtype=np.uint8)
        for pixels, format in ((bgr, m.PixelFormat.BGR8), (rgb, m.PixelFormat.RGB8)):
            adapted = _image_to_bgr(frame(pixels, pixel_format=format, padding=5).image)
            np.testing.assert_array_equal(adapted, bgr)
        p = policies()
        detector, _ = image_modules(p.detection, p.tracking, m.StationId("a"), m.SessionId("run-1"))
        from chongming.modules.workbench_device import WorkbenchMotionModule

        assert isinstance(detector, WorkbenchMotionModule)
        expected = PipelineContext(frame=bgr, timestamp=0)
        for stage in detector.stages:
            stage.process(expected)
        fresh, _ = image_modules(p.detection, p.tracking, m.StationId("a"), m.SessionId("run-1"))
        # Prime both temporal buffers with identical color data before the changed frame.
        fresh.detect(frame(rgb, pixel_format=m.PixelFormat.RGB8, padding=5))
        changed = np.roll(bgr, 3, axis=1)
        expected = PipelineContext(frame=changed, timestamp=0.1)
        for stage in detector.stages:
            stage.process(expected)
        result = fresh.detect(
            frame(
                np.asarray(cv2.cvtColor(changed, cv2.COLOR_BGR2RGB), dtype=np.uint8),
                time_ns=100_000_000,
                pixel_format=m.PixelFormat.RGB8,
                padding=5,
            )
        )
        np.testing.assert_allclose(
            [item.centroid_px for item in result.candidates],
            [(blob.centroid_px[0] * 2, blob.centroid_px[1] * 2) for blob in expected.blobs],
        )
