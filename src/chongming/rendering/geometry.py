"""Shared ray/surface intersections for logical visibility and image depth.

Ray directions need not be normalized: returned t satisfies origin + t * direction.
No camera, experiment clock, or window is owned by this module.
"""

from __future__ import annotations

import json
import math
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from chongming.contracts.model import Vec3M
from chongming.contracts.scene import (
    BoxGeometry,
    CylinderGeometry,
    ExternalAssetGeometry,
    GeometrySpec,
    KinematicState,
    SceneCameraDistortion,
    SceneCameraIntrinsics,
    SceneDistortionModel,
    SphereGeometry,
    TriangleMeshGeometry,
)

FloatArray = NDArray[np.float64]
NEAR = 1e-6

# Bundled aircraft are authored in glTF space (right-handed, +Y up) with the nose
# along a model-specific axis, and the mesh node carries the model's rotation,
# scale and translation. The body frame is x right, y forward (nose), z up, as for
# the built-in aircraft mesh. Python owns this conversion and sends the result to
# Godot, so truth geometry and the rendered mesh cannot diverge.
AIRCRAFT_ASSET_DIR = Path(__file__).parent / "godot" / "assets" / "aircraft"
AIRCRAFT_MODELS: dict[str, tuple[str, tuple[float, float, float]]] = {
    # file name, nose direction in glTF space
    "f86": ("f86triangled.glb", (1.0, 0.0, 0.0)),
    "la11": ("la11triangled.glb", (0.0, 0.0, -1.0)),
}
_GLTF_UP = np.array([0.0, 1.0, 0.0])
_GLB_INDEX_TYPES: dict[int, np.dtype[Any]] = {5123: np.dtype("<u2"), 5125: np.dtype("<u4")}


def _glb_primitives(identifier: str) -> tuple[dict[str, Any], FloatArray, NDArray[np.int64]]:
    """Return the glTF document and the first mesh's raw vertices and triangles."""
    data = (AIRCRAFT_ASSET_DIR / AIRCRAFT_MODELS[identifier][0]).read_bytes()
    magic, version, _ = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2:
        raise ValueError(f"{identifier}: not a glTF 2.0 binary")
    json_length, _ = struct.unpack_from("<I4s", data, 12)
    document = json.loads(data[20 : 20 + json_length])
    binary = memoryview(data)[28 + json_length :]

    def read(accessor_index: int, dtype: np.dtype[Any], width: int) -> NDArray[Any]:
        accessor = document["accessors"][accessor_index]
        view = document["bufferViews"][accessor["bufferView"]]
        if view.get("byteStride") or "sparse" in accessor:
            raise ValueError(f"{identifier}: strided or sparse accessors are not supported")
        offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        return np.frombuffer(binary, dtype=dtype, count=accessor["count"] * width, offset=offset)

    vertices: list[FloatArray] = []
    triangles: list[NDArray[np.int64]] = []
    base = 0
    for primitive in document["meshes"][0]["primitives"]:
        positions = read(primitive["attributes"]["POSITION"], np.dtype("<f4"), 3).reshape(-1, 3)
        index_type = _GLB_INDEX_TYPES[document["accessors"][primitive["indices"]]["componentType"]]
        triangles.append(
            read(primitive["indices"], index_type, 1).reshape(-1, 3).astype(np.int64) + base
        )
        vertices.append(positions.astype(np.float64))
        base += len(positions)
    return document, np.concatenate(vertices), np.concatenate(triangles)


def _quaternion_matrix(x: float, y: float, z: float, w: float) -> FloatArray:
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


@lru_cache(maxsize=len(AIRCRAFT_MODELS))
def asset_mesh(identifier: str) -> TriangleMeshGeometry:
    """Body-frame triangles (x right, y nose, z up) of a bundled aircraft glb."""
    nose = np.asarray(AIRCRAFT_MODELS[identifier][1], dtype=np.float64)
    document, vertices, triangles = _glb_primitives(identifier)
    nodes = [index for index, node in enumerate(document["nodes"]) if "mesh" in node]
    if len(nodes) != 1 or nodes[0] not in document["scenes"][0]["nodes"]:
        raise ValueError(f"{identifier}: expected exactly one root mesh node")
    node = document["nodes"][nodes[0]]
    scale = np.asarray(node.get("scale", (1.0, 1.0, 1.0)), dtype=np.float64)
    rotation = _quaternion_matrix(*node.get("rotation", (0.0, 0.0, 0.0, 1.0)))
    placed = (vertices * scale) @ rotation.T + np.asarray(node.get("translation", (0.0, 0.0, 0.0)))
    # Rows are the body axes in glTF space; right = nose x up keeps the frame right-handed.
    body_from_gltf = np.array([np.cross(nose, _GLTF_UP), nose, _GLTF_UP])
    if np.prod(scale) < 0:
        # A mirrored node flips winding; restore outward counter-clockwise faces.
        triangles = triangles[:, ::-1]
    return TriangleMeshGeometry(
        tuple(tuple(float(c) for c in v) for v in placed @ body_from_gltf.T),  # type: ignore[misc]
        tuple(tuple(int(i) for i in t) for t in triangles),  # type: ignore[misc]
    )


