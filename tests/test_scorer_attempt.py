"""Bounded evaluation attempt budgets, limits, and checkpoint persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chongming_scorer.attempt import EvaluationAttempt, EvaluationBudget, EvaluationLimit


class ScorerAttemptTests(unittest.TestCase):
    def test_100_is_allowed_and_hold_ends_at_exactly_10_seconds(self) -> None:
        budget = EvaluationBudget()
        self.assertFalse(budget.skip_tracking(100, 10_000_000_000))
        self.assertTrue(budget.skip_tracking(101, 9_999_999_999))
        with self.assertRaisesRegex(EvaluationLimit, "candidate_limit"):
            budget.skip_tracking(101, 10_000_000_000)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                EvaluationBudget(candidate_limit=value)

    def test_returned_phase_threshold_is_inclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = EvaluationAttempt(root, EvaluationBudget(phase_limit_s=2), ["a"])
            attempt.check_phase(2000.0)
            with self.assertRaisesRegex(EvaluationLimit, "phase_time_limit"):
                attempt.check_phase(2000.001)
            progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["phase"], "initialization")
            self.assertGreater(progress["rss_peak_bytes"], 0)
