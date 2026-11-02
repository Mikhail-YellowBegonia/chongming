"""Unfiltered center diagnostics retain overlapping groups and explicit failures."""

import unittest
from dataclasses import replace

import numpy as np

from chongming.diagnose_center import intersect
from chongming.modules.fusion_postintersection import candidate_groups
from tests import test_fusion_geometry as fixture


class ExhaustiveCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture.CleanFusionTests()
        self.fixture.setUp()

    def test_subsets_are_deduplicated_without_exclusive_selection(self) -> None:
        fx = self.fixture
        window = fx.window(fx.rays())
        candidates = fx.voxels.propose(window, ())
        groups = candidate_groups(candidates)
        self.assertEqual(len(groups), 4)
        three_station_groups = candidate_groups(candidates, minimum_stations=3)
        self.assertEqual(len(three_station_groups), 1)
        self.assertTrue(all(len(ids) == 3 for ids in three_station_groups))
        duplicate = replace(candidates.candidates[0], candidate_id="duplicate")
        self.assertEqual(
            set(
                candidate_groups(
                    replace(candidates, candidates=(*candidates.candidates, duplicate))
                )
            ),
            set(groups),
        )
        rows = intersect(window, candidates, fx.policy)
        self.assertEqual(len(rows), 4)
        for row in rows:
            np.testing.assert_allclose(row["raw_position_enu_m"], fixture.TARGET, atol=1e-12)
            self.assertEqual(row["geometry_measurement"].status.value, "located")

    def test_wrong_combinations_keep_raw_positions_and_rejection_classes(self) -> None:
        fx = self.fixture
        rays = fx.rays()
        window = fx.window(rays)
        candidates = fx.voxels.propose(window, ())
        bad_ray = fx.ray("c", (0.0, 3.0, 0.1), target=(1.7, 1.4, 0.7))
        rows = intersect(replace(window, rays=(*rays[:2], bad_ray)), candidates, fx.policy)
        rejected = [row for row in rows if row["geometry_measurement"].status.value != "located"]
        self.assertTrue(rejected)
        self.assertTrue(all(row["raw_position_enu_m"] is not None for row in rejected))
        self.assertTrue(all(row["classifications"] for row in rejected))
        self.assertTrue(
            any(
                "geometry:inconsistent_exact_constraints" in row["classifications"]
                for row in rejected
            )
        )

    def test_parallel_groups_are_recorded_without_inventing_unique_position(self) -> None:
        fx = self.fixture
        window = fx.window(fx.rays())
        candidates = fx.voxels.propose(window, ())
        rays = tuple(
            fx.ray(
                str(ray.event.envelope.station_id), ray.event.ray_origin_enu_m, direction=(1, 0, 0)
            )
            for ray in window.rays
        )
        rows = intersect(replace(window, rays=rays), candidates, fx.policy)
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNone(row["raw_position_enu_m"])
            self.assertIn("raw:nonunique_solution", row["classifications"])
