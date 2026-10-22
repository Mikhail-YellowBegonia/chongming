"""Offline OSM roads, tree points and polygon surfaces; no renderer ownership."""

from __future__ import annotations

import hashlib
import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import replace

from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize

from chongming.contracts import scene as s

TAGS = frozenset(
    {
        "highway",
        "width",
        "lanes",
        "natural",
        "landuse",
        "leisure",
        "water",
        "waterway",
        "leaf_type",
        "leaf_cycle",
        "diameter_crown",
    }
)
ROAD_WIDTHS = {
    "motorway": 12.0,
    "trunk": 10.0,
    "primary": 8.0,
    "secondary": 7.0,
    "tertiary": 6.0,
    "residential": 5.0,
    "unclassified": 5.0,
    "service": 3.0,
    "living_street": 4.0,
    "pedestrian": 3.0,
    "track": 2.5,
    "footway": 1.5,
    "path": 1.5,
    "cycleway": 2.0,
    "steps": 1.5,
}


def category(tags: dict[str, str]) -> str | None:
    if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
        return "water"
    if tags.get("natural") == "wood" or tags.get("landuse") == "forest":
        return "forest"
    if (
        tags.get("landuse") in {"grass", "meadow", "recreation_ground", "village_green"}
        or tags.get("natural") == "grassland"
        or tags.get("leisure") in {"park", "garden"}
    ):
        return "grass"
    return None


