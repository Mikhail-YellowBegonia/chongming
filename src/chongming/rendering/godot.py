"""Godot 4 renderer for the virtual-scene image path.

Python remains the owner of experiment time, visibility and privileged truth.
This module only serializes an immutable scene snapshot, sends it to a long
lived Godot worker and returns tightly packed RGB8 pixels.  The deliberately
small framed protocol is the boundary for later Godot scene/material work.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from chongming.contracts.scene import (
    BoxGeometry,
    CameraStateTruth,
    ExternalAssetGeometry,
    GeometrySpec,
    GrassVisualSpec,
    KinematicState,
    LogicalSceneSnapshot,
    ShrubVisualSpec,
    SphereGeometry,
    TreeVisualSpec,
    TriangleMeshGeometry,
)

_REQUEST_HEADER = struct.Struct("<I")
_RESPONSE_HEADER = struct.Struct("<QQ")
_MAX_REQUEST = 256 * 1024 * 1024
GODOT_VERSION = "4.7.2"
_EXTERNAL_PROJECT_ENV = "CHONGMING_GODOT_EXTERNAL_PROJECT"
_EXTERNAL_SCENE_ENV = "CHONGMING_GODOT_EXTERNAL_SCENE"


def _rotation(state: KinematicState) -> list[float]:
    return [float(value) for row in state.rotation_enu_from_body for value in row]


def _entity(
    entity_type: int,
    position: tuple[float, float, float],
    state: KinematicState,
    geometry: GeometrySpec,
    appearance: Any,
    *,
    object_id: str | None = None,
    visual: dict[str, Any] | None = None,
    surface_kind: str | None = None,
    surface_detail: float = 0.0,
    category: str | None = None,
    resources: _EntityResources | None = None,
) -> dict[str, Any]:
    if resources is not None:
        return resources.encode(
            entity_type,
            position,
            state,
            geometry,
            appearance,
            object_id=object_id,
            visual=visual,
            surface_kind=surface_kind,
            surface_detail=surface_detail,
            category=category,
        )
    payload: dict[str, Any] = {
        "type": entity_type,
        "position": [float(value) for value in position],
        "rotation": _rotation(state),
        "color": [float(value) for value in appearance.linear_rgb],
        "reflectance": float(appearance.diffuse_reflectance),
        "parameters": [],
        "vertices": None,
        "triangles": None,
        "object_id": object_id,
        "visual": visual,
        "surface": surface_kind,
        "surface_detail": surface_detail,
        "category": category,
    }
    if visual is not None:
        # Godot generates this visual model from semantic dimensions.
        pass
    elif isinstance(geometry, SphereGeometry):
        payload["parameters"] = [float(geometry.radius_m)]
    elif isinstance(geometry, BoxGeometry):
        payload["parameters"] = [float(value) for value in geometry.half_extents_body_m]
    elif isinstance(geometry, TriangleMeshGeometry):
        payload["vertices"] = [
            [float(value) for value in vertex] for vertex in geometry.vertices_body_m
        ]
        payload["triangles"] = [list(triangle) for triangle in geometry.triangle_vertex_indices]
    elif isinstance(geometry, ExternalAssetGeometry):
        payload["asset_model"] = geometry.asset_identifier
    else:  # pragma: no cover - GeometrySpec is a closed union
        raise TypeError(f"unsupported geometry: {type(geometry).__name__}")
    return payload


class _EntityResources:
    """One renderer's bounded resource registry; immutable geometry is compared by identity."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[Any, ...]] = {}
        self._serial = 0
        self.definitions: list[dict[str, Any]] = []
        self.active: set[str] = set()

    def begin(self) -> None:
        self.definitions = []
        self.active = set()

    def encode(
        self,
        entity_type: int,
        position: tuple[float, float, float],
        state: KinematicState,
        geometry: GeometrySpec,
        appearance: Any,
        *,
        object_id: str | None,
        visual: dict[str, Any] | None,
        surface_kind: str | None,
        surface_detail: float,
        category: str | None,
    ) -> dict[str, Any]:
        if object_id is None:
            raise ValueError("streamed render entities require stable IDs")
        old = self._items.get(object_id)
        semantics = (entity_type, appearance, visual, surface_kind, surface_detail, category)
        if old is None or old[0] is not geometry or old[1] != semantics:
            self._serial += 1
            token = str(self._serial)
            definition = _entity(
                entity_type,
                position,
                state,
                geometry,
                appearance,
                object_id=object_id,
                visual=visual,
                surface_kind=surface_kind,
                surface_detail=surface_detail,
                category=category,
            )
            definition["resource"] = token
            self.definitions.append(definition)
            self._items[object_id] = (geometry, semantics, token)
        else:
            token = old[2]
        self.active.add(object_id)
        return {"resource": token, "position": list(position), "rotation": _rotation(state)}

    def finish(self) -> None:
        self._items = {key: value for key, value in self._items.items() if key in self.active}


