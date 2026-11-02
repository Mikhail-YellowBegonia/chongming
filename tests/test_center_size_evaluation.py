"""Center responsibility, physical-size bands and exposure-time evaluation schedule."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from chongming.center_evaluation import CenterSizeMetrics, model_aabb_diagonal
from chongming.contracts import scene as s
from chongming.evaluate_center import evaluate
from chongming.scenarios.serialization import save_compiled_scenario
from tests.test_scene_rendering import test_scene


class CenterSizeEvaluationTests(unittest.TestCase):
    def test_geometry_diagonal_uses_physical_model_bounds(self) -> None:
        self.assertAlmostEqual(model_aabb_diagonal(s.SphereGeometry(1)), 2 * math.sqrt(3))
        self.assertEqual(model_aabb_diagonal(s.BoxGeometry((1, 2, 2))), 6)
        mesh = s.TriangleMeshGeometry(((0, 0, 0), (3, 0, 0), (0, 4, 0)), ((0, 1, 2),))
        self.assertEqual(model_aabb_diagonal(mesh), 5)

    def test_precise_and_complete_miss_boundaries(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        target: dict[str, tuple[float, ...]] = {"a": (0.0, 0.0, 0.0)}
        for error, precise, coarse in ((0.7, 1, 1), (1.0, 0, 1), (3.0, 0, 0)):
            row = metric.add([(error, 0.0, 0.0)], target, {"a": 1.0}, [], [])
            self.assertEqual(row["precise"]["tp"], precise)
            self.assertEqual(row["within_miss_gate"]["tp"], coarse)
        summary = metric.summary()
        self.assertEqual(summary["intermediate_matches"], 1)
        self.assertEqual(summary["completely_off_target_outputs"], 1)
        self.assertEqual(summary["no_match_within_miss_gate"], 1)

    def test_missing_outputs_duplicates_and_nonresponsible_targets(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        row = metric.add(
            [(0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (100.0, 0.0, 0.0)],
            {"a": (0.0, 0.0, 0.0), "b": (10.0, 0.0, 0.0)},
            {"a": 1.0, "b": 1.0},
            [(100.0, 0.0, 0.0)],
            [1.0],
        )
        self.assertEqual(row["precise"]["tp"], 1)
        self.assertEqual(row["precise"]["duplicates"], 1)
        self.assertEqual(row["precise"]["ignored_predictions"], 1)
        self.assertEqual(row["within_miss_gate"]["fn"], 1)
        self.assertEqual(row["completely_off_target_outputs"], 0)

    def test_nonresponsible_target_excludes_only_precise_hits(self) -> None:
        metric = CenterSizeMetrics(0.7, 3)
        row = metric.add([(1.0, 0.0, 0.0)], {}, {}, [(0.0, 0.0, 0.0)], [1.0])
        self.assertEqual(row["precise"]["fp"], 1)
        self.assertEqual(row["precise"]["ignored_predictions"], 0)
        self.assertEqual(row["within_miss_gate"]["ignored_predictions"], 1)

    def test_actual_exposure_schedule_not_window_midpoint_counts_empty_window(self) -> None:
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
                                {
                                    "key": {"start_ns": 0, "end_ns": 1_000_000},
                                    "reference_time_ns": 500_000,
                                },
                                {
                                    "key": {"start_ns": 1_000_000, "end_ns": 2_000_000},
                                    "reference_time_ns": 1_500_000,
                                },
                            ]
                        }
                    }
                ],
                "measurements": [
                    {
                        "measurement_id": "a",
                        "reference_time_ns": 100_000,
                        "status": "located",
                        "position_enu_m": [5, 0, 30],
                    }
                ],
            }
            (root / "center-output.jsonl").write_text(json.dumps(row) + "\n")
            (root / "center-input.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "progress": [
                                {
                                    "completed_capture_time": {
                                        "exposure_start_ns": ns,
                                        "exposure_end_ns": ns,
                                    }
                                }
                            ]
                        }
                    )
                    + "\n"
                    for ns in (100_000, 1_100_000)
                )
            )
            report = evaluate(root, root / "report", size_k=0.7, miss_k=3)
            self.assertEqual(report["metrics"]["precise"]["tp"], 1)
            self.assertEqual(report["metrics"]["precise"]["fn"], 1)
            self.assertEqual(report["metrics"]["precise"]["recall"], 0.5)
            frames = [
                json.loads(l) for l in (root / "report" / "frames.jsonl").read_text().splitlines()
            ]
            self.assertEqual([r["reference_time_ns"] for r in frames], [100_000, 1_100_000])
            three_station = evaluate(root, root / "three-station", size_k=0.7, minimum_stations=3)
            self.assertEqual(three_station["minimum_stations"], 3)
            self.assertEqual(three_station["metrics"]["precise"]["fn"], 0)
            after_warmup = evaluate(
                root, root / "warmup-report", size_k=0.7, warmup_s=0.001, warmup_origin_ns=100_000
            )
            self.assertEqual(after_warmup["metrics"]["precise"]["frames"], 1)
            self.assertEqual(after_warmup["metrics"]["precise"]["fn"], 1)
            self.assertEqual(after_warmup["warmup_origin_ns"], 100_000)
            row["diagnostic_raw_solutions"] = [
                {
                    "measurement_id": "raw/a",
                    "reference_time_ns": 100_000,
                    "position_enu_m": [5, 0, 30],
                },
                {
                    "measurement_id": "raw/b",
                    "reference_time_ns": 1_100_000,
                    "position_enu_m": [5, 0, 30],
                },
            ]
            (root / "center-output.jsonl").write_text(json.dumps(row) + "\n")
            raw_report = evaluate(root, root / "raw-report", size_k=0.7, diagnostic_raw=True)
            self.assertEqual(raw_report["metrics"]["precise"]["recall"], 1)
            self.assertEqual(raw_report["prediction_source"], "diagnostic raw intersections")