def prepare_features(
    content: bytes, config: s.OSMImportConfig
) -> tuple[
    tuple[s.OSMRoad, ...],
    tuple[s.OSMPointFeature, ...],
    tuple[s.OSMArea, ...],
    tuple[s.OSMDiagnostic, ...],
    bytes,
]:
    from chongming.scenarios.osm import TAGS as BUILDING_TAGS
    from chongming.scenarios.osm import _elements, _id, _metres, _tags, canonical_ring, project_enu

    ways = {}
    relations = {}
    tree_ids = set()
    for element in _elements(content):
        if element.tag == "bounds":
            continue
        identity = _id(element.attrib["id"])
        tags = {k: v for k, v in _tags(element).items() if k in (BUILDING_TAGS | TAGS)}
        if element.tag == "way":
            ways[identity] = (tuple(_id(n.attrib["ref"]) for n in element.findall("nd")), tags)
        elif element.tag == "relation" and category(tags) and tags.get("type") == "multipolygon":
            relations[identity] = (
                tuple(
                    (m.get("type"), _id(m.attrib["ref"]), m.get("role", ""))
                    for m in element.findall("member")
                ),
                tags,
            )
        elif element.tag == "node" and tags.get("natural") == "tree":
            tree_ids.add(identity)
    required_ways = {i for i, (_, t) in ways.items() if category(t) or "highway" in t}
    required_ways.update(
        ref for members, _ in relations.values() for kind, ref, _ in members if kind == "way"
    )
    ways = {i: v for i, v in ways.items() if i in required_ways}
    required_nodes = tree_ids | {ref for refs, _ in ways.values() for ref in refs}
    nodes = {}
    node_tags = {}
    for element in _elements(content):
        if element.tag == "node" and int(element.attrib["id"]) in required_nodes:
            identity = _id(element.attrib["id"])
            if identity in nodes:
                raise ValueError("duplicate referenced OSM node")
            point = (float(element.attrib["lon"]), float(element.attrib["lat"]))
            project_enu(*point, config.anchor)
            nodes[identity] = point
            node_tags[identity] = {
                k: v for k, v in _tags(element).items() if k in (BUILDING_TAGS | TAGS)
            }
    west, south, east, north = config.bounds_wgs84
    selection = box(west, south, east, north)
    roads = []
    points = []
    areas = []
    diagnostics = []
    member_ids = {
        ref for members, _ in relations.values() for kind, ref, _ in members if kind == "way"
    }

    def add_area(identity: str, polygon: Polygon, tags: dict[str, str]) -> None:
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError("invalid_area_polygon")
        clipped = polygon.intersection(selection)
        polygons = (
            [clipped] if clipped.geom_type == "Polygon" else list(getattr(clipped, "geoms", ()))
        )
        rings_list = []
        for p in polygons:
            if not isinstance(p, Polygon) or p.area == 0:
                continue
            outer = canonical_ring(
                tuple(project_enu(x, y, config.anchor) for x, y in list(p.exterior.coords)[:-1])
            )
            holes = sorted(
                canonical_ring(
                    tuple(project_enu(x, y, config.anchor) for x, y in list(h.coords)[:-1])
                )
                for h in p.interiors
            )
            rings_list.append((outer, *holes))
        for index, rings in enumerate(sorted(rings_list)):
            areas.append(
                s.OSMArea(
                    f"{identity}/{index}", rings, str(category(tags)), tuple(sorted(tags.items()))
                )
            )

    for identity, (refs, tags) in sorted(ways.items()):
        try:
            if any(ref not in nodes for ref in refs):
                raise ValueError("missing_node")
            coords = [nodes[ref] for ref in refs]
            road_class = tags.get("highway", "")
            if road_class:
                default = ROAD_WIDTHS.get(road_class.removesuffix("_link"))
                if default is None or tags.get("area") == "yes":
                    raise ValueError("unsupported_road_class")
                if (
                    tags.get("bridge", "no") != "no"
                    or tags.get("tunnel", "no") != "no"
                    or tags.get("layer", "0") != "0"
                ):
                    raise ValueError("unsupported_raised_or_underground_road")
                if len(set(coords)) < 2:
                    raise ValueError("degenerate_road")
                # Keep one stable way ID; split segments are represented in the mesh builder.
                line = LineString(coords)
                if not line.intersects(selection):
                    continue
                width = _metres(tags.get("width"))
                if width is None or not 0.3 <= width <= 40:
                    width = default
                    diagnostics.append(s.OSMDiagnostic(f"way/{identity}", "default_road_width"))
                roads.append(
                    s.OSMRoad(
                        identity,
                        tuple(project_enu(*p, config.anchor) for p in coords),
                        width,
                        road_class,
                        tuple(sorted(tags.items())),
                    )
                )
            if category(tags) and identity not in member_ids:
                if len(refs) < 4 or refs[0] != refs[-1]:
                    raise ValueError("unclosed_area")
                add_area(f"way/{identity}", Polygon(coords), tags)
        except ValueError as error:
            diagnostics.append(s.OSMDiagnostic(f"way/{identity}", str(error)))
    for identity, (members, tags) in sorted(relations.items()):
        start_count = len(areas)
        try:
            if any(
                kind != "way" or role not in {"outer", "inner", ""} or ref not in ways
                for kind, ref, role in members
            ):
                raise ValueError("unsupported_area_member")
            boundaries: dict[str, list[LineString]] = {"outer": [], "inner": []}
            for _, ref, role in members:
                refs = ways[ref][0]
                if len(refs) < 2 or any(n not in nodes for n in refs):
                    raise ValueError("missing_area_node")
                boundaries[role or "outer"].append(LineString([nodes[n] for n in refs]))
            # polygonize joins split OSM ways without filling unclosed boundaries.
            outer = list(polygonize(boundaries["outer"]))
            inner = list(polygonize(boundaries["inner"]))
            if (
                not outer
                or sum(p.length for p in outer) + sum(p.length for p in inner)
                < sum(l.length for lines in boundaries.values() for l in lines) - 1e-10
            ):
                raise ValueError("unclosed_area_relation")
            if any(sum(p.contains(h) for p in outer) != 1 for h in inner):
                raise ValueError("orphan_area_hole")
            for index, p in enumerate(sorted(outer, key=lambda p: p.normalize().wkt)):
                holes = [list(h.exterior.coords) for h in inner if p.contains(h)]
                add_area(f"relation/{identity}/{index}", Polygon(p.exterior.coords, holes), tags)
        except ValueError as error:
            del areas[start_count:]
            diagnostics.append(s.OSMDiagnostic(f"relation/{identity}", str(error)))
    for identity in sorted(tree_ids):
        lon, lat = nodes[identity]
        if not selection.covers(Point(lon, lat)):
            continue
        tags = node_tags[identity]
        height = _metres(tags.get("height"))
        crown = _metres(tags.get("diameter_crown"))
        if height is None or not 1 <= height <= 60:
            height = 10.0
            diagnostics.append(s.OSMDiagnostic(f"node/{identity}", "default_tree_height"))
        radius = crown / 2 if crown is not None and 1 <= crown <= 40 else height * 0.25
        points.append(
            s.OSMPointFeature(
                identity,
                project_enu(lon, lat, config.anchor),
                "tree",
                height,
                radius,
                tuple(sorted(tags.items())),
            )
        )
    cleaned = ET.Element("osm", version="0.6")
    for identity, (lon, lat) in sorted(nodes.items()):
        node = ET.SubElement(cleaned, "node", id=str(identity), lon=str(lon), lat=str(lat))
        for k, v in sorted(node_tags[identity].items()):
            ET.SubElement(node, "tag", k=k, v=v)
    for identity, (refs, tags) in sorted(ways.items()):
        way = ET.SubElement(cleaned, "way", id=str(identity))
        for ref in refs:
            ET.SubElement(way, "nd", ref=str(ref))
        for k, v in sorted(tags.items()):
            ET.SubElement(way, "tag", k=k, v=v)
    for identity, (members, tags) in sorted(relations.items()):
        rel = ET.SubElement(cleaned, "relation", id=str(identity))
        for kind, ref, role in members:
            ET.SubElement(rel, "member", type=str(kind), ref=str(ref), role=role)
        for k, v in sorted(tags.items()):
            ET.SubElement(rel, "tag", k=k, v=v)
    return (
        tuple(roads),
        tuple(points),
        tuple(sorted(areas, key=lambda a: a.element_id)),
        tuple(diagnostics),
        ET.tostring(cleaned),
    )