def _entities(
    scene: LogicalSceneSnapshot,
    resources: _EntityResources | None = None,
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    terrain = scene.terrain
    if terrain.heightfield_geometry is not None and terrain.heightfield_origin_enu_m is not None:
        east, north = terrain.heightfield_origin_enu_m
        pose = KinematicState(
            position_enu_m=(east, north, terrain.base_elevation_m),
            velocity_enu_mps=(0.0, 0.0, 0.0),
            rotation_enu_from_body=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            angular_velocity_body_radps=(0.0, 0.0, 0.0),
        )
        entities.append(
            _entity(
                2,
                pose.position_enu_m,
                pose,
                terrain.heightfield_geometry,
                terrain.appearance,
                object_id=str(terrain.terrain_id),
                surface_kind="terrain",
                surface_detail=scene.render_effects.terrain_detail_strength,
                category="terrain",
                resources=resources,
            )
        )
    for world_item in scene.world_objects:
        entities.append(
            _entity(
                _geometry_type(world_item.geometry),
                world_item.pose.position_enu_m,
                world_item.pose,
                world_item.geometry,
                world_item.appearance,
                object_id=str(world_item.object_id),
                category=world_item.category,
                resources=resources,
            )
        )
    for vegetation_item in scene.vegetation:
        visual = _vegetation_visual(vegetation_item, scene.render_effects)
        entities.append(
            _entity(
                _geometry_type(vegetation_item.geometry),
                vegetation_item.base_pose.position_enu_m,
                vegetation_item.base_pose,
                vegetation_item.geometry,
                vegetation_item.appearance,
                object_id=str(vegetation_item.vegetation_id),
                visual=visual,
                category="vegetation",
                resources=resources,
            )
        )
    for flying_item in scene.flying_objects:
        entities.append(
            _entity(
                _geometry_type(flying_item.geometry),
                flying_item.state.position_enu_m,
                flying_item.state,
                flying_item.geometry,
                flying_item.appearance,
                object_id=f"aircraft/{flying_item.truth_object_id}",
                resources=resources,
            )
        )
    return entities


def _vegetation_visual(item: Any, effects: Any) -> dict[str, Any] | None:
    """Describe vegetation to Godot without shipping its Python mesh.

    The logical cylinder remains available to the truth resolver.  It is deliberately
    not part of the render protocol: Godot owns the high-detail visual model.
    """
    visual = getattr(item, "visual", None)
    if visual is None:
        return None
    payload = {
        "seed": int.from_bytes(
            hashlib.blake2s(str(item.vegetation_id).encode(), digest_size=4).digest(), "little"
        ),
        "lod_near": effects.vegetation_lod_near_m,
        "lod_far": effects.vegetation_lod_far_m,
    }
    if isinstance(visual, TreeVisualSpec):
        return {
            **payload,
            "kind": f"tree_{visual.part.value}",
            "height": visual.height_m,
            "radius": visual.radius_m,
        }
    if isinstance(visual, ShrubVisualSpec):
        return {**payload, "kind": "shrub", "height": visual.height_m, "radius": visual.radius_m}
    if isinstance(visual, GrassVisualSpec):
        return {
            **payload,
            "kind": "grass",
            "width": visual.width_m,
            "depth": visual.depth_m,
            "height": visual.blade_height_m,
            "density": visual.density_per_m2,
        }
    raise TypeError(f"unsupported vegetation visual: {type(visual).__name__}")


def _geometry_type(geometry: GeometrySpec) -> int:
    if isinstance(geometry, SphereGeometry):
        return 0
    elif isinstance(geometry, BoxGeometry):
        return 1
    elif isinstance(geometry, TriangleMeshGeometry):
        return 2
    elif isinstance(geometry, ExternalAssetGeometry):
        return 3
    else:  # pragma: no cover
        raise TypeError(f"unsupported geometry: {type(geometry).__name__}")


def _request(
    scene: LogicalSceneSnapshot, camera: CameraStateTruth, entities: list[dict[str, Any]]
) -> dict[str, Any]:
    if camera.actual_distortion.model.value != "none":
        raise ValueError("Godot renderer currently requires a distortion-free camera")
    i = camera.actual_intrinsics
    if min(i.image_width_px, i.image_height_px, i.focal_x_px, i.focal_y_px) <= 0:
        raise ValueError("camera dimensions and focal lengths must be positive")
    if abs(i.skew_px) > 1e-9 or abs(i.focal_x_px - i.focal_y_px) > 1e-9:
        raise ValueError("Godot cameras require zero skew and square pixels")
    return {
        "width": i.image_width_px,
        "height": i.image_height_px,
        "focal": [i.focal_x_px, i.focal_y_px, i.principal_x_px, i.principal_y_px],
        "skew": i.skew_px,
        "origin": list(camera.optical_state.position_enu_m),
        "rotation": _rotation(camera.optical_state),
        "sky": list(scene.illumination.sky_linear_rgb),
        "sun": list(scene.illumination.sun_direction_enu_unit),
        "illuminance": scene.illumination.illuminance_lux,
        "atmosphericVisibility": scene.illumination.atmospheric_visibility_m,
        "sunDiameter": scene.illumination.sun_angular_diameter_deg,
        "glareStrength": scene.illumination.glare_strength,
        "cloudTime": scene.sample_time.time_ns / 1e9,
        "windTime": scene.sample_time.time_ns / 1e9,
        "wind": (
            [*scene.wind.mean_velocity_enu_mps, *scene.wind.turbulent_velocity_std_enu_mps]
            if scene.wind is not None
            else None
        ),
        "clouds": [
            {
                "altitude": cloud.layer_altitude_m,
                "density": cloud.optical_density,
                "drift": list(cloud.drift_velocity_enu_mps),
            }
            for cloud in scene.clouds
        ],
        "terrainDetail": scene.render_effects.terrain_detail_strength,
        "ssaoIntensity": scene.render_effects.ssao_intensity,
        "fxaaEnabled": scene.render_effects.fxaa_enabled,
        "godRaysStrength": scene.render_effects.god_rays_strength,
        "terrainElevation": scene.terrain.base_elevation_m,
        "terrainColor": list(scene.terrain.appearance.linear_rgb),
        "terrainReflectance": scene.terrain.appearance.diffuse_reflectance,
        "entities": entities,
    }


def _editor_markers(scene: LogicalSceneSnapshot) -> list[dict[str, Any]]:
    """Serialize only the non-sensor gizmos used by the Godot freeview."""
    markers: list[dict[str, Any]] = []
    for camera in scene.cameras:
        markers.append(
            {
                "kind": "camera",
                "id": f"{camera.station_id}/{camera.camera_role.value}",
                "position": list(camera.optical_state.position_enu_m),
                "rotation": _rotation(camera.optical_state),
                "focal": [
                    camera.actual_intrinsics.focal_x_px,
                    camera.actual_intrinsics.focal_y_px,
                    camera.actual_intrinsics.principal_x_px,
                    camera.actual_intrinsics.principal_y_px,
                ],
                "width": camera.actual_intrinsics.image_width_px,
                "height": camera.actual_intrinsics.image_height_px,
                "length": 34.0 if camera.camera_role.value == "main" else 22.0,
            }
        )
    for item in scene.flying_objects:
        markers.append(
            {
                "kind": "target",
                "id": str(item.truth_object_id),
                "position": list(item.state.position_enu_m),
                "rotation": _rotation(item.state),
            }
        )
    return markers


def projected_footprints(
    scene: LogicalSceneSnapshot, camera: CameraStateTruth
) -> dict[str, tuple[float, float]]:
    """Return projection bounds for truth/monitoring; pixels come from Godot."""
    i = camera.actual_intrinsics
    camera_from_enu = tuple(zip(*camera.optical_state.rotation_enu_from_body))
    result: dict[str, tuple[float, float]] = {}
    for item in scene.flying_objects:
        if isinstance(item.geometry, SphereGeometry):
            r = item.geometry.radius_m
            local = [
                (x * r, y * r, z * r) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)
            ]
        elif isinstance(item.geometry, BoxGeometry):
            hx, hy, hz = item.geometry.half_extents_body_m
            local = [
                (x * hx, y * hy, z * hz)
                for x in (-1.0, 1.0)
                for y in (-1.0, 1.0)
                for z in (-1.0, 1.0)
            ]
        elif isinstance(item.geometry, TriangleMeshGeometry):
            local = list(item.geometry.vertices_body_m)
        else:
            raise TypeError(f"unsupported aircraft geometry: {type(item.geometry).__name__}")
        projected: list[tuple[float, float]] = []
        for point in local:
            world = tuple(
                sum(item.state.rotation_enu_from_body[row][k] * point[k] for k in range(3))
                + item.state.position_enu_m[row]
                for row in range(3)
            )
            displacement = tuple(
                world[k] - camera.optical_state.position_enu_m[k] for k in range(3)
            )
            camera_point = tuple(
                sum(camera_from_enu[row][k] * displacement[k] for k in range(3)) for row in range(3)
            )
            if camera_point[2] > 1e-6:
                x, y = camera_point[0] / camera_point[2], camera_point[1] / camera_point[2]
                projected.append(
                    (
                        i.focal_x_px * x + i.skew_px * y + i.principal_x_px,
                        i.focal_y_px * y + i.principal_y_px,
                    )
                )
        if projected:
            xs, ys = zip(*projected)
            width = max(
                0.0,
                min(float(i.image_width_px), max(xs))
                - max(0.0, min(float(i.image_width_px), min(xs))),
            )
            height = max(
                0.0,
                min(float(i.image_height_px), max(ys))
                - max(0.0, min(float(i.image_height_px), min(ys))),
            )
            if width > 0 and height > 0:
                result[str(item.truth_object_id)] = (width, height)
    return result