def resolve_geometry(geometry: GeometrySpec) -> GeometrySpec:
    """Replace an external asset reference by the mesh the renderer draws."""
    if isinstance(geometry, ExternalAssetGeometry):
        return asset_mesh(geometry.asset_identifier)
    return geometry


@lru_cache(maxsize=8)
def optical_rays(
    intrinsics: SceneCameraIntrinsics, distortion: SceneCameraDistortion
) -> FloatArray:
    """Invert the generating lens model at integer pixel centres.

    This is a camera-geometry utility for projection and tests.  It does not
    render pixels; the production image path is the Godot backend.
    """

    if intrinsics.image_width_px <= 0 or intrinsics.image_height_px <= 0:
        raise ValueError("camera dimensions must be positive")
    if min(intrinsics.focal_x_px, intrinsics.focal_y_px) <= 0:
        raise ValueError("camera focal lengths must be positive")
    rows, columns = np.indices(
        (intrinsics.image_height_px, intrinsics.image_width_px), dtype=np.float64
    )
    yd = (rows.ravel() - intrinsics.principal_y_px) / intrinsics.focal_y_px
    xd = (
        columns.ravel() - intrinsics.principal_x_px - intrinsics.skew_px * yd
    ) / intrinsics.focal_x_px
    x, y = xd.copy(), yd.copy()
    if distortion.model is SceneDistortionModel.BROWN_CONRADY:
        k1, k2, k3 = distortion.radial_k1_k2_k3
        p1, p2 = distortion.tangential_p1_p2
        for _ in range(30):
            r2 = x * x + y * y
            radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
            dx = 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
            dy = p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
            x, y = (xd - dx) / radial, (yd - dy) / radial
        r2 = x * x + y * y
        radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
        error_x = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x) - xd
        error_y = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y - yd
        if not np.all(np.isfinite(x + y)) or np.max(np.abs(error_x) + np.abs(error_y)) > 1e-7:
            raise ValueError("lens distortion cannot be inverted over this image")
    result = np.column_stack((x, y, np.ones_like(x)))
    result.flags.writeable = False
    return result