def surface_mesh(
    rings: tuple[tuple[tuple[float, float], ...], ...],
    height: Callable[[float, float], float],
    grid: s.OSMElevation | None = None,
) -> s.TriangleMeshGeometry:
    polygon = Polygon(rings[0], rings[1:])
    if not polygon.is_valid or polygon.area <= 0:
        raise ValueError("invalid_area_polygon")
    patches = [polygon]
    if grid is not None:
        # Cut surfaces at terrain triangle edges so roads and land cover follow
        # the very same piecewise planar ground, including long forest outlines.
        patches = []
        ox, oy = grid.origin_enu_m
        spacing = grid.spacing_m
        left, bottom, right, top = polygon.bounds
        for y in range(
            max(0, math.floor((bottom - oy) / spacing)),
            min(len(grid.heights_m) - 1, math.ceil((top - oy) / spacing)),
        ):
            for x in range(
                max(0, math.floor((left - ox) / spacing)),
                min(len(grid.heights_m[0]) - 1, math.ceil((right - ox) / spacing)),
            ):
                a, b = (
                    (ox + x * spacing, oy + y * spacing),
                    (ox + (x + 1) * spacing, oy + y * spacing),
                )
                c, d = (a[0], a[1] + spacing), (b[0], b[1] + spacing)
                for cell in (Polygon([a, b, d]), Polygon([a, d, c])):
                    part = polygon.intersection(cell)
                    if isinstance(part, Polygon) and part.area > 1e-10:
                        patches.append(part)
                    elif hasattr(part, "geoms"):
                        patches.extend(
                            p for p in part.geoms if isinstance(p, Polygon) and p.area > 1e-10
                        )
    triangles = []
    for triangle in (t for p in patches for t in constrained_delaunay_triangles(p).geoms):
        assert isinstance(triangle, Polygon)
        coords = tuple((float(x), float(y)) for x, y in list(triangle.exterior.coords)[:3])
        a, b, c = coords
        if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0:
            coords = (a, c, b)
        start = min(range(3), key=coords.__getitem__)
        triangles.append(coords[start:] + coords[:start])
    vertices = sorted({p for t in triangles for p in t})
    indices = {p: i for i, p in enumerate(vertices)}
    return s.TriangleMeshGeometry(
        tuple((x, y, height(x, y)) for x, y in vertices),
        tuple((indices[a], indices[b], indices[c]) for a, b, c in sorted(triangles)),
    )


