"""Analytical checks for new offline comparison and stratification code."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from chongming.benchmark_center_attribution import (
    coarse_measurement,
    direct_candidates,
    scheduled_exposure,
)
from chongming.benchmark_device_extensions import classify_trace, policies, region_scores
from chongming.contracts import model as m
from chongming.modules.fusion_postintersection import ExhaustiveAssociationModule, candidate_groups
from tests import test_fusion_geometry as fixture


class FollowupTests(unittest.TestCase):
    def test_empty_window_uses_actual_exposure_for_warmup_boundary(self) -> None:
        window = {
            "key": {"start_ns": 15_000_000_000, "end_ns": 15_033_333_333},
            "reference_time_ns": 15_016_666_666,
            "rays": [],
        }
        ns = scheduled_exposure(window, [15_000_999_850])
        self.assertEqual(ns, 15_000_999_850)
        self.assertLess(ns - 1_000_000, 15_000_000_000)

    def test_motion_conditions_need_history_and_a_departure_before_return(self) -> None:
        self.assertEqual(classify_trace([(0, (0.0, 0.0))])["motion"], "uncovered")
        trace = [(i * 34_000_000, (0.0, 0.0)) for i in range(91)]
        self.assertEqual(classify_trace(trace)["recurrence"], "no_return")
        trace[35] = (35 * 34_000_000, (30.0, 0.0))
        result = classify_trace(trace)
        self.assertEqual(result["motion"], "slow")
        self.assertEqual(result["recurrence"], "return_to_region")

    def test_direct_baseline_retains_all_three_station_support(self) -> None:
        fx = fixture.CleanFusionTests()
        fx.setUp()
        window = fx.window(fx.rays())
        policy = replace(fx.policy, candidates=fx.candidate_policy)
        candidates = direct_candidates(window, policy)
        self.assertEqual(len(candidate_groups(candidates, 3)), 1)
        self.assertEqual(
            set(candidate_groups(fx.voxels.propose(window, ()), 3)),
            set(candidate_groups(candidates, 3)),
        )
        self.assertTrue(all(len(group) == 3 for group in candidate_groups(candidates, 3)))

    def test_coarse_point_residual_gate_is_not_a_continuous_fit(self) -> None:
        fx = fixture.CleanFusionTests()
        fx.setUp()
        window = fx.window(fx.rays())
        policy = replace(fx.policy, candidates=fx.candidate_policy)
        groups = ExhaustiveAssociationModule(minimum_stations=3).associate(
            window, direct_candidates(window, policy), ()
        )
        group = groups.selected_groups[0]
        exact = coarse_measurement(window, group, fixture.TARGET, policy)
        self.assertIs(exact.status, m.FusionStatus.LOCATED)
        self.assertEqual(exact.position_enu_m, fixture.TARGET)
        rejected = coarse_measurement(window, group, (100.0, 100.0, 100.0), policy)
        self.assertIsNone(rejected.position_enu_m)
        self.assertIn("excessive_residual_at_coarse_point", rejected.reason_codes)
        self.assertIsNone(exact.conditional_position_covariance_enu_m2)

    def test_new_arm_changes_only_erosion_from_tuned_mog2(self) -> None:
        variants = policies(Path.cwd())
        both = variants["mog2-on-erode3-dilate3"]
        for station, policy in both.items():
            self.assertTrue(policy["detection"]["apply_erosion"])
            restored = json.loads(json.dumps(policy))
            del restored["detection"]["apply_erosion"]
            self.assertEqual(restored, variants["mog2-tuned"][station])

    def test_region_scores_keep_the_full_matching_population(self) -> None:
        mask = np.zeros((2, 4), dtype=np.uint8)
        mask[0, 0] = 1
        row = {
            "station_id": "a",
            "warmup_excluded": False,
            "eligible_targets_px": {"inside": [0.5, 0.5], "outside": [6.0, 2.0]},
            "target_gates_px": {"inside": 1.0, "outside": 1.0},
            "predictions_px": [[0.5, 0.5]],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.jsonl"
            path.write_text(json.dumps(row) + "\n")
            result = region_scores(path, {"a": mask})
        self.assertEqual(result["inside"]["responsible_samples"], 1)
        self.assertEqual(result["inside"]["recall"], 1)
        self.assertEqual(result["outside"]["responsible_samples"], 1)
        self.assertEqual(result["outside"]["recall"], 0)
