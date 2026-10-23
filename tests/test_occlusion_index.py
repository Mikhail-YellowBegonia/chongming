"""Conservative BVH candidate parity checks for sight-line truth."""

from __future__ import annotations

import unittest

from chongming.contracts import scene as s
from chongming.modules.scene import VisibilityModule
from chongming.rendering.occlusion_index import OcclusionIndex

IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def pose(position: tuple[float, float, float]) -> s.KinematicState:
    return s.KinematicState(position, (0.0, 0.0, 0.0), IDENTITY, (0.0, 0.0, 0.0))


def obstacle(
    name: str, position: tuple[float, float, float], geometry: s.GeometrySpec
) -> s.WorldObjectSpec:
    return s.WorldObjectSpec(
        s.WorldObjectId(name),
        name,
        s.WorldObjectKind.OBSTACLE,
        "test",
        pose(position),
        geometry,
        s.SurfaceAppearance((0.4, 0.4, 0.4), 0.8),
        True,
    )


class OcclusionIndexTests(unittest.TestCase):
    def test_candidates_are_a_superset_of_exact_hits_and_keep_identity(self) -> None:
        objects = tuple(
            obstacle("sphere", (0.0, 0.0, 5.0), s.SphereGeometry(1.0)) for _ in range(1)
        ) + (
            obstacle("box", (4.0, 0.0, 8.0), s.BoxGeometry((1.0, 1.0, 1.0))),
            obstacle(
                "mesh",
                (-4.0, 0.0, 9.0),
                s.TriangleMeshGeometry(
                    ((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
                    ((0, 1, 2), (0, 2, 3)),
                ),
            ),
        )
        index = OcclusionIndex(objects)
        for target in ((0.0, 0.0, 10.0), (4.0, 0.0, 10.0), (-4.0, 0.0, 10.0), (20.0, 0.0, 10.0)):
            candidates = index.candidates((0.0, 0.0, 0.0), target)
            exact = {
                id(entity)
                for entity in objects
                if VisibilityModule._segment_hit(entity, (0.0, 0.0, 0.0), target) is not None
            }
            self.assertTrue(exact.issubset({id(entity) for entity in candidates}))
            self.assertEqual(
                tuple(id(entity) for entity in candidates),
                tuple(id(entity) for entity in objects if entity in candidates),
            )

    def test_identity_matching_rebuilds_for_replaced_entities(self) -> None:
        first = (obstacle("a", (0.0, 0.0, 2.0), s.SphereGeometry(1.0)),)
        second = (obstacle("a", (0.0, 0.0, 20.0), s.SphereGeometry(1.0)),)
        index = OcclusionIndex(first)
        self.assertTrue(index.matches(first))
        self.assertFalse(index.matches(second))


if __name__ == "__main__":
    unittest.main()