def feature_objects(imported: s.OSMImport) -> tuple[s.WorldObjectSpec, ...]:
    from chongming.scenarios.osm import project_enu
    from chongming.scenarios.osm_elevation import elevation_at

    pose = s.KinematicState(
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        (0.0, 0.0, 0.0),
    )
    objects = []
    colors = {
        "road": (0.14, 0.14, 0.14),
        "forest": (0.09, 0.19, 0.07),
        "grass": (0.22, 0.36, 0.12),
        "water": (0.06, 0.22, 0.32),
    }
    west, south, east, north = imported.config.bounds_wgs84
    selection = Polygon(
        [
            project_enu(x, y, imported.config.anchor)
            for x, y in ((west, south), (east, south), (east, north), (west, north))
        ]
    )
    for road in imported.roads:
        shape = (
            LineString(road.centerline_enu_m)
            .buffer(road.width_m / 2, join_style="bevel")
            .intersection(selection)
        )
        polygons = [shape] if shape.geom_type == "Polygon" else list(getattr(shape, "geoms", ()))
        for index, p in enumerate(sorted(polygons, key=lambda p: p.wkt)):
            if not isinstance(p, Polygon) or p.area <= 0:
                continue
            rings = tuple(
                tuple((float(x), float(y)) for x, y in list(r.coords)[:-1])
                for r in (p.exterior, *p.interiors)
            )
            mesh = surface_mesh(
                rings, lambda x, y: elevation_at(imported, x, y) + 0.08, imported.elevation
            )
            objects.append(
                s.WorldObjectSpec(
                    s.WorldObjectId(f"osm/road/{road.osm_way_id}/{index}"),
                    f"OSM road {road.osm_way_id}",
                    s.WorldObjectKind.FIXED_REFERENCE,
                    "osm-road",
                    pose,
                    mesh,
                    s.SurfaceAppearance(colors["road"], 0.9),
                    False,
                )
            )
    for area in imported.areas:
        # Land cover, including water, is draped over the coarse DEM. Inferring a
        # horizontal water level without bathymetry can bury the lake in terrain.
        offset = 0.04 if area.category == "water" else 0.03

        def height(x: float, y: float, offset: float = offset) -> float:
            return elevation_at(imported, x, y) + offset

        mesh = surface_mesh(
            area.rings_enu_m,
            height,
            imported.elevation,
        )
        objects.append(
            s.WorldObjectSpec(
                s.WorldObjectId(f"osm/area/{area.element_id}"),
                f"OSM {area.category} {area.element_id}",
                s.WorldObjectKind.FIXED_REFERENCE,
                f"osm-{area.category}",
                pose,
                mesh,
                s.SurfaceAppearance(colors[area.category], 0.9),
                False,
            )
        )
    return tuple(objects)