def surface_hits(
    origin: Vec3M,
    directions: FloatArray,
    pose: KinematicState,
    geometry: GeometrySpec,
) -> tuple[FloatArray, FloatArray]:
    """Return nearest positive depths and ENU normals; misses have infinite depth."""
    geometry = resolve_geometry(geometry)
    rotation = np.asarray(pose.rotation_enu_from_body, dtype=np.float64)
    local_origin = (np.asarray(origin) - pose.position_enu_m) @ rotation
    rays = directions @ rotation
    count = len(rays)
    normals = np.zeros((count, 3), dtype=np.float64)
    depth = np.full(count, np.inf)
    if isinstance(geometry, SphereGeometry):
        if geometry.radius_m <= 0:
            raise ValueError("sphere radius must be positive")
        a = np.einsum("ij,ij->i", rays, rays)
        b = rays @ local_origin
        c = float(local_origin @ local_origin) - geometry.radius_m**2
        discriminant = b * b - a * c
        root = np.sqrt(np.maximum(discriminant, 0.0))
        near = (-b - root) / a
        far = (-b + root) / a
        chosen = np.where(near > NEAR, near, far)
        hit = (discriminant >= 0) & (chosen > NEAR)
        depth[hit] = chosen[hit]
        normals[hit] = (local_origin + chosen[hit, None] * rays[hit]) / geometry.radius_m
    elif isinstance(geometry, BoxGeometry):
        half = np.asarray(geometry.half_extents_body_m)
        if np.any(half <= 0):
            raise ValueError("box half extents must be positive")
        near_axis = np.full_like(rays, -np.inf)
        far_axis = np.full_like(rays, np.inf)
        parallel = np.abs(rays) < 1e-12
        np.divide(-half - local_origin, rays, out=near_axis, where=~parallel)
        np.divide(half - local_origin, rays, out=far_axis, where=~parallel)
        outside_parallel = np.any(parallel & (np.abs(local_origin) > half), axis=1)
        entry_axis = np.minimum(near_axis, far_axis)
        exit_axis = np.maximum(near_axis, far_axis)
        near = np.max(entry_axis, axis=1)
        far = np.min(exit_axis, axis=1)
        chosen = np.where(near > NEAR, near, far)
        hit = ~outside_parallel & (far >= near) & (chosen > NEAR)
        depth[hit] = chosen[hit]
        points = local_origin + chosen[hit, None] * rays[hit]
        axes = np.argmax(np.abs(points / half), axis=1)
        indices = np.flatnonzero(hit)
        normals[indices, axes] = np.sign(points[np.arange(len(points)), axes])
    elif isinstance(geometry, CylinderGeometry):
        if geometry.radius_m <= 0 or geometry.height_m <= 0:
            raise ValueError("cylinder dimensions must be positive")
        radial = rays[:, 0] ** 2 + rays[:, 1] ** 2
        linear = 2.0 * (local_origin[0] * rays[:, 0] + local_origin[1] * rays[:, 1])
        constant = local_origin[0] ** 2 + local_origin[1] ** 2 - geometry.radius_m**2
        discriminant = linear * linear - 4.0 * radial * constant
        root = np.sqrt(np.maximum(discriminant, 0.0))
        near = (-linear - root) / np.where(radial > 1e-12, 2.0 * radial, 1.0)
        far = (-linear + root) / np.where(radial > 1e-12, 2.0 * radial, 1.0)
        side_depth = np.where(near > NEAR, near, far)
        side_point = local_origin + side_depth[:, None] * rays
        side_hit = (
            (radial > 1e-12)
            & (discriminant >= 0)
            & (side_depth > NEAR)
            & (side_point[:, 2] >= 0.0)
            & (side_point[:, 2] <= geometry.height_m)
        )
        depth[side_hit] = side_depth[side_hit]
        normals[side_hit, 0] = side_point[side_hit, 0] / geometry.radius_m
        normals[side_hit, 1] = side_point[side_hit, 1] / geometry.radius_m
        cap_depths = []
        for z in (0.0, geometry.height_m):
            cap = (z - local_origin[2]) / np.where(np.abs(rays[:, 2]) > 1e-12, rays[:, 2], 1.0)
            point = local_origin + cap[:, None] * rays
            cap_hit = (
                (np.abs(rays[:, 2]) > 1e-12)
                & (cap > NEAR)
                & (point[:, 0] ** 2 + point[:, 1] ** 2 <= geometry.radius_m**2)
            )
            cap_depths.append((cap, cap_hit, z))
        for cap, cap_hit, z in cap_depths:
            replace = cap_hit & (cap < depth)
            depth[replace] = cap[replace]
            normals[replace, 2] = 1.0 if z == geometry.height_m else -1.0
    else:
        assert isinstance(geometry, TriangleMeshGeometry)
        vertices = np.asarray(geometry.vertices_body_m, dtype=np.float64)
        for triangle in geometry.triangle_vertex_indices:
            if min(triangle) < 0 or max(triangle) >= len(vertices):
                raise ValueError("mesh triangle references an absent vertex")
            v0, v1, v2 = vertices[list(triangle)]
            edge1, edge2 = v1 - v0, v2 - v0
            normal = np.cross(edge1, edge2)
            length = float(np.linalg.norm(normal))
            if length < 1e-12:
                continue
            p = np.cross(rays, edge2)
            determinant = p @ edge1
            inverse = np.zeros(count)
            np.divide(1.0, determinant, out=inverse, where=np.abs(determinant) > 1e-12)
            offset = local_origin - v0
            u = (p @ offset) * inverse
            q = np.cross(offset, edge1)
            v = (rays @ q) * inverse
            distance = float(edge2 @ q) * inverse
            hit = (
                (np.abs(determinant) > 1e-12)
                & (u >= -1e-10)
                & (v >= -1e-10)
                & (u + v <= 1.0 + 1e-10)
                & (distance > NEAR)
                & (distance < depth)
            )
            depth[hit] = distance[hit]
            normals[hit] = normal / length
            # Meshes are double sided; shade the surface facing the viewer.
            reverse = hit & ((rays @ normal) > 0)
            normals[reverse] *= -1
    return depth, normals @ rotation.T


@lru_cache(maxsize=512)
def _closed_mesh(geometry: TriangleMeshGeometry) -> bool:
    edges: dict[tuple[int, int], int] = {}
    for a, b, c in geometry.triangle_vertex_indices:
        for u, v in ((a, b), (b, c), (c, a)):
            edges[u, v] = edges.get((u, v), 0) + 1
    return bool(edges) and all(
        count == 1 and edges.get((v, u)) == 1 for (u, v), count in edges.items()
    )


