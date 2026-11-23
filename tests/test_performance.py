"""Recorded performance preserves missing metrics and nested timing semantics."""

from __future__ import annotations

import math
import unittest
from typing import Any

from chongming_proto.online import common_pb2, online_pb2
from chongming_scorer.performance import distribution, summarize_edge_timings
from tests import test_godot_visualization as transport


class PerformanceTests(unittest.TestCase):
    def test_percentiles_use_nearest_rank_and_empty_is_not_zero(self) -> None:
        self.assertEqual(distribution(list(range(1, 21)))["p95"], 19)
        self.assertIsNone(distribution([])["mean"])
        self.assertEqual(distribution([])["samples"], 0)

    def test_invalid_durations_are_rejected(self) -> None:
        for invalid in (-1.0, math.inf, math.nan):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                distribution([1.0, invalid])

    def test_edge_timings_separate_decode_and_cv_latencies(self) -> None:
        batches = [
            online_pb2.DetectionBatch(
                main_frame_ref=common_pb2.FrameRef(station_id="s1", frame_id="f1"),
                decode_duration_ns=12_000_000,
                cv_duration_ns=3_000_000,
            ),
            online_pb2.DetectionBatch(
                main_frame_ref=common_pb2.FrameRef(station_id="s2", frame_id="f2"),
                decode_duration_ns=8_000_000,
                cv_duration_ns=5_000_000,
            ),
        ]
        report = summarize_edge_timings(batches)
        self.assertEqual(report["aggregate"]["decode_ms"]["mean"], 10.0)
        self.assertEqual(report["aggregate"]["cv_ms"]["mean"], 4.0)
        self.assertEqual(report["aggregate"]["total_algorithm_ms"], report["aggregate"]["cv_ms"])
        self.assertEqual(report["stations"]["s1"]["decode_ms"]["max"], 12.0)
        self.assertEqual(report["stations"]["s2"]["cv_ms"]["max"], 5.0)


@unittest.skipUnless(transport.GODOT, "Godot is required")
class VisualizationPerformanceTests(unittest.TestCase):
    def test_duplicate_and_rejected_updates_do_not_add_samples(self) -> None:
        state = transport.state()
        invalid: dict[str, Any] = {"algorithm_output": {}, "truth_snapshots": []}
        reports = transport.VisualizationReceiverTests().receive(state, state, invalid)
        for item in reports:
            perf = item["performance"]
            self.assertEqual(perf["update_ms"]["samples"], 1)
            self.assertEqual(perf["redraw_ms"]["samples"], 0)
            self.assertIsNone(perf["redraw_ms"]["p95"])
            self.assertGreaterEqual(perf["update_ms"]["p95"], 0)
        self.assertEqual(reports[0]["performance"], reports[1]["performance"])
        self.assertEqual(reports[1]["performance"], reports[2]["performance"])

    def test_timing_window_is_bounded_without_losing_update_count(self) -> None:
        from dataclasses import replace

        from chongming.contracts import visualization as v
        from chongming.rendering.godot import visualization_ui_state

        states = [
            {
                "version": index,
                **visualization_ui_state(
                    replace(
                        transport.fixed_update(index),
                        edge_events=None,
                        center_events=None,
                        station_states=None,
                    ),
                    (v.TruthComparisonSnapshot(targets=()),),
                ),
            }
            for index in range(1, 521)
        ]
        result = transport.VisualizationReceiverTests().receive(*states)[-1]
        self.assertEqual(result["update_count"], 520)
        timing = result["performance"]["update_ms"]
        self.assertEqual(timing["samples"], 512)
        self.assertLessEqual(timing["p50"], timing["p95"])
        self.assertLessEqual(timing["p95"], timing["max"])