def forest_points(imported: s.OSMImport) -> tuple[s.OSMPointFeature, ...]:
    """Sparse deterministic stands; forest masks, holes and exclusion masks are authoritative.

    This is a bounded visual/occlusion approximation, not an inferred real tree census.
    Generated negative IDs cannot collide with positive OSM node IDs.
    """
    from shapely.ops import unary_union

    spacing, budget = imported.config.forest_spacing_m, imported.config.maximum_forest_trees
    if not budget:
        return ()
    forests = [
        Polygon(a.rings_enu_m[0], a.rings_enu_m[1:])
        for a in imported.areas
        if a.category == "forest"
    ]
    if not forests:
        return ()
    forest = unary_union(forests)
    exclusions = [Polygon(b.footprint_enu_m) for b in imported.buildings]
    exclusions.extend(
        Polygon(a.rings_enu_m[0], a.rings_enu_m[1:])
        for a in imported.areas
        if a.category == "water"
    )
    exclusions.extend(
        LineString(r.centerline_enu_m).buffer(r.width_m / 2 + 2) for r in imported.roads
    )
    exclusions.extend(Point(*p.position_enu_m).buffer(p.radius_m + 2) for p in imported.points)
    excluded = unary_union(exclusions)
    left, bottom, right, top = forest.bounds
    nx, ny = math.ceil((right - left) / spacing) + 2, math.ceil((top - bottom) / spacing) + 2
    if nx * ny > 250_000:
        raise ValueError("forest candidate budget exceeded; increase forest_spacing_m")
    candidates = []
    for ix in range(math.floor(left / spacing), math.ceil(right / spacing)):
        for iy in range(math.floor(bottom / spacing), math.ceil(top / spacing)):
            digest = hashlib.sha256(f"osm-forest-42/{ix}/{iy}".encode()).digest()
            x = (ix + 0.2 + 0.6 * int.from_bytes(digest[:4]) / 2**32) * spacing
            y = (iy + 0.2 + 0.6 * int.from_bytes(digest[4:8]) / 2**32) * spacing
            point = Point(x, y)
            if forest.contains(point) and not excluded.covers(point):
                candidates.append((digest, x, y))
    result = []
    for digest, x, y in sorted(candidates)[:budget]:
        height = 9.0 + digest[8] / 255 * 5
        result.append(
            s.OSMPointFeature(
                -int.from_bytes(digest[:8]),
                (x, y),
                "tree",
                height,
                height * 0.25,
                (("natural", "tree"),),
            )
        )
    return tuple(result)


def area_tree_points(imported: s.OSMImport) -> tuple[s.OSMPointFeature, ...]:
    """Derive sparse park/meadow trees from polygon land cover.

    OSM green polygons do not carry a tree census. These stable samples are
    deliberately marked as an approximation and never replace surveyed OSM
    ``natural=tree`` points or the denser forest stand samples.
    """
    from shapely.ops import unary_union

    areas = [
        Polygon(area.rings_enu_m[0], area.rings_enu_m[1:])
        for area in imported.areas
        if area.category == "grass"
    ]
    if not areas:
        return ()
    exclusions = [Polygon(building.footprint_enu_m).buffer(2.0) for building in imported.buildings]
    exclusions.extend(
        LineString(road.centerline_enu_m).buffer(road.width_m / 2 + 2.0)
        for road in imported.roads
    )
    exclusions.extend(
        Polygon(area.rings_enu_m[0], area.rings_enu_m[1:])
        for area in imported.areas
        if area.category == "water"
    )
    exclusions.extend(Point(*point.position_enu_m).buffer(point.radius_m + 2.0) for point in imported.points)
    excluded = unary_union(exclusions) if exclusions else None
    result: list[s.OSMPointFeature] = []
    for area in imported.areas:
        if area.category != "grass" or len(result) >= 192:
            continue
        mask: BaseGeometry = Polygon(area.rings_enu_m[0], area.rings_enu_m[1:]).buffer(-7.0)
        if excluded is not None:
            mask = mask.difference(excluded)
        if mask.is_empty:
            continue
        left, bottom, right, top = mask.bounds
        spacing = 32.0
        for ix in range(math.floor(left / spacing), math.ceil(right / spacing)):
            for iy in range(math.floor(bottom / spacing), math.ceil(top / spacing)):
                if len(result) >= 192:
                    break
                digest = hashlib.sha256(
                    f"osm-area-tree-42/{area.element_id}/{ix}/{iy}".encode()
                ).digest()
                if digest[0] / 255.0 > 0.58:
                    continue
                x = (ix + 0.2 + 0.6 * int.from_bytes(digest[1:5], "big") / 2**32) * spacing
                y = (iy + 0.2 + 0.6 * int.from_bytes(digest[5:9], "big") / 2**32) * spacing
                if not mask.covers(Point(x, y)):
                    continue
                result.append(
                    s.OSMPointFeature(
                        -(1 << 64) - int.from_bytes(digest[9:17], "big"),
                        (x, y),
                        "tree",
                        5.5 + digest[17] / 255.0 * 3.5,
                        1.5 + digest[18] / 255.0 * 1.1,
                        (("landcover", "grass"), ("source", "area")),
                    )
                )
            if len(result) >= 192:
                break
    return tuple(result)


