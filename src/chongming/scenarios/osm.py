"""Offline OSM XML preparation and verified local landscape assets.

No download, render process, experiment clock or UI is owned here. A preview
consumer may subscribe to completed imports and send landscape_objects() through
the existing scene/render boundary. Changing selection/configuration and calling
prepare_osm again replaces the preview without advancing an experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from chongming.contracts import scene as s
from chongming.contracts.model import ConfigDigest, GeodeticAnchor
from chongming.scenarios.assets import read_verified_asset, resolve_external_asset
from chongming.scenarios.osm_features import TAGS as FEATURE_TAGS

FORMAT = "osm-landscape-v2"
IMPORTER_VERSION = "chongming-osm-2"
ATTRIBUTION = "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)"
TAGS = frozenset(
    {
        "building",
        "building:part",
        "height",
        "building:levels",
        "min_height",
        "building:min_level",
        "name",
        "type",
    }
)
TAGS = TAGS | FEATURE_TAGS | {"area", "bridge", "tunnel", "layer"}
Point = tuple[float, float]


@dataclass(frozen=True)
class OSMPreparation:
    imported: s.OSMImport
    cleaned_xml: bytes
    source_bytes: int
    removed_tags: int


def _elements(content: bytes) -> Iterator[ET.Element]:
    """Stream top-level records, clearing them after the consumer has read them."""
    if b"\x00" in content:
        raise ValueError("OSM XML must use UTF-8 encoding")
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("OSM XML must not contain DTDs or entities")
    try:
        parser = ET.iterparse(io.BytesIO(content), events=("start", "end"))
        _, root = next(parser)
        if root.tag != "osm" or root.get("version") != "0.6":
            raise ValueError("expected OSM XML 0.6; convert PBF offline first")
        for event, element in parser:
            if event == "end" and element.tag in {"node", "way", "relation", "bounds"}:
                yield element
                element.clear()
                root.clear()
    except (ET.ParseError, StopIteration) as error:
        raise ValueError(f"invalid OSM XML: {error}") from error


def _tags(element: ET.Element) -> dict[str, str]:
    pairs = [(tag.attrib["k"], tag.attrib["v"]) for tag in element.findall("tag")]
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError(f"duplicate tags in {element.tag}/{element.get('id')}")
    return dict(pairs)


def _id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError("OSM IDs must be positive integers")
    return result


def validate_config(config: s.OSMImportConfig) -> None:
    anchor = config.anchor
    values = (
        *config.bounds_wgs84,
        anchor.latitude_deg,
        anchor.longitude_deg,
        anchor.height_m,
        config.default_height_m,
        config.level_height_m,
        config.maximum_height_m,
        config.base_elevation_m,
        config.forest_spacing_m,
    )
    if any(isinstance(v, bool) or not math.isfinite(v) for v in values):
        raise ValueError("OSM configuration requires finite numbers")
    west, south, east, north = config.bounds_wgs84
    if not (-180 <= west < east <= 180 and -90 < south < north < 90):
        raise ValueError("invalid WGS84 bounds; antimeridian selections are unsupported")
    if not (west <= anchor.longitude_deg <= east and south <= anchor.latitude_deg <= north):
        raise ValueError("OSM ENU anchor must lie within the selected bounds")
    if anchor.datum != "WGS84" or anchor.height_reference != "ellipsoidal":
        raise ValueError("OSM projection requires an explicit WGS84 ellipsoidal anchor")
    if not anchor.anchor_id.strip() or not anchor.datum_epoch.strip():
        raise ValueError("OSM anchor requires ID and datum epoch")
    if not (0 < config.default_height_m <= config.maximum_height_m and config.level_height_m > 0):
        raise ValueError("invalid OSM height fallback policy")
    if (
        config.forest_spacing_m < 10
        or type(config.maximum_forest_trees) is not int
        or not 0 <= config.maximum_forest_trees <= 2000
    ):
        raise ValueError("OSM forest policy requires spacing >= 10 m and budget 0..2000 trees")


def project_enu(longitude: float, latitude: float, anchor: GeodeticAnchor) -> Point:
    """WGS84 geodetic -> ECEF -> local east/north metres.

    Footprints use the anchor's ellipsoidal height. Vertical curvature is dropped;
    all buildings stand at the explicitly configured scene base_elevation_m.
    """

    def ecef(lon: float, lat: float) -> tuple[float, float, float]:
        lam, phi = math.radians(lon), math.radians(lat)
        n = 6378137.0 / math.sqrt(1 - 6.6943799901413165e-3 * math.sin(phi) ** 2)
        return (
            (n + anchor.height_m) * math.cos(phi) * math.cos(lam),
            (n + anchor.height_m) * math.cos(phi) * math.sin(lam),
            (n * (1 - 6.6943799901413165e-3) + anchor.height_m) * math.sin(phi),
        )

    if not (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    ):
        raise ValueError("invalid OSM node coordinates")
    x, y, z = (
        a - b
        for a, b in zip(
            ecef(longitude, latitude), ecef(anchor.longitude_deg, anchor.latitude_deg), strict=True
        )
    )
    lam, phi = math.radians(anchor.longitude_deg), math.radians(anchor.latitude_deg)
    return (
        round(-math.sin(lam) * x + math.cos(lam) * y, 6),
        round(
            -math.sin(phi) * math.cos(lam) * x
            - math.sin(phi) * math.sin(lam) * y
            + math.cos(phi) * z,
            6,
        ),
    )


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _intersects(a: Point, b: Point, c: Point, d: Point) -> bool:
    def on_segment(p: Point, q: Point, r: Point) -> bool:
        return (
            abs(_cross(p, q, r)) <= 1e-9
            and min(p[0], q[0]) - 1e-9 <= r[0] <= max(p[0], q[0]) + 1e-9
            and min(p[1], q[1]) - 1e-9 <= r[1] <= max(p[1], q[1]) + 1e-9
        )

    return (_cross(a, b, c) * _cross(a, b, d) < 0 and _cross(c, d, a) * _cross(c, d, b) < 0) or any(
        (on_segment(a, b, c), on_segment(a, b, d), on_segment(c, d, a), on_segment(c, d, b))
    )


def canonical_ring(points: tuple[Point, ...]) -> tuple[Point, ...]:
    """Reject damaged rings; normalize winding and start independently of OSM order."""
    if len(points) < 3 or len(set(points)) != len(points):
        raise ValueError("degenerate_or_repeated_vertices")
    if any(not math.isfinite(v) for point in points for v in point):
        raise ValueError("nonfinite_footprint")
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if j == i + 1 or (i == 0 and j == len(points) - 1):
                continue
            if _intersects(
                points[i], points[(i + 1) % len(points)], points[j], points[(j + 1) % len(points)]
            ):
                raise ValueError("self_intersection")
    # Remove straight-through collinear vertices, but reject folded edges.
    ring = list(points)
    changed = True
    while changed and len(ring) >= 3:
        changed = False
        for i in range(len(ring)):
            a, b, c = ring[i - 1], ring[i], ring[(i + 1) % len(ring)]
            if abs(_cross(a, b, c)) < 1e-9:
                if (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) <= 0:
                    raise ValueError("folded_edge")
                del ring[i]
                changed = True
                break
    area2 = sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(ring, ring[1:] + ring[:1], strict=True))
    if len(ring) < 3 or abs(area2) < 1e-6:
        raise ValueError("zero_area")
    if area2 < 0:
        ring.reverse()
    start = min(range(len(ring)), key=ring.__getitem__)
    return tuple(ring[start:] + ring[:start])


def extrude(building: s.OSMBuilding) -> s.TriangleMeshGeometry:
    """Closed concave prism, shared by Python intersections and Godot rendering."""
    ring = building.footprint_enu_m
    remaining = list(range(len(ring)))
    caps: list[tuple[int, int, int]] = []
    while len(remaining) > 3:
        for index, b in enumerate(remaining):
            a, c = remaining[index - 1], remaining[(index + 1) % len(remaining)]
            if _cross(ring[a], ring[b], ring[c]) <= 1e-9:
                continue
            if any(
                all(_cross(ring[u], ring[v], ring[p]) >= -1e-9 for u, v in ((a, b), (b, c), (c, a)))
                for p in remaining
                if p not in (a, b, c)
            ):
                continue
            caps.append((a, b, c))
            del remaining[index]
            break
        else:
            raise ValueError("triangulation_failed")
    caps.append((remaining[0], remaining[1], remaining[2]))
    n = len(ring)
    vertices = tuple((x, y, z) for z in (0.0, building.height_m) for x, y in ring)
    faces = [(c, b, a) for a, b, c in caps] + [(a + n, b + n, c + n) for a, b, c in caps]
    for a in range(n):
        b = (a + 1) % n
        faces.extend(((a, b, b + n), (a, b + n, a + n)))
    return s.TriangleMeshGeometry(vertices, tuple(faces))


def _metres(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|ft)?\s*", value)
    if match is None:
        return None
    result = float(match[1]) * (0.3048 if match[2] == "ft" else 1.0)
    return result if math.isfinite(result) else None


def _height(tags: dict[str, str], config: s.OSMImportConfig) -> tuple[float, str]:
    height = _metres(tags.get("height"))
    if height is not None and 0 < height <= config.maximum_height_m:
        return height, "height"
    levels = tags.get("building:levels", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", levels):
        height = float(levels) * config.level_height_m
        if math.isfinite(height) and 0 < height <= config.maximum_height_m:
            return height, "building:levels"
    return config.default_height_m, "default"


def prepare_osm(
    content: bytes,
    config: s.OSMImportConfig,
    *,
    source_url: str,
    data_date: str,
    preview: Callable[[s.OSMImport], None] | None = None,
    attribution: str = ATTRIBUTION,
    license_name: str = "ODbL-1.0",
    elevation: s.OSMElevation | None = None,
) -> OSMPreparation:
    """Clean local XML and publish a completed immutable import to an optional preview hook.

    Boundary policy: retain whole footprints only; crossing/enclosing buildings
    are reported, never silently clipped. Unsupported relations are preserved in
    the cleaned XML and reported so that holes cannot become solid buildings.
    """
    validate_config(config)
    date.fromisoformat(data_date)
    if not source_url.strip() or not attribution.strip() or not license_name.strip():
        raise ValueError("OSM source, attribution and license are required")
    ways: dict[int, tuple[tuple[int, ...], dict[str, str]]] = {}
    relations: dict[int, tuple[tuple[tuple[str, str, str], ...], dict[str, str]]] = {}
    removed_tags = 0
    seen: set[tuple[str, int]] = set()
    for element in _elements(content):
        if element.tag == "bounds":
            continue
        element_id = _id(element.attrib["id"])
        identity_key = (element.tag, element_id)
        # The second pass checks retained nodes only: no planet-sized node ID set.
        if element.tag != "node":
            if identity_key in seen:
                raise ValueError(f"duplicate OSM identity: {identity_key}")
            seen.add(identity_key)
        tags = _tags(element)
        removed_tags += len(set(tags) - TAGS)
        tags = {key: value for key, value in tags.items() if key in TAGS}
        if element.tag == "way" and (tags.get("building", "no") != "no" or "building:part" in tags):
            ways[element_id] = (tuple(_id(nd.attrib["ref"]) for nd in element.findall("nd")), tags)
        if element.tag == "relation" and (
            tags.get("building", "no") != "no"
            or "building:part" in tags
            or tags.get("type") in {"multipolygon", "building"}
        ):
            members = tuple(
                (m.attrib["type"], m.attrib["ref"], m.get("role", ""))
                for m in element.findall("member")
            )
            relations[element_id] = (members, tags)
    required = {node for refs, _ in ways.values() for node in refs}
    nodes: dict[int, tuple[float, float]] = {}
    for element in _elements(content):
        if element.tag == "node" and int(element.attrib["id"]) in required:
            if int(element.attrib["id"]) in nodes:
                raise ValueError("duplicate referenced OSM node")
            point = (float(element.attrib["lon"]), float(element.attrib["lat"]))
            project_enu(*point, config.anchor)  # validate before selection
            nodes[int(element.attrib["id"])] = point
    diagnostics: list[s.OSMDiagnostic] = []
    member_ways: set[int] = set()
    kept_relations = {}
    for relation_id, (members, tags) in sorted(relations.items()):
        referenced = {int(ref) for kind, ref, _ in members if kind == "way"}
        if (
            tags.get("building", "no") != "no"
            or "building:part" in tags
            or tags.get("type") == "building"
            or referenced.intersection(ways)
        ):
            member_ways.update(referenced)
            kept_relations[relation_id] = (members, tags)
            diagnostics.append(s.OSMDiagnostic(f"relation/{relation_id}", "unsupported_relation"))
    buildings = []
    west, south, east, north = config.bounds_wgs84
    for way_id, (refs, tags) in sorted(ways.items()):
        identity = f"way/{way_id}"
        try:
            if way_id in member_ways:
                raise ValueError("unsupported_relation_member")
            if "building:part" in tags:
                raise ValueError("unsupported_building_part")
            if len(refs) < 4 or refs[0] != refs[-1]:
                raise ValueError("unclosed_way")
            if any(ref not in nodes for ref in refs):
                raise ValueError("missing_node")
            coords = [nodes[ref] for ref in refs[:-1]]
            if (
                max(p[0] for p in coords) < west
                or min(p[0] for p in coords) > east
                or max(p[1] for p in coords) < south
                or min(p[1] for p in coords) > north
            ):
                raise ValueError("outside_bounds")
            if not all(west <= lon <= east and south <= lat <= north for lon, lat in coords):
                raise ValueError("boundary_crossing")
            if any(
                key in tags and _metres(tags[key]) != 0
                for key in ("min_height", "building:min_level")
            ):
                raise ValueError("unsupported_raised_building")
            ring = canonical_ring(tuple(project_enu(*p, config.anchor) for p in coords))
            height, height_source = _height(tags, config)
            building = s.OSMBuilding(
                way_id, ring, height, height_source, tuple(sorted(tags.items()))
            )
            extrude(building)  # geometry validity is required before preview publication
            buildings.append(building)
            if "height" in tags and height_source != "height":
                diagnostics.append(s.OSMDiagnostic(identity, "invalid_height_fallback"))
            if "building:levels" in tags and height_source == "default":
                diagnostics.append(s.OSMDiagnostic(identity, "invalid_levels_fallback"))
            if height_source == "default":
                diagnostics.append(s.OSMDiagnostic(identity, "default_height"))
        except ValueError as error:
            diagnostics.append(s.OSMDiagnostic(identity, str(error)))
    from chongming.scenarios.osm_features import prepare_features

    roads, points, areas, feature_diagnostics, _feature_cleaned = prepare_features(content, config)
    diagnostics.extend(feature_diagnostics)
    imported = s.OSMImport(
        FORMAT,
        IMPORTER_VERSION,
        source_url,
        data_date,
        hashlib.sha256(content).hexdigest(),
        attribution,
        license_name,
        config,
        tuple(buildings),
        tuple(sorted(diagnostics, key=lambda item: (item.element_id, item.reason))),
        roads,
        points,
        areas,
        elevation,
    )
    cleaned = ET.Element("osm", version="0.6", generator=IMPORTER_VERSION)
    for node_id, (lon, lat) in sorted(nodes.items()):
        ET.SubElement(cleaned, "node", id=str(node_id), lon=str(lon), lat=str(lat))
    for way_id, (refs, tags) in sorted(ways.items()):
        way = ET.SubElement(cleaned, "way", id=str(way_id))
        for ref in refs:
            ET.SubElement(way, "nd", ref=str(ref))
        for key, value in sorted(tags.items()):
            ET.SubElement(way, "tag", k=key, v=value)
    for relation_id, (members, tags) in sorted(kept_relations.items()):
        relation = ET.SubElement(cleaned, "relation", id=str(relation_id))
        for kind, member_ref, role in sorted(members):
            ET.SubElement(relation, "member", type=kind, ref=member_ref, role=role)
        for key, value in sorted(tags.items()):
            ET.SubElement(relation, "tag", k=key, v=value)
    # Merge feature dependencies with building records, preferring the richer tags.
    feature_root = ET.fromstring(_feature_cleaned)
    records = {(e.tag, e.get("id")): e for e in cleaned}
    records.update({(e.tag, e.get("id")): e for e in feature_root})
    cleaned.clear()
    cleaned.attrib.update(version="0.6", generator=IMPORTER_VERSION)
    for record_key in sorted(
        records, key=lambda k: ({"node": 0, "way": 1, "relation": 2}[k[0]], int(k[1] or 0))
    ):
        cleaned.append(records[record_key])
    result = OSMPreparation(
        imported,
        ET.tostring(cleaned, encoding="utf-8", xml_declaration=True),
        len(content),
        removed_tags,
    )
    validate_import(imported)
    if preview is not None:
        preview(imported)
    return result


def asset_bytes(imported: s.OSMImport) -> bytes:
    return (
        json.dumps(
            asdict(imported),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def validate_import(imported: s.OSMImport) -> None:
    if (imported.format, imported.importer_version) not in {
        (FORMAT, IMPORTER_VERSION),
        ("osm-buildings-v1", "chongming-osm-1"),
    }:
        raise ValueError("unsupported OSM asset/importer version")
    if imported.format == "osm-buildings-v1" and (
        imported.roads or imported.points or imported.areas or imported.elevation is not None
    ):
        raise ValueError("legacy OSM asset cannot contain landscape features")
    validate_config(imported.config)
    date.fromisoformat(imported.data_date)
    if (
        not imported.source_url.strip()
        or not re.fullmatch(r"[0-9a-f]{64}", imported.source_sha256)
        or not imported.license.strip()
        or not imported.attribution.strip()
    ):
        raise ValueError("invalid OSM provenance")
    ids = [building.osm_way_id for building in imported.buildings]
    if ids != sorted(set(ids)) or any(i <= 0 for i in ids):
        raise ValueError("OSM building IDs must be positive, unique and sorted")
    for building in imported.buildings:
        if canonical_ring(building.footprint_enu_m) != building.footprint_enu_m:
            raise ValueError("noncanonical OSM footprint")
        if not 0 < building.height_m <= imported.config.maximum_height_m:
            raise ValueError("invalid OSM building height")
        if tuple(sorted(dict(building.tags).items())) != building.tags or any(
            key not in TAGS for key, _ in building.tags
        ):
            raise ValueError("invalid OSM building tags")
        if (building.height_m, building.height_source) != _height(
            dict(building.tags), imported.config
        ):
            raise ValueError("OSM building height disagrees with recorded tag policy")
        tags = dict(building.tags)
        if (
            tags.get("building", "no") == "no"
            or "building:part" in tags
            or any(
                key in tags and _metres(tags[key]) != 0
                for key in ("min_height", "building:min_level")
            )
        ):
            raise ValueError("unsupported OSM building tags")
        extrude(building)
    road_ids = [road.osm_way_id for road in imported.roads]
    if road_ids != sorted(set(road_ids)):
        raise ValueError("OSM road IDs must be unique and sorted")
    for road in imported.roads:
        if len(road.centerline_enu_m) < 2 or not 0.3 <= road.width_m <= 40:
            raise ValueError("invalid OSM road")
        if not road.road_class or tuple(sorted(dict(road.tags).items())) != road.tags:
            raise ValueError("invalid OSM road tags")
    point_ids = [point.osm_node_id for point in imported.points]
    if point_ids != sorted(set(point_ids)):
        raise ValueError("OSM point IDs must be unique and sorted")
    for point in imported.points:
        if (
            point.category != "tree"
            or not 1 <= point.height_m <= 60
            or not 0 < point.radius_m <= 40
        ):
            raise ValueError("invalid OSM point feature")
    area_ids = [area.element_id for area in imported.areas]
    if area_ids != sorted(set(area_ids)):
        raise ValueError("OSM area IDs must be unique and sorted")
    for area in imported.areas:
        if area.category not in {"forest", "grass", "water"} or not area.rings_enu_m:
            raise ValueError("invalid OSM area")
    from chongming.scenarios.osm_elevation import validate_elevation
    from chongming.scenarios.osm_features import validate_features

    validate_features(imported)
    if imported.elevation is not None:
        validate_elevation(imported.elevation)
        grid = imported.elevation
        ox, oy = grid.origin_enu_m
        xmax = ox + (len(grid.heights_m[0]) - 1) * grid.spacing_m
        ymax = oy + (len(grid.heights_m) - 1) * grid.spacing_m
        west, south, east, north = imported.config.bounds_wgs84
        if any(
            not (ox - 0.01 <= x <= xmax + 0.01 and oy - 0.01 <= y <= ymax + 0.01)
            for lon, lat in ((west, south), (east, south), (east, north), (west, north))
            for x, y in (project_enu(lon, lat, imported.config.anchor),)
        ):
            raise ValueError("DEM grid must cover the full OSM selection")


def load_landscape(uri: str, digest: str, asset_root: Path) -> s.OSMLandscape:
    from chongming.scenarios.serialization import _decode

    content = read_verified_asset(resolve_external_asset(uri, asset_root), digest)
    imported: s.OSMImport = _decode(json.loads(content), s.OSMImport)
    validate_import(imported)
    return s.OSMLandscape(uri, digest, imported)


def building_objects(imported: s.OSMImport) -> tuple[s.WorldObjectSpec, ...]:
    """Preview/render integration point; creates no renderer or simulation state."""
    validate_import(imported)
    from chongming.scenarios.osm_elevation import elevation_at

    pose = s.KinematicState(
        (0.0, 0.0, imported.config.base_elevation_m),
        (0.0, 0.0, 0.0),
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        (0.0, 0.0, 0.0),
    )
    return tuple(
        s.WorldObjectSpec(
            s.WorldObjectId(f"osm/way/{b.osm_way_id}"),
            dict(b.tags).get("name", f"OSM {b.osm_way_id}"),
            s.WorldObjectKind.STATIC_STRUCTURE,
            "osm-building",
            replace(
                pose,
                position_enu_m=(
                    0.0,
                    0.0,
                    min(elevation_at(imported, x, y) for x, y in b.footprint_enu_m),
                ),
            ),
            extrude(b),
            s.SurfaceAppearance((0.58, 0.57, 0.54), 0.9),
            True,
        )
        for b in imported.buildings
    )


def landscape_objects(imported: s.OSMImport) -> tuple[s.WorldObjectSpec, ...]:
    from chongming.scenarios.osm_features import feature_objects

    return building_objects(imported) + feature_objects(imported)


def landscape_vegetation(imported: s.OSMImport) -> tuple[s.VegetationSpec, ...]:
    from chongming.scenarios.osm_features import tree_objects

    return tree_objects(imported)


def landscape_vegetation_perturbations(
    vegetation: tuple[s.VegetationSpec, ...],
) -> tuple[s.VegetationPerturbation, ...]:
    """Assign bounded deterministic sway to inferred OSM vegetation.

    OSM does not provide per-plant wind parameters.  These values are semantic
    envelopes for the existing scene wind model, not claims about surveyed trees.
    """
    perturbations = []
    for item in vegetation:
        if item.motion_model is not s.VegetationMotionModel.WIND_DRIVEN:
            continue
        visual = item.visual
        if isinstance(visual, s.TreeVisualSpec):
            amplitude = max(0.12, min(0.65, visual.height_m * 0.035))
            frequency = 0.16 if visual.part is s.TreePart.CANOPY else 0.0
        elif isinstance(visual, s.ShrubVisualSpec):
            amplitude, frequency = min(0.24, visual.height_m * 0.10), 0.32
        elif isinstance(visual, s.GrassVisualSpec):
            amplitude, frequency = min(0.10, visual.blade_height_m * 0.20), 0.75
        else:  # pragma: no cover - closed visual union
            continue
        if frequency > 0:
            perturbations.append(
                s.VegetationPerturbation(
                    s.WorldObjectId(str(item.vegetation_id)),
                    (amplitude, amplitude * 0.35, 0.0),
                    frequency,
                )
            )
    return tuple(perturbations)


def attach_landscape(spec: s.ScenarioSpec, landscape: s.OSMLandscape) -> s.ScenarioSpec:
    """Attach imported geometry and align all geographic metadata to its anchor."""
    anchor = landscape.imported.config.anchor
    scene = spec.scene_definition
    from chongming.scenarios.osm_elevation import terrain_from_elevation

    configs = []
    for config in spec.station_configs:
        updated = replace(config, geodetic_anchor=anchor)
        digest = ConfigDigest(
            hashlib.sha256(
                json.dumps(asdict(updated), sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        )
        configs.append(
            replace(
                updated,
                config_digest=digest,
                installation_uncertainty=replace(
                    updated.installation_uncertainty,
                    state_ref=replace(
                        updated.installation_uncertainty.state_ref, config_digest=digest
                    ),
                ),
            )
        )
    landscape_vegetation_items = landscape_vegetation(landscape.imported)
    existing_perturbations = tuple(
        p
        for p in spec.perturbations.vegetation
        if not str(p.object_id).startswith("osm/")
    )
    wind_perturbations = (
        landscape_vegetation_perturbations(landscape_vegetation_items)
        if spec.perturbations.wind is not None
        else ()
    )
    return replace(
        spec,
        perturbations=replace(
            spec.perturbations,
            vegetation=existing_perturbations + wind_perturbations,
        ),
        station_configs=tuple(configs),
        scene_definition=replace(
            scene,
            world=replace(scene.world, geodetic_anchor=anchor),
            osm_landscape=landscape,
            world_objects=tuple(o for o in scene.world_objects if not o.category.startswith("osm-"))
            + landscape_objects(landscape.imported),
            vegetation=tuple(
                v for v in scene.vegetation if not str(v.vegetation_id).startswith("osm/")
            )
            + landscape_vegetation_items,
            terrain=terrain_from_elevation(scene.terrain, landscape.imported.elevation),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare local OSM landscape assets (offline)")
    parser.add_argument("source", type=Path, help="fixed .osm XML file; no remote URLs")
    parser.add_argument("--config", type=Path, required=True, help="OSMImportConfig JSON")
    parser.add_argument("--source-url", required=True, help="provenance only; never fetched")
    parser.add_argument("--data-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--attribution", default=ATTRIBUTION)
    parser.add_argument("--license", default="ODbL-1.0", dest="license_name")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleaned-output", type=Path, help="optional filtered .osm XML")
    parser.add_argument("--report", type=Path, help="defaults to OUTPUT.report.json")
    parser.add_argument("--dem-hgt", type=Path, help="optional fixed local EGM96 .hgt or .hgt.gz")
    parser.add_argument("--dem-tile", help="SRTM tile name, e.g. N52E009")
    parser.add_argument("--dem-source-url", help="DEM provenance only; never fetched")
    parser.add_argument("--dem-data-date", help="DEM source date, YYYY-MM-DD")
    parser.add_argument("--dem-license", help="DEM provider license description or terms URL")
    parser.add_argument("--dem-attribution", help="DEM provider attribution")
    parser.add_argument("--dem-spacing", type=float, default=60.0)
    args = parser.parse_args()
    report_path = args.report or args.output.with_suffix(".report.json")
    paths = [
        args.source.resolve(),
        args.config.resolve(),
        args.output.resolve(),
        report_path.resolve(),
    ]
    if args.cleaned_output is not None:
        paths.append(args.cleaned_output.resolve())
    if args.dem_hgt is not None:
        paths.append(args.dem_hgt.resolve())
        if not all(
            (
                args.dem_tile,
                args.dem_source_url,
                args.dem_data_date,
                args.dem_license,
                args.dem_attribution,
            )
        ):
            parser.error("--dem-hgt requires tile, source URL/date, license and attribution")
    if len(paths) != len(set(paths)):
        parser.error("source, config and output paths must be distinct")
    from chongming.scenarios.serialization import _decode

    started = time.perf_counter()
    config: s.OSMImportConfig = _decode(json.loads(args.config.read_text()), s.OSMImportConfig)
    from chongming.scenarios.osm_elevation import prepare_hgt

    elevation = (
        prepare_hgt(
            args.dem_hgt.read_bytes(),
            args.dem_tile,
            config,
            source_url=args.dem_source_url,
            data_date=args.dem_data_date,
            spacing_m=args.dem_spacing,
            license_name=args.dem_license,
            attribution=args.dem_attribution,
        )
        if args.dem_hgt is not None
        else None
    )
    result = prepare_osm(
        args.source.read_bytes(),
        config,
        source_url=args.source_url,
        data_date=args.data_date,
        attribution=args.attribution,
        license_name=args.license_name,
        elevation=elevation,
    )
    content = asset_bytes(result.imported)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    if args.cleaned_output is not None:
        args.cleaned_output.parent.mkdir(parents=True, exist_ok=True)
        args.cleaned_output.write_bytes(result.cleaned_xml)
    report: dict[str, Any] = {
        "source_url": result.imported.source_url,
        "data_date": result.imported.data_date,
        "attribution": result.imported.attribution,
        "license": result.imported.license,
        "importer_version": IMPORTER_VERSION,
        "config": asdict(config),
        "asset_sha256": hashlib.sha256(content).hexdigest(),
        "source_sha256": result.imported.source_sha256,
        "cleaned_sha256": hashlib.sha256(result.cleaned_xml).hexdigest(),
        "source_bytes": result.source_bytes,
        "cleaned_bytes": len(result.cleaned_xml),
        "removed_tags": result.removed_tags,
        "buildings": len(result.imported.buildings),
        "roads": len(result.imported.roads),
        "tree_points": len(result.imported.points),
        "areas": len(result.imported.areas),
        "elevation": asdict(elevation) if elevation is not None else None,
        "diagnostics": [asdict(d) for d in result.imported.diagnostics],
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