def srgb_to_linear(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """Decode camera RGB for energy-domain exposure integration and noise."""
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def quantize_rgb8(
    intensity: NDArray[np.float64],
    intensity_min: float,
    intensity_max: float,
    noise_std: float,
    seed: int,
    stream: str,
) -> bytes:
    if intensity_max <= intensity_min:
        raise ValueError("intensity_max must be greater than intensity_min")
    if noise_std < 0:
        raise ValueError("sensor noise standard deviation cannot be negative")
    values = intensity
    if noise_std:
        digest = __import__("hashlib").blake2b(f"{seed}:{stream}".encode(), digest_size=16).digest()
        values = values + np.random.Generator(
            np.random.PCG64(int.from_bytes(digest, "big"))
        ).normal(0.0, noise_std, values.shape)
    linear = np.clip((values - intensity_min) / (intensity_max - intensity_min), 0.0, 1.0)
    encoded = np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)
    return np.asarray(
        np.rint(encoded * 255.0),
        dtype=np.uint8,
    ).tobytes()


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError("Godot renderer closed its socket")
        data.extend(chunk)
    return bytes(data)


def _read_line(sock: socket.socket, limit: int = 256) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("Godot renderer closed its handshake")
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data)
    raise RuntimeError("Godot renderer handshake is too long")


