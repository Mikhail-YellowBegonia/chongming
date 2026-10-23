"""Landscape classification, hole topology, ground parity and offline DEM checks."""

from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon

from chongming.contracts import scene as s
from chongming.rendering.geometry import contains_point, mesh_segment_may_intersect, surface_hits
from chongming.rendering.godot import _entities, _EntityResources
from chongming.scenarios.demo import demo_spec, state
from chongming.scenarios.osm import (
    asset_bytes,
    attach_landscape,
    extrude,
    landscape_objects,
    landscape_vegetation,
    landscape_vegetation_perturbations,
    load_landscape,
    prepare_osm,
    validate_import,
)
from chongming.scenarios.osm_elevation import (
    elevation_at,
    prepare_hgt,
    terrain_from_elevation,
)
from chongming.scenarios.osm_features import area_tree_points, forest_points
from chongming.scenarios.serialization import (
    load_compiled_scenario,
    save_compiled_scenario,
    validate_scenario_spec,
)
from tests.test_osm import ASSETS, CONFIG, sample

SOURCE = (ASSETS / "osm_landscape_demo.osm").read_bytes()
POLICY = replace(CONFIG, forest_spacing_m=10.0, maximum_forest_trees=12)
GRID = s.OSMElevation(
    (-120.0, -120.0),
    60.0,
    (
        (-2.0, -1.0, 0.0, 1.0, 2.0),
        (-1.0, 0.0, 1.0, 2.0, 3.0),
        (0.0, 1.0, 8.0, 3.0, 4.0),
        (1.0, 2.0, 3.0, 4.0, 5.0),
        (2.0, 3.0, 4.0, 5.0, 6.0),
    ),
    "synthetic:CC0-grid",
    hashlib.sha256(b"synthetic-grid").hexdigest(),
    "2026-10-20",
    "synthetic-relative",
    0.0,
    "CC0-1.0",
    "Synthetic CC0 grid",
)


def prepare(elevation: s.OSMElevation | None = None) -> s.OSMImport:
    return prepare_osm(
        SOURCE, POLICY, source_url="synthetic:CC0", data_date="2026-10-20", elevation=elevation
    ).imported


