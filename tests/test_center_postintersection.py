"""Complete-link spatial suppression and exhaustive refinement behavior."""

import json
import unittest
from dataclasses import replace
from typing import Any

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.fusion_postintersection import (
    ExhaustiveAssociationModule,
    exclusive_tracking_inputs,
    suppress_duplicates,
)
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import _decode
from tests import test_fusion_geometry as fixture


class PostIntersectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = fixture.CleanFusionTests()
        self.fx.setUp()
        self.window = self.fx.window(self.fx.rays())
        candidates = self.fx.voxels.propose(self.window, ())
        self.association = ExhaustiveAssociationModule().associate(self.window, candidates, ())
        self.measurements = tuple(
            self.fx.geometry.refine(
                self.window, replace(self.association, selected_groups=(group,)), ()
            )
            .results[0]
            .measurement
            for group in self.association.selected_groups
        )

    def item(self, key: str, x: float, **kwargs: Any) -> m.FusionMeasurement:
        return replace(
            self.measurements[0],
            measurement_id=m.MeasurementId(key),
            position_enu_m=(x, 0.0, 0.0),
            **kwargs,
        )

    def test_exhaustive_groups_refine_then_keep_three_station_representative(self) -> None:
        self.assertEqual(len(self.measurements), 4)
        kept, suppressed = suppress_duplicates(self.measurements, 3)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0].supporting_station_ids), 3)
        self.assertEqual(len(suppressed), 3)
        for removed in suppressed:
            self.assertEqual(removed.representative_measurement_id, kept[0].measurement_id)
            self.assertEqual(removed.reason, "duplicate_location")
            self.assertLessEqual(removed.distance_m, 3)
        reverse = suppress_duplicates(tuple(reversed(self.measurements)), 3)
        self.assertEqual((kept, suppressed), reverse)

    def test_complete_link_prevents_chain_merge_and_includes_boundary(self) -> None:
        a, b, c = (self.item(key, x) for key, x in (("a", 0), ("b", 3), ("c", 6)))
        kept, removed = suppress_duplicates((c, b, a), 3)
        self.assertEqual([item.measurement_id for item in kept], ["a", "c"])
        self.assertEqual(removed[0].measurement.measurement_id, "b")
        self.assertEqual(removed[0].distance_m, 3)

    def test_no_merge_across_time_or_world_and_rejections_are_preserved(self) -> None:
        a = self.item("a", 0)
        later = replace(
            a, measurement_id=m.MeasurementId("later"), reference_time_ns=a.reference_time_ns + 1
        )
        other = replace(
            a, measurement_id=m.MeasurementId("other"), world_frame_id=m.WorldFrameId("other")
        )
        rejected = replace(
            a,
            measurement_id=m.MeasurementId("failed"),
            status=m.FusionStatus.GEOMETRY_DEGENERATE,
            position_enu_m=None,
        )
        kept, removed = suppress_duplicates((a, later, other, rejected), 3)
        self.assertEqual(len(kept), 4)
        self.assertFalse(removed)

    def test_quality_prefers_residual_then_condition_without_averaging(self) -> None:
        a = self.item("a", 0, residuals_m=(0.5,), geometry_condition=1)
        b = self.item("b", 1, residuals_m=(0.2,), geometry_condition=10)
        c = self.item("c", 2, residuals_m=(0.2,), geometry_condition=2)
        kept, removed = suppress_duplicates((a, b, c), 3)
        self.assertEqual(kept, (c,))
        self.assertTrue(all(r.representative_measurement_id == "c" for r in removed))

    def test_nonpositive_or_nonfinite_distance_is_rejected(self) -> None:
        for value in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                suppress_duplicates((), value)

    def test_suppressed_measurement_roundtrip_preserves_evidence(self) -> None:
        _, removed = suppress_duplicates(self.measurements, 3)
        encoded = json.loads(_json(removed[0]))
        self.assertEqual(_decode(encoded, f.SuppressedMeasurement), removed[0])

    def test_shared_ray_conflict_excluded_only_from_tracking(self) -> None:
        a = self.item("a", 0, residuals_m=(0.1,))
        b = self.item("b", 10, residuals_m=(0.2,))
        locations, spatial_removed = suppress_duplicates((a, b), 3)
        self.assertEqual(len(locations), 2)
        self.assertFalse(spatial_removed)
        tracking, excluded = exclusive_tracking_inputs(locations)
        self.assertEqual(tracking, (a,))
        self.assertEqual(excluded[0].reason, "shared_ray_conflict")
        self.assertEqual(excluded[0].representative_measurement_id, a.measurement_id)
        self.assertEqual(excluded[0].measurement, b)
        self.assertEqual(len(locations), 2)