def _godot_executable() -> str:
    configured = os.environ.get("CHONGMING_GODOT")
    if configured:
        return configured
    for name in ("godot4", "godot"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Godot 4 executable not found; set CHONGMING_GODOT or install godot4")


class GodotRenderer:
    """One persistent Godot process and one serialized TCP request stream."""

    def __init__(self, startup_timeout: float = 8.0) -> None:
        executable = _godot_executable()
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        if not version.startswith(f"{GODOT_VERSION}.stable."):
            raise RuntimeError(f"Godot {GODOT_VERSION} stable required; found {version!r}")
        self.version = version
        self._files = ExitStack()
        project = self._files.enter_context(as_file(files("chongming.rendering").joinpath("godot")))
        workspace = self._files.enter_context(tempfile.TemporaryDirectory(prefix="chongming-"))
        launch_project = Path(workspace) / "project"
        external_project = os.environ.get(_EXTERNAL_PROJECT_ENV)
        external_scene = os.environ.get(_EXTERNAL_SCENE_ENV)
        if bool(external_project) != bool(external_scene):
            raise ValueError(
                f"{_EXTERNAL_PROJECT_ENV} and {_EXTERNAL_SCENE_ENV} must be set together"
            )
        if external_project is not None and external_scene is not None:
            source = Path(external_project).expanduser().resolve()
            if not source.is_dir():
                raise ValueError(f"external Godot project does not exist: {source}")
            if not external_scene.startswith("res://"):
                raise ValueError(f"external Godot scene must use a res:// path: {external_scene!r}")
            # Overlay the package worker project on a copy of the external project.
            # The external project.godot is intentionally ignored so the worker
            # remains the main scene; all external res:// assets stay addressable.
            # Copy external project (read-only from Nix store)
            shutil.copytree(
                source,
                launch_project,
                ignore=shutil.ignore_patterns(".godot", "project.godot"),
            )

            # Make all copied content writable before overlaying worker files
            for root, dirs, filenames in os.walk(launch_project, topdown=False):
                for f in filenames:
                    os.chmod(os.path.join(root, f), 0o644)
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o755)
            os.chmod(launch_project, 0o755)

            # Overlay worker project, which may overwrite external files
            shutil.copytree(project, launch_project, dirs_exist_ok=True)
        else:
            shutil.copytree(project, launch_project)
            external_scene = None
        if external_scene is not None:
            # GLTF and other imported assets are materialized in the temporary
            # worker project, so an external project's source-side .godot
            # cache cannot be reused after copying.
            import_result = subprocess.run(
                [
                    executable,
                    "--headless",
                    "--editor",
                    "--quit",
                    "--path",
                    str(launch_project),
                    "--audio-driver",
                    "Dummy",
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if import_result.returncode != 0:
                detail = import_result.stdout.strip()
                raise RuntimeError(
                    "Godot could not import the external project"
                    f": {detail or 'unknown error'}"
                )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        token = uuid.uuid4().hex
        # A headless display selects Godot's dummy renderer. Keep a graphics
        # context with a minimized window instead; force_draw is request-driven.
        self._log = self._files.enter_context(tempfile.TemporaryFile())  # noqa: SIM115 - ExitStack
        self._process: subprocess.Popen[bytes] | None = subprocess.Popen(
            [
                executable,
                "--path",
                str(launch_project),
                "--audio-driver",
                "Dummy",
                "--rendering-method",
                "gl_compatibility",
                "--disable-render-loop",
                "--disable-vsync",
                "--max-fps",
                "100",
                "--",
                "--port",
                str(port),
                "--token",
                token,
                *(["--external-scene", external_scene] if external_scene is not None else []),
            ],
            cwd=workspace,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._resources = _EntityResources()
        deadline = time.monotonic() + startup_timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                detail = self._diagnostics()
                self.close()
                raise RuntimeError(
                    f"Godot renderer exited during startup: {detail or 'unknown error'}"
                )
            try:
                self._socket = socket.create_connection(("127.0.0.1", port), timeout=0.25)
                break
            except OSError as error:
                last_error = str(error)
                time.sleep(0.03)
        if self._socket is None:
            detail = self._diagnostics()
            self.close()
            raise RuntimeError(f"Godot renderer did not become ready: {last_error}; {detail}")
        self._socket.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            greeting = _read_line(self._socket).decode("utf-8", errors="replace").strip()
        except Exception:
            self.close()
            raise
        if greeting != f"READY {token}":
            self.close()
            raise RuntimeError(f"Godot renderer handshake failed: {greeting!r}")
        self._socket.settimeout(30)

    def _diagnostics(self) -> str:
        self._log.seek(0)
        return self._log.read().decode(errors="replace")[-8000:]

    def close(self) -> None:
        sock = getattr(self, "_socket", None)
        self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        process = getattr(self, "_process", None)
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self._process = None
        resources = getattr(self, "_files", None)
        if resources is not None:
            resources.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown order varies
        self.close()

    def render(self, scene: LogicalSceneSnapshot, camera: CameraStateTruth) -> tuple[bytes, float]:
        expected = (
            camera.actual_intrinsics.image_width_px * camera.actual_intrinsics.image_height_px * 3
        )
        with self._lock:
            sock = self._socket
            if sock is None:
                raise RuntimeError("Godot renderer is closed")
            self._resources.begin()
            entities = _entities(scene, self._resources)
            self._resources.finish()
            request = _request(scene, camera, entities)
            request["resources"] = self._resources.definitions
            payload = json.dumps(request, separators=(",", ":")).encode()
            if len(payload) >= 2**32 or len(payload) > _MAX_REQUEST:
                raise ValueError("Godot render request is too large")
            sock.sendall(_REQUEST_HEADER.pack(len(payload)) + payload)
            pixel_length, render_ns = _RESPONSE_HEADER.unpack(
                _read_exact(sock, _RESPONSE_HEADER.size)
            )
            if pixel_length != expected:
                raise RuntimeError(
                    f"Godot renderer returned {pixel_length} bytes, expected {expected}"
                )
            return _read_exact(sock, pixel_length), render_ns / 1_000_000.0

    def editor_update(
        self,
        scene: LogicalSceneSnapshot,
        camera: CameraStateTruth,
        *,
        enabled: bool = True,
        realtime: bool = True,
        reopen: bool = False,
    ) -> bool:
        """Push a scene snapshot to Godot's interactive freeview.

        The editor view is deliberately a separate protocol mode: it produces
        no sensor pixels and therefore cannot be mistaken for an experiment
        exposure. During editing Godot draws at its own display cadence;
        otherwise the view only redraws on a scene update or navigation.
        """
        with self._lock:
            sock = self._socket
            if sock is None:
                raise RuntimeError("Godot renderer is closed")
            self._resources.begin()
            entities = _entities(scene, self._resources)
            self._resources.finish()
            request = _request(scene, camera, entities)
            request["mode"] = "editor"
            request["editor_enabled"] = bool(enabled)
            request["editor_realtime"] = bool(realtime)
            request["editor_reopen"] = bool(reopen)
            request["editor_markers"] = _editor_markers(scene)
            request["resources"] = self._resources.definitions
            payload = json.dumps(request, separators=(",", ":")).encode()
            if len(payload) >= 2**32 or len(payload) > _MAX_REQUEST:
                raise ValueError("Godot editor request is too large")
            sock.sendall(_REQUEST_HEADER.pack(len(payload)) + payload)
            status_length, _render_ns = _RESPONSE_HEADER.unpack(
                _read_exact(sock, _RESPONSE_HEADER.size)
            )
            if status_length != 1:
                raise RuntimeError("Godot freeview returned an invalid status")
            return _read_exact(sock, 1) == b"\x01"


_default_renderer: GodotRenderer | None = None
_default_lock = threading.Lock()


def close_default_renderer() -> None:
    global _default_renderer
    with _default_lock:
        if _default_renderer is not None:
            _default_renderer.close()
            _default_renderer = None


atexit.register(close_default_renderer)


def default_renderer() -> GodotRenderer:
    global _default_renderer
    with _default_lock:
        if _default_renderer is None:
            _default_renderer = GodotRenderer()
        return _default_renderer


def render_rgb8(scene: LogicalSceneSnapshot, camera: CameraStateTruth) -> tuple[bytes, float]:
    return default_renderer().render(scene, camera)
