"""Visual audit must preserve scoring assignments and sample provenance."""

import unittest

from chongming.evaluation import annotate
from chongming.evaluation_preview import select_frames


class EvaluationPreviewTests(unittest.TestCase):
    def test_annotation_distinguishes_miss_duplicate_and_far_false_alarm(self) -> None:
        result = annotate(
            {
                "eligible_targets_px": {"b": [30, 0], "a": [0, 0]},
                "predictions_px": [[1, 0], [2, 0], [80, 0]],
            },
            5,
        )
        self.assertEqual([t["matched"] for t in result["targets"]], [True, False])
        self.assertEqual(
            [p["kind"] for p in result["predictions"]],
            ["matched", "duplicate", "false_alarm"],
        )
        self.assertEqual(result["targets"][0]["id"], "a")

    def test_samples_include_worst_and_time_endpoints_without_duplicates(self) -> None:
        rows = [
            {
                "frame_id": str(i),
                "station_id": "station",
                "time_ns": i,
                "fn": 9 if i == 4 else 0,
                "fp": 7 if i == 6 else 0,
            }
            for i in range(10)
        ]
        sampled = select_frames(rows, 2)
        ids = [r["frame_id"] for r in sampled]
        self.assertTrue({"0", "4", "6", "9"}.issubset(ids))
        self.assertEqual(len(ids), len(set(ids)))
