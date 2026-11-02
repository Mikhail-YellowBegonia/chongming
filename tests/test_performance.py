"""Recorded performance preserves missing metrics and nested timing semantics."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from chongming.performance import distribution, report
from tests import test_godot_visualization as transport


class PerformanceTests(unittest.TestCase):
    def fixture(self, directory: str) -> Path:
        root = Path(directory)
        (root / "manifest.json").write_text(json.dumps({"code_revision": "fixture"}))
        (root / "summary.json").write_text(json.dumps({"status": "complete", "counts": {}}))
        rows = [
            {
                "sample_time": {"time_ns": index * 1_000_000_000},
                "stages": {"total_ms": total, "render_ms": total - 1, "gpu_ms": 1},
                "evidence_ms": 2,
            }
            for index, total in enumerate((8, 18))
        ]
        (root / "timing.jsonl").write_text("\n".join(map(json.dumps, rows)))
        return root

    def test_nested_stages_are_not_added_and_missing_center_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = report(self.fixture(directory))
        self.assertEqual(result["metrics_ms"]["step_wall_ms"]["mean"], 15)
        self.assertEqual(result["measured_step_wall_s"], 0.03)
        self.assertAlmostEqual(result["throughput_steps_per_s"], 2 / 0.03)
        self.assertEqual(result["recorded_span_s"], 1)
        self.assertIn("center_ms", result["unavailable"])
        self.assertNotIn("center_ms", result["metrics_ms"])

    def test_percentiles_use_nearest_rank_and_empty_is_not_zero(self) -> None:
        self.assertEqual(distribution(list(range(1, 21)))["p95"], 19)
        self.assertIsNone(distribution([])["mean"])
        self.assertEqual(distribution([])["samples"], 0)

    def test_invalid_evidence_is_rejected(self) -> None:
        for invalid in (-1, math.inf, math.nan, True, "3"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                row = {
                    "sample_time": {"time_ns": 0},
                    "stages": {"total_ms": invalid},
                    "evidence_ms": 0,
                }
                (root / "timing.jsonl").write_text(json.dumps(row))
                with self.assertRaises(
                    TypeError if isinstance(invalid, (bool, str)) else ValueError
                ):
                    report(root)

    def test_empty_run_and_center_samples_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / "timing.jsonl").write_text("")
            (root / "center-output.jsonl").write_text(json.dumps({"center_ms": 4}))
            result = report(root)
        self.assertIsNone(result["throughput_steps_per_s"])
        self.assertIsNone(result["recorded_span_s"])
        self.assertEqual(result["metrics_ms"]["center_ms"]["samples"], 1)
        self.assertNotIn("center_ms", result["unavailable"])


@unittest.skipUnless(transport.GODOT, "Godot is required")
class VisualizationPerformanceTests(unittest.TestCase):
    def test_duplicate_and_rejected_updates_do_not_add_samples(self) -> None:
        state = transport.state()
        invalid: dict[str, Any] = {"algorithm_output": {}, "truth_snapshots": []}
        reports = transport.VisualizationReceiverTests().receive(state, state, invalid)
        for item in reports:
            perf = item["performance"]
            self.assertEqual(perf["update_ms"]["samples"], 1)
            self.assertEqual(perf["evaluation_ms"]["samples"], 1)
            self.assertEqual(perf["redraw_ms"]["samples"], 0)
            self.assertIsNone(perf["redraw_ms"]["p95"])
            self.assertGreaterEqual(perf["update_ms"]["p95"], 0)
        self.assertEqual(reports[0]["performance"], reports[1]["performance"])
        self.assertEqual(reports[1]["performance"], reports[2]["performance"])

    def test_timing_window_is_bounded_without_losing_update_count(self) -> None:
        from tests.test_godot_evaluation import TIME, payload

        states = [payload({TIME: ()}, update_id=index) for index in range(1, 521)]
        result = transport.VisualizationReceiverTests().receive(*states)[-1]
        self.assertEqual(result["update_count"], 520)
        for key in ("update_ms", "evaluation_ms"):
            timing = result["performance"][key]
            self.assertEqual(timing["samples"], 512)
            self.assertLessEqual(timing["p50"], timing["p95"])
            self.assertLessEqual(timing["p95"], timing["max"])
