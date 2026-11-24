"""Unfiltered center diagnostics retain overlapping groups and explicit failures."""

import hashlib
import unittest
from dataclasses import replace
from typing import Any

import numpy as np

from chongming.contracts import fusion as f
from chongming.modules.fusion import CalibrationWeightingModule
from chongming.modules.fusion_association import CrossStationAssociationModule
from chongming.modules.fusion_geometry import (
    _STATION_SCORE,
    GeometryRefinementModule,
    _event_id,
    _exposure_span,
    _line_fit,
    _reference_time,
)
from chongming.modules.fusion_motion import midpoint_span
from chongming.modules.fusion_postintersection import candidate_groups
from tests import test_fusion_geometry as fixture


def intersect(
    window: f.SealedObservationWindow,
    candidates: f.CandidateBatch,
    policy: f.FusionPolicy,
) -> list[dict[str, Any]]:
    """Retain raw least-squares points even when production gates reject a group."""
    weights = CalibrationWeightingModule(rules=policy.admission.calibration_usage_rules)
    geometry = GeometryRefinementModule(
        center_session_id=window.key.center_session_id,
        world_frame_id=candidates.search.world_frame_id,
        policy=policy.geometry,
        weights=weights,
    )
    association = CrossStationAssociationModule(
        policy=policy.association, motion_policy=policy.geometry, weights=weights
    )
    available = {_event_id(ray): ray for ray in window.rays}
    rows: list[dict[str, Any]] = []
    for ids, candidate_ids in candidate_groups(candidates).items():
        chosen = tuple(available[event_id] for event_id in ids)
        reference = _reference_time(chosen)
        group = f.AssociatedRayGroup(
            hypothesis_id="exhaustive/" + hashlib.sha256(repr(ids).encode()).hexdigest(),
            candidate_id=candidate_ids[0],
            window=window.key,
            reference_time_ns=reference,
            support_exposure_span_ns=_exposure_span(chosen),
            supporting_ray_event_ids=ids,
            supporting_station_ids=tuple(ray.event.envelope.station_id for ray in chosen),
            prior_track_id=None,
            motion_alignment_model=f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR,
            score=float(len(chosen)),
            score_definition=_STATION_SCORE,
        )
        result = geometry._refine_group(window, group, available, ())
        reasons = ["geometry:" + reason.value for reason in result.reasons]
        raw_position = None
        residuals: list[float] = []
        depths: list[float] = []
        rank = None
        condition = None
        try:
            point, errors, forward, rank, singular = _line_fit(chosen)
        except np.linalg.LinAlgError:
            reasons.append("raw:solver_failure")
        else:
            residuals, depths = errors.tolist(), forward.tolist()
            if rank < 3:
                reasons.append("raw:nonunique_solution")
            else:
                raw_position = point.tolist()
                condition = float((singular[0] / singular[-1]) ** 2)
            if np.any(forward < 0):
                reasons.append("raw:nonforward_solution")
            if np.any(point < candidates.search.min_enu_m) or np.any(
                point > candidates.search.max_enu_m
            ):
                reasons.append("raw:outside_search_volume")
            if candidates.search.forward_range_m is not None:
                near, far = candidates.search.forward_range_m
                if np.any(forward < near) or np.any(forward > far):
                    reasons.append("raw:outside_forward_range")
            if float(errors.max()) > policy.association.spatial_gate_m:
                reasons.append("association:excessive_residual")
        if midpoint_span(chosen) > policy.association.temporal_gate_ns:
            reasons.append("association:temporal_gate_failed")
        gate, _, _ = association._gate(chosen, candidates.search, reference, None)
        if gate is not None:
            reasons.append("association:" + gate.value)
        rows.append(
            {
                "hypothesis_id": group.hypothesis_id,
                "reference_time_ns": reference,
                "supporting_ray_event_ids": ids,
                "candidate_ids": sorted(set(candidate_ids)),
                "raw_position_enu_m": raw_position,
                "raw_rank": rank,
                "raw_condition": condition,
                "raw_residuals_m": residuals,
                "raw_forward_depths_m": depths,
                "classifications": sorted(set(reasons)),
                "geometry_measurement": result.measurement,
            }
        )
    return rows


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
