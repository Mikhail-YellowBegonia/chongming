"""Recorded performance preserves missing metrics and nested timing semantics."""

from __future__ import annotations

import math
import unittest

from chongming_proto.online import common_pb2, online_pb2
from chongming_scorer.performance import distribution, summarize_edge_timings


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
