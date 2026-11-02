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
from chongming.evaluate_recording import ImagePolicy
from chongming.evaluation import DetectionMetrics, assignment
from chongming.modules.workbench_device import image_modules
from chongming.scenarios.serialization import _decode
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


class RecordingEvaluationTests(unittest.TestCase):
    def test_warmup_trains_all_frames_but_fov_responsibility_scores_only_later_frames(self) -> None:
        import hashlib
        from unittest.mock import patch

        from chongming.evaluate_recording import evaluate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video").mkdir()
            (root / "video/main.mp4").write_bytes(b"fixture decoder input")
            digest = hashlib.sha256(b"fixture decoder input").hexdigest()
            (root / "manifest.json").write_text(json.dumps({"code_revision": "fixture"}))
            (root / "video.json").write_text(
                json.dumps(
                    {
                        "streams": {
                            "camera": {"path": "video/main.mp4", "frame_count": 3, "sha256": digest}
                        }
                    }
                )
            )
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps({"station-west": json.loads(PROFILE.read_text())["station-west"]})
            )
            online, truth = [], []
            for index, ns in enumerate((0, 15_000_000_000, 16_000_000_000)):
                capture = {
                    "clock_domain_id": "fixture",
                    "clock_uncertainty_ns": 0,
                    "exposure_start_ns": ns,
                    "exposure_end_ns": ns,
                    "rolling_shutter_model": "none",
                }
                metadata = {
                    "station_id": "station-west",
                    "session_id": "fixture",
                    "camera_role": "main",
                    "frame_id": str(index),
                    "capture_time": capture,
                    "path": "video/main.mp4",
                    "frame_index": index,
                    "height_px": 32,
                    "width_px": 32,
                    "camera_config_ref": "fixture",
                }
                online.append({"sample_time": {"time_ns": ns}, "frames": [metadata]})
                truth.append(
                    {
                        "sample_time": {"time_ns": ns},
                        "camera_frames": [
                            {
                                "station_id": "station-west",
                                "station_session_id": "fixture",
                                "camera_role": "main",
                                "frame_id": str(index),
                                "capture_time": capture,
                                "targets": [
                                    {
                                        "truth_object_id": "target",
                                        "projection": {
                                            "center_in_field_of_view": index != 2,
                                            "centroid_px": [16, 16],
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                )
            (root / "online.jsonl").write_text("\n".join(map(json.dumps, online)))
            (root / "truth.jsonl").write_text("\n".join(map(json.dumps, truth)))

            class Decoder:
                def __init__(self, path: str):
                    self.remaining = 3

                def read(self) -> tuple[bool, Any]:
                    self.remaining -= 1
                    return (
                        (True, np.zeros((32, 32, 3), dtype=np.uint8))
                        if self.remaining >= 0
                        else (False, None)
                    )

                def release(self) -> None:
                    pass

            exported: list[str] = []
            with patch("chongming.evaluate_recording.cv2.VideoCapture", Decoder):
                result = evaluate(
                    root,
                    policy_path,
                    root / "report",
                    gate_px=10,
                    observation_sink=lambda image, observations: exported.append(
                        str(image.frame_id)
                    ),
                )
            self.assertEqual(exported, ["0", "1", "2"])
            self.assertEqual(result["stations"]["station-west"]["processed_frames"], 3)
            self.assertEqual(result["stations"]["station-west"]["warmup_frames"], 1)
            self.assertEqual(result["aggregate"]["frames"], 2)
            self.assertEqual(result["aggregate"]["fn"], 1)
            self.assertEqual(result["aggregate"]["recall"], 0)
            self.assertIsNone(result["aggregate"]["precision"])
            with self.assertRaises(FileExistsError):
                evaluate(root, policy_path, root / "report", gate_px=10)

    def test_center_empty_windows_count_misses_at_exact_reference_times(self) -> None:
        from chongming.contracts import scene as s
        from chongming.evaluate_center import evaluate
        from chongming.scenarios.serialization import save_compiled_scenario
        from tests.test_scene_rendering import test_scene

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_compiled_scenario(
                test_scene(s.SphereGeometry(1), (5, 0, 30)), root / "scenario.json"
            )
            row = {
                "batches": [
                    {
                        "window_update": {
                            "sealed_windows": [
                                {"reference_time_ns": 500_000},
                                {"reference_time_ns": 1_000_000},
                            ]
                        }
                    }
                ],
                "measurements": [
                    {
                        "measurement_id": "one",
                        "reference_time_ns": 500_000,
                        "status": "located",
                        "position_enu_m": [5, 0, 30],
                    }
                ],
            }
            (root / "center-output.jsonl").write_text(json.dumps(row) + "\n")
            report = evaluate(root, root / "report", gate_m=1)
            self.assertEqual(report["metrics"]["tp"], 1)
            self.assertEqual(report["metrics"]["fn"], 1)
            self.assertEqual(report["metrics"]["recall"], 0.5)
            self.assertEqual(report["metrics"]["precision"], 1)
            evidence = json.loads((root / "report" / "frames.jsonl").read_text().splitlines()[0])
            self.assertEqual(evidence["matched_errors"], [0])


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
        from chongming.composition.graph import declare_assembly
        from chongming.modules.workbench_device import (
            WorkbenchMotionModule,
            WorkbenchTrackingModule,
        )
        from tests.fixtures import architecture_specs

        spec, run = architecture_specs()
        p = policies()
        stations = tuple(
            replace(
                station,
                device_policy=replace(
                    station.device_policy, detection=p.detection, tracking=p.tracking
                ),
            )
            for station in run.stations
        )
        assembly = declare_assembly(spec, replace(run, stations=stations))
        for station in assembly.stations:
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
