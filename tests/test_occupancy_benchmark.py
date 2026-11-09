"""Overload boundaries, incomplete scoring, and real worker termination evidence."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import numpy as np

from chongming.benchmark_occupancy import arm_policies, supervise
from chongming.evaluate_recording import evaluate
from chongming.evaluation_attempt import EvaluationBudget, EvaluationLimit
from chongming.scenarios.run import _json
from tests.test_detection import batch, frame

ROOT = Path(__file__).resolve().parents[1]


def recording_fixture(root: Path, times: list[int]) -> Path:
    (root / "main.mp4").write_bytes(b"mock decoded frames")
    (root / "manifest.json").write_text(json.dumps({"code_revision": "fixture"}))
    (root / "video.json").write_text(
        json.dumps(
            {
                "streams": {
                    "main": {
                        "path": "main.mp4",
                        "frame_count": len(times),
                        "sha256": hashlib.sha256(b"mock decoded frames").hexdigest(),
                    }
                }
            }
        )
    )
    policy = root / "policy.json"
    policy.write_text(json.dumps({"a": next(iter(arm_policies(ROOT)["difference-on"].values()))}))
    online, truth = [], []
    for index, ns in enumerate(times):
        source = frame(time_ns=ns)
        metadata = {
            "station_id": "a",
            "session_id": "run-1",
            "camera_role": "main",
            "frame_id": source.frame_id,
            "capture_time": source.capture_time,
            "path": "main.mp4",
            "frame_index": index,
            "width_px": 32,
            "height_px": 32,
            "camera_config_ref": "fixture",
        }
        online.append({"sample_time": {"time_ns": ns}, "frames": [metadata]})
        truth.append(
            {
                "sample_time": {"time_ns": ns},
                "camera_frames": [
                    {
                        "station_id": "a",
                        "station_session_id": "run-1",
                        "camera_role": "main",
                        "frame_id": source.frame_id,
                        "capture_time": source.capture_time,
                        "targets": [
                            {
                                "truth_object_id": "target",
                                "projection": {
                                    "center_in_field_of_view": True,
                                    "centroid_px": [16, 16],
                                },
                            }
                        ],
                    }
                ],
            }
        )
    (root / "online.jsonl").write_text("".join(_json(row) + "\n" for row in online))
    (root / "truth.jsonl").write_text("".join(_json(row) + "\n" for row in truth))
    return policy


class OverloadTests(unittest.TestCase):
    def test_100_is_allowed_and_hold_ends_at_exactly_10_seconds(self) -> None:
        budget = EvaluationBudget()
        self.assertFalse(budget.skip_tracking(100, 10_000_000_000))
        self.assertTrue(budget.skip_tracking(101, 9_999_999_999))
        with self.assertRaisesRegex(EvaluationLimit, "candidate_limit"):
            budget.skip_tracking(101, 10_000_000_000)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                EvaluationBudget(candidate_limit=value)

    def evaluate_fixture(
        self, root: Path, counts: list[int], *, probe: bool = False
    ) -> tuple[dict[str, Any], Mock, Mock]:
        times = [0, 10_000_000_000, 16_000_000_000]
        policy = recording_fixture(root, times)
        detector, tracker, decoder = Mock(), Mock(), Mock()
        detector.detect.side_effect = [
            batch(ns, *([(16.0, 16.0)] * count)) for ns, count in zip(times, counts, strict=True)
        ]
        tracker.update.return_value.observations = ()
        decoder.read.side_effect = [(True, np.zeros((32, 32, 3), dtype=np.uint8))] * 3 + [
            (False, None)
        ]
        with (
            patch("chongming.evaluate_recording.cv2.VideoCapture", return_value=decoder),
            patch("chongming.evaluate_recording.image_modules", return_value=(detector, tracker)),
        ):
            result = evaluate(
                root,
                policy,
                root / "report",
                gate_px=10,
                budget=EvaluationBudget(),
                max_frames_per_station=2 if probe else None,
            )
        return result, detector, tracker

    def test_early_overload_keeps_learning_but_never_enters_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, detector, tracker = self.evaluate_fixture(root, [101, 100, 1])
            self.assertEqual(detector.detect.call_count, 3)
            self.assertEqual(tracker.update.call_count, 2)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["attempt"]["overload_suppressed_frames"], {"a": 1})
            self.assertEqual(result["aggregate"]["fn"], 1)
            self.assertEqual(result["aggregate"]["recall"], 0)
            self.assertIsNone(result["aggregate"]["precision"])
            rows = [
                json.loads(line) for line in (root / "report/frames.jsonl").read_text().splitlines()
            ]
            self.assertTrue(rows[0]["tracking_suppressed"])
            self.assertEqual(rows[0]["timing_ms"]["track_ms"], 0)

    def test_late_overload_preserves_failing_candidates_and_has_no_full_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, detector, tracker = self.evaluate_fixture(root, [101, 101, 1])
            self.assertEqual(detector.detect.call_count, 2)
            tracker.update.assert_not_called()
            self.assertEqual(result["status"], "resource_limit")
            self.assertEqual(result["stop_reason"], "candidate_limit")
            self.assertIsNone(result["aggregate"])
            self.assertEqual(result["attempt"]["detected_frames"], {"a": 2})
            self.assertEqual(result["attempt"]["processed_frames"], {"a": 1})
            candidates = [
                json.loads(line)
                for line in (root / "report/candidates.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[-1]["counts"]["accepted_candidates"], 101)
            self.assertEqual(result["attempt"]["last_valid_frame"]["frame_index"], 0)
            self.assertEqual(result["attempt"]["current_frame"]["frame_index"], 1)

    def test_probe_is_not_a_full_recording_quality_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, detector, _ = self.evaluate_fixture(Path(directory), [1, 1, 1], probe=True)
            self.assertEqual(result["status"], "probe_complete")
            self.assertEqual(detector.detect.call_count, 2)
            self.assertIsNone(result["aggregate"])
            self.assertIsNone(result["stations"]["a"]["metrics"])


class SupervisorTests(unittest.TestCase):
    def test_hung_native_phase_leaves_a_failure_summary_after_hard_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "attempt"
            script = "\n".join(
                [
                    "import json, pathlib, time",
                    f"p = pathlib.Path({str(output / 'evaluation')!r})",
                    "p.mkdir()",
                    "(p / 'progress.json').write_text(json.dumps({'phase': 'track', 'phase_started_monotonic': time.monotonic(), 'current_frame': {'frame_index': 42}}))",
                    "time.sleep(10)",
                ]
            )
            result = supervise(
                [sys.executable, "-c", script],
                output,
                wall_limit_s=5,
                memory_limit_bytes=1024**3,
                phase_limit_s=0.2,
            )
            self.assertEqual(result["status"], "resource_limit")
            self.assertEqual(result["stop_reason"], "phase_time_limit")
            self.assertEqual(result["attempt"]["current_frame"]["frame_index"], 42)
            self.assertLess(result["supervision"]["exit_code"], 0)
            self.assertIsNone(result["aggregate"])
            self.assertEqual(json.loads((output / "summary.json").read_text()), result)

    def test_wall_memory_and_startup_failures_also_keep_records(self) -> None:
        for reason in ("wall_time_limit", "memory_limit", "worker_exit"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                command = [
                    sys.executable,
                    "-c",
                    "raise SystemExit(7)"
                    if reason == "worker_exit"
                    else "import time; time.sleep(10)",
                ]
                result = supervise(
                    command,
                    Path(directory) / "attempt",
                    wall_limit_s=0.2 if reason == "wall_time_limit" else 5,
                    memory_limit_bytes=1 if reason == "memory_limit" else 1024**3,
                    phase_limit_s=2,
                )
                self.assertEqual(result["stop_reason"], reason)
                self.assertIsNone(result["aggregate"])

    def test_policies_keep_morphology_off_and_preserve_separate_dilation_sizes(self) -> None:
        arms = arm_policies(ROOT)
        for name, stations in arms.items():
            for station in stations.values():
                policy = station["detection"]
                self.assertFalse(policy["apply_opening"])
                self.assertFalse(policy["apply_closing"])
                self.assertEqual(
                    policy["blob_dilation_size"],
                    3 if name == "mog2-tuned" or name.endswith("-dilate3") else 5,
                )
                self.assertEqual(
                    policy["apply_occupancy"],
                    "-off" not in name,
                )
                self.assertEqual(policy.get("apply_erosion", False), "-erode3-" in name)

    def test_supplemental_arms_change_only_blob_dilation_and_preserve_originals(self) -> None:
        arms = arm_policies(ROOT)
        for base in ("difference-off", "mog2-off"):
            for station, policy in arms[base + "-dilate3"].items():
                self.assertFalse(policy["detection"]["apply_occupancy"])
                self.assertEqual(policy["detection"]["blob_dilation_size"], 3)
                restored = json.loads(json.dumps(policy))
                restored["detection"]["blob_dilation_size"] = 5
                self.assertEqual(restored, arms[base][station])

    def test_erosion_arms_add_only_the_declared_single_erosion(self) -> None:
        from chongming.contracts.device import FrameDifferencePolicy
        from chongming.evaluate_recording import ImagePolicy
        from chongming.modules.workbench_device import WorkbenchMotionModule
        from chongming.scenarios.serialization import _decode
        from chongming.vision.morphology import MorphologicalFilterStage

        arms = arm_policies(ROOT)
        for base in ("difference-off", "mog2-off"):
            for station, policy in arms[base + "-erode3-dilate3"].items():
                restored = json.loads(json.dumps(policy))
                self.assertTrue(restored["detection"].pop("apply_erosion"))
                self.assertEqual(restored, arms[base + "-dilate3"][station])
                decoded = _decode(policy, ImagePolicy)
                assert isinstance(decoded.detection, FrameDifferencePolicy)
                detector = WorkbenchMotionModule(policy=decoded.detection)
                morphology = detector.stages[2]
                assert isinstance(morphology, MorphologicalFilterStage)
                self.assertTrue(morphology.config.apply_erosion)
                self.assertEqual(morphology.config.kernel_size, 3)