def tree_objects(imported: s.OSMImport) -> tuple[s.VegetationSpec, ...]:
    from shapely.ops import unary_union

    from chongming.scenarios.osm_elevation import elevation_at

    objects = []
    derived_area_trees = area_tree_points(imported)
    area_tree_ids = {point.osm_node_id for point in derived_area_trees}
    tree_points = (*imported.points, *forest_points(imported), *derived_area_trees)
    for tree_point in tree_points:
        x, y = tree_point.position_enu_m
        pose = s.KinematicState(
            (x, y, elevation_at(imported, x, y)),
            (0.0, 0.0, 0.0),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            (0.0, 0.0, 0.0),
        )
        for part, offset, height, radius, color in (
            (
                s.TreePart.TRUNK,
                0.0,
                tree_point.height_m * 0.4,
                min(0.4, tree_point.radius_m * 0.1),
                (0.22, 0.12, 0.05),
            ),
            (
                s.TreePart.CANOPY,
                tree_point.height_m * 0.3,
                tree_point.height_m * 0.7,
                tree_point.radius_m,
                (0.1, 0.28, 0.06),
            ),
        ):
            if tree_point.osm_node_id in area_tree_ids:
                tree_prefix = "area-tree"
            elif tree_point.osm_node_id < 0:
                tree_prefix = "forest-tree"
            else:
                tree_prefix = "tree"
            objects.append(
                s.VegetationSpec(
                    s.VegetationId(f"osm/{tree_prefix}/{tree_point.osm_node_id}/{part.value}"),
                    f"OSM tree {tree_point.osm_node_id}",
                    replace(pose, position_enu_m=(x, y, pose.position_enu_m[2] + offset)),
                    s.CylinderGeometry(radius, height),
                    s.SurfaceAppearance(color, 0.9),
                    (
                        s.VegetationMotionModel.WIND_DRIVEN
                        if part is s.TreePart.CANOPY
                        else s.VegetationMotionModel.STATIC
                    ),
                    True,
                    s.TreeVisualSpec(part, height, radius),
                )
            )
    # Grass polygons receive bounded, deterministic patches. The polygon remains
    # the logical land-cover surface; these are only visual instances, so they do
    # not add hundreds of per-blade occlusion tests to the CPU truth path. Keep a
    # conservative exclusion mask so a square patch cannot spill into a road,
    # building, water or a neighboring forest polygon at its corners.
    exclusions = [Polygon(building.footprint_enu_m).buffer(1.5) for building in imported.buildings]
    exclusions.extend(
        LineString(road.centerline_enu_m).buffer(road.width_m / 2 + 1.5)
        for road in imported.roads
    )
    exclusions.extend(
        Polygon(area.rings_enu_m[0], area.rings_enu_m[1:]).buffer(1.5)
        for area in imported.areas
        if area.category == "water"
    )
    exclusions.extend(Point(*point.position_enu_m).buffer(point.radius_m + 1.5) for point in imported.points)
    excluded = unary_union(exclusions) if exclusions else None
    total_patches = 0
    max_total_patches = 2048
    max_area_patches = 256
    for area in imported.areas:
        if area.category not in {"grass", "forest"}:
            continue
        mask = Polygon(area.rings_enu_m[0], area.rings_enu_m[1:])
        inset: BaseGeometry = mask.buffer(-9.0)
        if excluded is not None:
            inset = inset.difference(excluded)
        if inset.is_empty:
            continue
        left, bottom, right, top = inset.bounds
        spacing = 18.0
        if (math.ceil((right - left) / spacing) + 2) * (math.ceil((top - bottom) / spacing) + 2) > 250_000:
            raise ValueError("grass candidate budget exceeded; reduce the selected area")
        patch_index = 0
        for ix in range(math.floor(left / spacing), math.ceil(right / spacing)):
            for iy in range(math.floor(bottom / spacing), math.ceil(top / spacing)):
                if patch_index >= max_area_patches or total_patches >= max_total_patches:
                    break
                digest = hashlib.sha256(
                    f"osm-grass-42/{area.element_id}/{ix}/{iy}".encode()
                ).digest()
                x = (ix + 0.25 + 0.5 * int.from_bytes(digest[:4], "big") / 2**32) * spacing
                y = (iy + 0.25 + 0.5 * int.from_bytes(digest[4:8], "big") / 2**32) * spacing
                # Keep a small deterministic gap pattern, but do not reduce
                # green areas to a handful of isolated blades.
                if digest[9] / 255.0 > 0.90 or not inset.covers(Point(x, y)):
                    continue
                width = depth = 12.0
                height = 0.24 + digest[8] / 255.0 * 0.18
                grass_pose = _grass_pose(imported, x, y, width)
                if grass_pose is None:
                    continue
                rotation = grass_pose.rotation_enu_from_body
                footprint = Polygon([
                    (x + rotation[0][0] * a + rotation[0][1] * b,
                     y + rotation[1][0] * a + rotation[1][1] * b)
                    for a, b in ((-6.0, -6.0), (6.0, -6.0), (6.0, 6.0), (-6.0, 6.0))
                ])
                if not mask.covers(footprint) or (excluded is not None and excluded.intersects(footprint)):
                    continue
                objects.append(
                    s.VegetationSpec(
                        s.VegetationId(f"osm/grass/{area.element_id}/{patch_index}"),
                        f"OSM grass {area.element_id}",
                        grass_pose,
                        s.CylinderGeometry(math.hypot(width, depth) * 0.5, height),
                        s.SurfaceAppearance((0.17, 0.34, 0.08), 0.82),
                        s.VegetationMotionModel.WIND_DRIVEN,
                        False,
                        s.GrassVisualSpec(width, depth, height, 8.0),
                    )
                )
                patch_index += 1
                total_patches += 1
            if patch_index >= max_area_patches or total_patches >= max_total_patches:
                break
    # Shrub rows are an explicitly marked approximation: OSM green polygons do
    # not identify individual shrubs, so derive a sparse deterministic layer
    # from the same masks instead of claiming these are surveyed instances.
    shrub_total = 0
    shrub_area_limit = 64
    for area in imported.areas:
        if area.category not in {"grass", "forest"} or shrub_total >= 384:
            continue
        mask = Polygon(area.rings_enu_m[0], area.rings_enu_m[1:])
        inset = mask.buffer(-6.0)
        if excluded is not None:
            inset = inset.difference(excluded)
        if inset.is_empty:
            continue
        left, bottom, right, top = inset.bounds
        local_count = 0
        spacing = 26.0
        for ix in range(math.floor(left / spacing), math.ceil(right / spacing)):
            for iy in range(math.floor(bottom / spacing), math.ceil(top / spacing)):
                if local_count >= shrub_area_limit or shrub_total >= 384:
                    break
                digest = hashlib.sha256(
                    f"osm-shrub-42/{area.element_id}/{ix}/{iy}".encode()
                ).digest()
                if digest[0] / 255.0 > 0.42:
                    continue
                x = (ix + 0.25 + 0.5 * int.from_bytes(digest[1:5], "big") / 2**32) * spacing
                y = (iy + 0.25 + 0.5 * int.from_bytes(digest[5:9], "big") / 2**32) * spacing
                candidate_point = Point(x, y)
                if not inset.covers(candidate_point):
                    continue
                radius = 0.65 + digest[9] / 255.0 * 0.65
                height = 1.2 + digest[10] / 255.0 * 1.4
                shrub_pose = _grass_pose(imported, x, y, max(2.0, radius * 2.0))
                if shrub_pose is None:
                    continue
                objects.append(
                    s.VegetationSpec(
                        s.VegetationId(f"osm/shrub/{area.element_id}/{local_count}"),
                        f"OSM inferred shrub {area.element_id}",
                        shrub_pose,
                        s.CylinderGeometry(radius, height),
                        s.SurfaceAppearance((0.10, 0.27, 0.07), 0.85),
                        s.VegetationMotionModel.WIND_DRIVEN,
                        True,
                        s.ShrubVisualSpec(height, radius),
                    )
                )
                local_count += 1
                shrub_total += 1
            if local_count >= shrub_area_limit or shrub_total >= 384:
                break
    return tuple(objects)