class LandscapeTests(unittest.TestCase):
    def test_bounds_filter_preserves_rotated_mesh_intersections(self) -> None:
        mesh = extrude(
            s.OSMBuilding(
                1,
                ((0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)),
                3.0,
                "default",
                (("building", "yes"),),
            )
        )
        rotation = ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        pose = replace(state((11.0, -7.0, 20.0)), rotation_enu_from_body=rotation)
        transform = np.asarray(rotation)

        def world(local: np.ndarray) -> tuple[float, float, float]:
            values = local @ transform.T + pose.position_enu_m
            return float(values[0]), float(values[1]), float(values[2])

        rng = np.random.default_rng(42)
        for a, b in rng.uniform(-4.0, 7.0, (80, 2, 3)):
            origin, target = world(a), world(b)
            distance = surface_hits(origin, np.asarray([np.asarray(target) - origin]), pose, mesh)[
                0
            ][0]
            if contains_point(origin, pose, mesh) or 0.0 < distance <= 1.0:
                self.assertTrue(mesh_segment_may_intersect(origin, target, pose, mesh))
        for a, b in (((-4.0, 1.0, 1.0), (-1.0, 1.0, 1.0)), ((-4.0, -1.0, 1.0), (-4.0, 4.0, 1.0))):
            self.assertFalse(
                mesh_segment_may_intersect(world(np.array(a)), world(np.array(b)), pose, mesh)
            )

    def test_classification_cleaning_and_holes(self) -> None:
        result = prepare_osm(SOURCE, POLICY, source_url="synthetic:CC0", data_date="2026-10-20")
        imported = result.imported
        self.assertEqual([a.category for a in imported.areas], ["forest", "water", "grass"])
        self.assertEqual(imported.roads[0].width_m, 6.0)
        self.assertEqual((imported.points[0].height_m, imported.points[0].radius_m), (14.0, 4.0))
        self.assertNotIn(b"species", result.cleaned_xml)
        forest = imported.areas[0]
        self.assertEqual(len(forest.rings_enu_m), 2)
        polygon = Polygon(forest.rings_enu_m[0], forest.rings_enu_m[1:])
        mesh = next(o.geometry for o in landscape_objects(imported) if o.category == "osm-forest")
        assert isinstance(mesh, s.TriangleMeshGeometry)
        area = 0.0
        for a, b, c in mesh.triangle_vertex_indices:
            triangle = Polygon([mesh.vertices_body_m[i][:2] for i in (a, b, c)])
            self.assertTrue(polygon.covers(triangle.representative_point()))
            area += triangle.area
        self.assertAlmostEqual(area, polygon.area, places=5)
        cleaned = prepare_osm(
            result.cleaned_xml, POLICY, source_url="synthetic:CC0", data_date="2026-10-20"
        ).imported
        for field in ("roads", "points", "areas", "diagnostics"):
            self.assertEqual(getattr(imported, field), getattr(cleaned, field))

    def test_forest_distribution_is_bounded_and_respects_holes(self) -> None:
        imported = prepare()
        trees = forest_points(imported)
        self.assertEqual(trees, forest_points(imported))
        self.assertEqual(len(trees), POLICY.maximum_forest_trees)
        forest = imported.areas[0]
        polygon = Polygon(forest.rings_enu_m[0], forest.rings_enu_m[1:])
        self.assertTrue(all(polygon.contains(Point(*t.position_enu_m)) for t in trees))
        self.assertTrue(all(t.osm_node_id < 0 for t in trees))
        self.assertEqual(
            forest_points(replace(imported, config=replace(POLICY, maximum_forest_trees=0))), ()
        )

    def test_landscape_vegetation_has_deterministic_cover_and_wind_roles(self) -> None:
        imported = prepare()
        first = landscape_vegetation(imported)
        replay = landscape_vegetation(imported)
        self.assertEqual(first, replay)
        trees = [item for item in first if isinstance(item.visual, s.TreeVisualSpec)]
        shrubs = [item for item in first if isinstance(item.visual, s.ShrubVisualSpec)]
        grass = [item for item in first if isinstance(item.visual, s.GrassVisualSpec)]
        self.assertGreater(len(trees), 0)
        self.assertGreater(len(shrubs), 0)
        self.assertGreater(len(grass), 0)
        self.assertTrue(
            all(item.motion_model is s.VegetationMotionModel.WIND_DRIVEN for item in shrubs + grass)
        )
        trunks = [
            item
            for item in trees
            if isinstance(item.visual, s.TreeVisualSpec) and item.visual.part is s.TreePart.TRUNK
        ]
        canopies = [
            item
            for item in trees
            if isinstance(item.visual, s.TreeVisualSpec) and item.visual.part is s.TreePart.CANOPY
        ]
        self.assertTrue(all(item.motion_model is s.VegetationMotionModel.STATIC for item in trunks))
        self.assertTrue(
            all(item.motion_model is s.VegetationMotionModel.WIND_DRIVEN for item in canopies)
        )
        perturbations = landscape_vegetation_perturbations(first)
        self.assertEqual(
            {str(item.object_id) for item in perturbations},
            {str(item.vegetation_id) for item in canopies + shrubs + grass},
        )

    def test_each_green_landcover_generates_three_vegetation_layers(self) -> None:
        # Exercise classification as well as scattering; a park must not silently
        # turn into only a colored ground polygon or a forest-only tree layer.
        for tag, value, category in (
            ("landuse", "grass", "grass"),
            ("landuse", "meadow", "grass"),
            ("leisure", "park", "grass"),
            ("landuse", "forest", "forest"),
            ("natural", "wood", "forest"),
        ):
            with self.subTest(tag=tag, value=value):
                xml = ET.fromstring(SOURCE)
                area = xml.find("way[@id='106']")
                assert area is not None
                for old in area.findall("tag"):
                    area.remove(old)
                ET.SubElement(area, "tag", k=tag, v=value)
                imported = prepare_osm(
                    ET.tostring(xml),
                    replace(POLICY, maximum_forest_trees=512),
                    source_url="synthetic:CC0",
                    data_date="2026-10-20",
                ).imported
                selected = next(a for a in imported.areas if a.element_id.startswith("way/106/"))
                self.assertEqual(selected.category, category)
                # Isolate the selected polygon so the other forest cannot satisfy
                # the three-layer requirement on its behalf.
                isolated = replace(imported, areas=(selected,))
                vegetation = landscape_vegetation(isolated)
                self.assertEqual(
                    {type(item.visual) for item in vegetation},
                    {s.TreeVisualSpec, s.ShrubVisualSpec, s.GrassVisualSpec},
                )
                polygon = Polygon(selected.rings_enu_m[0], selected.rings_enu_m[1:])
                inferred = [
                    v for v in vegetation if not str(v.vegetation_id).startswith("osm/tree/")
                ]
                self.assertTrue(
                    all(polygon.covers(Point(*v.base_pose.position_enu_m[:2])) for v in inferred)
                )

    def test_point_forest_and_area_tree_ids_are_disjoint_and_render_semantic_trees(self) -> None:
        imported = prepare()
        areas = area_tree_points(imported)
        self.assertTrue(areas)
        self.assertEqual(areas, area_tree_points(imported))
        point_ids = {p.osm_node_id for p in imported.points}
        forest_ids = {p.osm_node_id for p in forest_points(imported)}
        area_ids = {p.osm_node_id for p in areas}
        self.assertFalse(point_ids & forest_ids or point_ids & area_ids or forest_ids & area_ids)
        vegetation = landscape_vegetation(imported)
        ids = [str(v.vegetation_id) for v in vegetation]
        self.assertEqual(len(ids), len(set(ids)))
        for prefix in ("osm/tree/", "osm/forest-tree/", "osm/area-tree/"):
            trees = [v for v in vegetation if str(v.vegetation_id).startswith(prefix)]
            self.assertTrue(trees)
            self.assertTrue(all(isinstance(v.visual, s.TreeVisualSpec) for v in trees))
            parts = set()
            for tree in trees:
                assert isinstance(tree.visual, s.TreeVisualSpec)
                parts.add(tree.visual.part)
            self.assertEqual(parts, {s.TreePart.TRUNK, s.TreePart.CANOPY})

    def test_dem_mesh_and_feature_heights_match(self) -> None:
        imported = prepare(GRID)
        terrain = terrain_from_elevation(demo_spec().scene_definition.terrain, GRID)
        self.assertEqual(terrain.base_elevation_m, -2.0)
        self.assertEqual(terrain.maximum_relief_m, 10.0)
        mesh = terrain.heightfield_geometry
        assert mesh is not None
        pose = state((-120.0, -120.0, -2.0))
        for x, y in ((-14.0, -37.0), (12.0, 31.0), (32.0, -12.0)):
            depth, _ = surface_hits((x, y, 100.0), np.array([[0.0, 0.0, -1.0]]), pose, mesh)
            self.assertAlmostEqual(100.0 - depth[0], elevation_at(imported, x, y))
        for obj in landscape_objects(imported):
            assert isinstance(obj.geometry, s.TriangleMeshGeometry)
            for x, y, z in obj.geometry.vertices_body_m:
                offset = (
                    0.08
                    if obj.category == "osm-road"
                    else 0.04
                    if obj.category == "osm-water"
                    else 0.03
                )
                self.assertAlmostEqual(z, elevation_at(imported, x, y) + offset)

    def test_scene_roundtrip_payload_and_tamper_rejection(self) -> None:
        imported = prepare(GRID)
        payload = asset_bytes(imported)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "land.json").write_bytes(payload)
            land = load_landscape("land.json", hashlib.sha256(payload).hexdigest(), root)
            spec = attach_landscape(demo_spec(), land)
            validate_scenario_spec(spec)
            save_compiled_scenario(spec, root / "scene.json")
            self.assertEqual(load_compiled_scenario(root / "scene.json", asset_root=root), spec)
            first = sample(spec)
            entities = _entities(first)
            self.assertTrue(
                any(e["object_id"].startswith("osm/road/") for e in entities if e["object_id"])
            )
            self.assertTrue(
                any(e["visual"] and e["visual"]["kind"] == "tree_canopy" for e in entities)
            )
            bad = replace(spec.scene_definition.terrain, maximum_relief_m=99.0)
            with self.assertRaisesRegex(ValueError, "OSM terrain"):
                validate_scenario_spec(
                    replace(spec, scene_definition=replace(spec.scene_definition, terrain=bad))
                )
            with self.assertRaisesRegex(ValueError, "OSM vegetation"):
                validate_scenario_spec(
                    replace(spec, scene_definition=replace(spec.scene_definition, vegetation=()))
                )

    def test_renderer_uploads_static_definitions_once_and_streams_only_poses(self) -> None:
        imported = prepare(GRID)
        payload = asset_bytes(imported)
        spec = attach_landscape(
            demo_spec(),
            s.OSMLandscape("memory:landscape.json", hashlib.sha256(payload).hexdigest(), imported),
        )
        scene = sample(spec)
        registry = _EntityResources()
        registry.begin()
        first = _entities(scene, registry)
        first_upload_count = len(registry.definitions)
        registry.finish()
        self.assertGreater(first_upload_count, 0)
        self.assertTrue(all(set(item) == {"resource", "position", "rotation"} for item in first))
        registry.begin()
        repeated = _entities(scene, registry)
        self.assertEqual(registry.definitions, [])
        self.assertEqual(first, repeated)
        registry.finish()

    def test_invalid_feature_assets_and_relations(self) -> None:
        imported = prepare(GRID)
        for grid in (
            replace(GRID, spacing_m=0.0),
            replace(GRID, heights_m=((1.0,), (2.0,))),
            replace(GRID, heights_m=((0.0, float("nan")), (1.0, 2.0))),
            replace(GRID, source_sha256="bad"),
            replace(GRID, attribution=""),
            replace(GRID, origin_enu_m=(0.0, 0.0)),
        ):
            with self.assertRaises(ValueError):
                validate_import(replace(imported, elevation=grid))
        bad = replace(imported.points[0], position_enu_m=(float("nan"), 0.0))
        with self.assertRaises(ValueError):
            validate_import(replace(imported, points=(bad,)))
        root = ET.fromstring(SOURCE)
        root.remove(next(w for w in root.findall("way") if w.get("id") == "103"))
        result = prepare_osm(
            ET.tostring(root), POLICY, source_url="test", data_date="2026-10-20"
        ).imported
        self.assertNotIn("forest", [a.category for a in result.areas])
        self.assertIn(
            s.OSMDiagnostic("relation/201", "unsupported_area_member"), result.diagnostics
        )

    def test_hgt_resampling_axis_reference_voids_and_digest(self) -> None:
        # Big-endian, north-to-south rows. Deliberately steep synthetic ramp.
        raster = np.broadcast_to(np.arange(1201, dtype=">i2")[:, None], (1201, 1201)).copy()
        raw = raster.astype(">i2").tobytes()
        source = gzip.compress(raw, mtime=0)
        config = replace(
            POLICY,
            anchor=replace(POLICY.anchor, latitude_deg=52.38, longitude_deg=9.77),
            bounds_wgs84=(9.769, 52.379, 9.771, 52.381),
        )
        grid = prepare_hgt(
            source,
            "N52E009",
            config,
            source_url="synthetic:CC0",
            data_date="2026-10-20",
            license_name="CC0-1.0",
            attribution="Synthetic CC0 grid",
        )
        self.assertEqual(grid.source_sha256, hashlib.sha256(source).hexdigest())
        self.assertAlmostEqual(grid.reference_height_m, 744.0, places=5)
        self.assertGreater(grid.heights_m[0][0], grid.heights_m[-1][0])
        self.assertAlmostEqual(
            elevation_at(replace(prepare(), elevation=grid, config=config), 0.0, 0.0), 0.0, places=3
        )
        raster[:] = -32768
        with self.assertRaisesRegex(ValueError, "void"):
            prepare_hgt(
                raster.astype(">i2").tobytes(),
                "N52E009",
                config,
                source_url="test",
                data_date="2026-10-20",
                license_name="CC0-1.0",
                attribution="Synthetic CC0 grid",
            )
        with self.assertRaisesRegex(ValueError, "cover"):
            prepare_hgt(
                raw,
                "N51E009",
                config,
                source_url="test",
                data_date="2026-10-20",
                license_name="CC0-1.0",
                attribution="Synthetic CC0 grid",
            )


if __name__ == "__main__":
    unittest.main()