def contains_point(origin: Vec3M, pose: KinematicState, geometry: GeometrySpec) -> bool:
    """Containment for primitives and closed meshes, including cameras inside blockers."""
    geometry = resolve_geometry(geometry)
    point = (np.asarray(origin) - pose.position_enu_m) @ np.asarray(pose.rotation_enu_from_body)
    if isinstance(geometry, SphereGeometry):
        return bool(point @ point < geometry.radius_m**2)
    if isinstance(geometry, BoxGeometry):
        return bool(np.all(np.abs(point) < geometry.half_extents_body_m))
    if isinstance(geometry, CylinderGeometry):
        return bool(
            point[0] ** 2 + point[1] ** 2 < geometry.radius_m**2
            and 0.0 < point[2] < geometry.height_m
        )
    # A closed, consistently wound mesh subtends 4*pi steradians from inside.
    # This covers cameras/targets inside concave OSM prisms.
    assert isinstance(geometry, TriangleMeshGeometry)
    vertices = np.asarray(geometry.vertices_body_m, dtype=np.float64)
    if (
        np.any(point < vertices.min(axis=0))
        or np.any(point > vertices.max(axis=0))
        or not _closed_mesh(geometry)
    ):
        return False
    vertices = vertices - point
    if np.any(np.linalg.norm(vertices, axis=1) < 1e-9):
        return True
    triangles = vertices[np.asarray(geometry.triangle_vertex_indices)]
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    la, lb, lc = (np.linalg.norm(v, axis=1) for v in (a, b, c))
    numerator = np.einsum("ij,ij->i", a, np.cross(b, c))
    denominator = (
        la * lb * lc
        + np.einsum("ij,ij->i", a, b) * lc
        + np.einsum("ij,ij->i", b, c) * la
        + np.einsum("ij,ij->i", c, a) * lb
    )
    angle = float(np.sum(2 * np.arctan2(numerator, denominator)))
    return abs(abs(angle) - 4 * math.pi) < 1e-5


@lru_cache(maxsize=512)
def _mesh_bounds(geometry: TriangleMeshGeometry) -> tuple[Vec3M, Vec3M]:
    vertices = np.asarray(geometry.vertices_body_m)
    minimum = tuple(float(value) for value in vertices.min(axis=0))
    maximum = tuple(float(value) for value in vertices.max(axis=0))
    assert len(minimum) == len(maximum) == 3
    return minimum, maximum


def mesh_segment_may_intersect(
    origin: Vec3M, target: Vec3M, pose: KinematicState, geometry: TriangleMeshGeometry
) -> bool:
    """Conservative slab test before expensive triangle/solid-angle queries.

    This only rejects segments outside the mesh's body-space bounds. It never
    replaces the exact surface/containment tests for a possible intersection.
    """
    if not geometry.vertices_body_m:
        return False
    rotation = np.asarray(pose.rotation_enu_from_body)
    start = (np.asarray(origin) - pose.position_enu_m) @ rotation
    end = (np.asarray(target) - pose.position_enu_m) @ rotation
    minimum, maximum = _mesh_bounds(geometry)
    entry, exit = 0.0, 1.0
    for axis in range(3):
        lo, hi = minimum[axis] - 1e-7, maximum[axis] + 1e-7
        delta = end[axis] - start[axis]
        if abs(delta) < 1e-12:
            if start[axis] < lo or start[axis] > hi:
                return False
            continue
        a, b = (lo - start[axis]) / delta, (hi - start[axis]) / delta
        entry, exit = max(entry, min(a, b)), min(exit, max(a, b))
        if entry > exit:
            return False
    return True


def model_aabb_diagonal(geometry: GeometrySpec) -> float:
    """Body-space model AABB diagonal in metres, independent of target pose."""
    geometry = resolve_geometry(geometry)
    if isinstance(geometry, SphereGeometry):
        diagonal = 2 * geometry.radius_m * math.sqrt(3)
    elif isinstance(geometry, BoxGeometry):
        diagonal = 2 * math.sqrt(sum(v * v for v in geometry.half_extents_body_m))
    elif isinstance(geometry, TriangleMeshGeometry):
        diagonal = math.sqrt(
            sum(
                (
                    max(v[i] for v in geometry.vertices_body_m)
                    - min(v[i] for v in geometry.vertices_body_m)
                )
                ** 2
                for i in range(3)
            )
        )
    else:
        raise TypeError(f"unsupported target model geometry: {type(geometry).__name__}")
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise ValueError("target AABB diagonal must be finite and positive")
    return diagonal