def _grass_pose(imported: s.OSMImport, x: float, y: float, width: float) -> s.KinematicState | None:
    """Fit one tiny grass patch to the local DEM plane; skip terrain creases.

    The coarse DEM is piecewise planar. Reject a patch crossing a sharp edge
    instead of putting a flat bed of blades above/below the ground. These are
    visual samples only; the authoritative ground remains the terrain mesh.
    """
    from chongming.scenarios.osm_elevation import elevation_at

    z = elevation_at(imported, x, y)
    dx = (elevation_at(imported, x + 0.1, y) - elevation_at(imported, x - 0.1, y)) / 0.2
    dy = (elevation_at(imported, x, y + 0.1) - elevation_at(imported, x, y - 0.1)) / 0.2
    nx, nz = math.sqrt(1 + dx * dx), math.sqrt(1 + dx * dx + dy * dy)
    axis_x = (1 / nx, 0.0, dx / nx)
    normal = (-dx / nz, -dy / nz, 1 / nz)
    axis_y = (normal[1] * axis_x[2], normal[2] * axis_x[0] - normal[0] * axis_x[2], -normal[1] * axis_x[0])
    rotation: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]] = (
        (axis_x[0], axis_y[0], normal[0]),
        (axis_x[1], axis_y[1], normal[1]),
        (axis_x[2], axis_y[2], normal[2]),
    )
    for a in (-width / 2, 0.0, width / 2):
        for b in (-width / 2, 0.0, width / 2):
            px = x + axis_x[0] * a + axis_y[0] * b
            py = y + axis_x[1] * a + axis_y[1] * b
            pz = z + axis_x[2] * a + axis_y[2] * b
            if abs(pz - elevation_at(imported, px, py)) > 0.05:
                return None
    return s.KinematicState(
        (x, y, z + 0.05), (0.0, 0.0, 0.0), rotation, (0.0, 0.0, 0.0)
    )


def validate_features(imported: s.OSMImport) -> None:
    from chongming.scenarios.osm import canonical_ring

    for road in imported.roads:
        if road.osm_way_id <= 0 or any(
            not math.isfinite(v) for p in road.centerline_enu_m for v in p
        ):
            raise ValueError("invalid OSM road coordinates/ID")
        if len(set(road.centerline_enu_m)) < 2:
            raise ValueError("degenerate OSM road")
    for point in imported.points:
        if point.osm_node_id <= 0 or any(
            not math.isfinite(v) for v in (*point.position_enu_m, point.height_m, point.radius_m)
        ):
            raise ValueError("invalid OSM tree coordinates/ID")
    for area in imported.areas:
        if any(canonical_ring(ring) != ring for ring in area.rings_enu_m):
            raise ValueError("noncanonical OSM area rings")
        polygon = Polygon(area.rings_enu_m[0], area.rings_enu_m[1:])
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError("invalid OSM area polygon")
