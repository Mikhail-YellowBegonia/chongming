"""Physical difficulty labels are independent of pixel matching and false positives."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from chongming.difficulty_evaluation import bin_bounds, range_to_size, score


class DifficultyTests(unittest.TestCase):
    def test_range_is_euclidean_and_common_translation_does_not_change_ratio(self) -> None:
        self.assertEqual(range_to_size((0, 0, 0), (3, 4, 0), 2), 2.5)
        self.assertEqual(range_to_size((10, 20, 30), (13, 24, 30), 2), 2.5)
        for size in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                range_to_size((0, 0, 0), (3, 4, 0), size)

    def test_fixed_power_of_two_bins_have_explicit_boundaries(self) -> None:
        self.assertEqual(bin_bounds(4), (4, 8))
        self.assertEqual(bin_bounds(math.nextafter(4, 0)), (2, 4))
        self.assertEqual(bin_bounds(0.5), (0.5, 1))
        for value in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                bin_bounds(value)

    def test_false_predictions_do_not_receive_a_truth_difficulty(self) -> None:
        row = {
            "warmup_excluded": False,
            "frame_id": "a",
            "fp": 1,
            "eligible_targets_px": {"near": [0, 0], "far": [100, 0]},
            "target_gates_px": {"near": 10, "far": 10},
            "predictions_px": [[0, 0], [1000, 0]],
        }
        index = {
            "a": {
                "near": {"bin_lower": 2, "difficulty": 3},
                "far": {"bin_lower": 4, "difficulty": 5},
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.jsonl"
            path.write_text(json.dumps(row) + "\n")
            result = score(path, index)
        self.assertEqual(result["responsible_samples"], 2)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual([entry["recall"] for entry in result["bins"]], [1, 0])
        self.assertEqual(sum(entry["responsible_samples"] for entry in result["bins"]), 2)
