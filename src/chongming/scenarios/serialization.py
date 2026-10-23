"""Human-editable ScenarioSpec persistence and first-party validation.

The JSON format carries short record type markers and ordinary enum strings.
This keeps nested scene records readable while allowing the loader to reject
unknown or malformed edits before a run starts.  It is a current format, not a
backwards-compatibility layer.
"""

from __future__ import annotations

import base64
import json
import math
import sys
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from functools import cache
from itertools import pairwise
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from chongming.contracts.model import CameraRole, PixelFormat
from chongming.contracts.scene import (
    SCENE_RANDOM_SEED,
    CylinderGeometry,
    GrassVisualSpec,
    ScenarioSpec,
    SceneCameraSpec,
    ShrubVisualSpec,
    TerrainModel,
    WorldObjectId,
)
from chongming.scenarios.assets import read_verified_asset, resolve_external_asset
from chongming.scenarios.uncertainty import covariance_factor

FORMAT_VERSION = "scenario-json-v1"


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "$type": type(value).__name__,
            **{field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


@cache
def _hints(cls: type[Any]) -> dict[str, Any]:
    return get_type_hints(cls)


def _decode(value: Any, annotation: Any = Any) -> Any:
    if hasattr(annotation, "__supertype__"):
        return _decode(value, annotation.__supertype__)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        if type(None) in args and value is None:
            return None
        candidates = tuple(item for item in args if item is not type(None))
        errors: list[Exception] = []
        for candidate in candidates:
            try:
                return _decode(value, candidate)
            except (TypeError, ValueError, KeyError) as error:
                errors.append(error)
        raise ValueError(
            f"cannot decode union value {value!r}: {errors[-1] if errors else value!r}"
        )
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("sequence must be a JSON array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError(f"expected {len(args)} array items, got {len(value)}")
        return tuple(_decode(item, expected) for item, expected in zip(value, args, strict=True))
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("mapping must be a JSON object")
        key_type, value_type = args or (str, Any)
        return {_decode(key, key_type): _decode(item, value_type) for key, item in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if annotation is bytes:
        if not isinstance(value, dict) or set(value) != {"$bytes"}:
            raise ValueError("expected base64 bytes")
        return base64.b64decode(value["$bytes"], validate=True)
    if annotation is Path:
        if not isinstance(value, str):
            raise ValueError("expected path string")
        return Path(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        cls = annotation
        if not isinstance(value, dict) or value.get("$type", cls.__name__) != cls.__name__:
            raise ValueError(f"expected {cls.__name__} record")
        expected = _hints(cls)
        supplied = {key: item for key, item in value.items() if key != "$type"}
        unknown = set(supplied) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"unknown fields for {cls.__name__}: {sorted(unknown)}")
        missing = {
            field.name
            for field in fields(cls)
            if field.name not in supplied
            and field.default is MISSING
            and field.default_factory is MISSING
        }
        if missing:
            raise ValueError(f"missing fields for {cls.__name__}: {sorted(missing)}")
        kwargs = {
            field.name: _decode(supplied[field.name], expected.get(field.name, Any))
            for field in fields(cls)
            if field.name in supplied
        }
        return cls(**kwargs)
    if annotation in (Any, object):
        return value
    if annotation is type(None) and value is None:
        return None
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError("expected boolean")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected integer")
        return value
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected number")
        if not math.isfinite(value):
            raise ValueError("expected finite number")
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError("expected string")
        return value
    raise ValueError(f"unsupported or invalid value for {annotation}: {value!r}")


def scenario_to_dict(spec: ScenarioSpec) -> dict[str, Any]:
    return {"format": FORMAT_VERSION, "scenario": _encode(spec)}


def scenario_from_dict(document: dict[str, Any]) -> ScenarioSpec:
    if set(document) != {"format", "scenario"}:
        raise ValueError("scenario document requires exactly format and scenario")
    if document.get("format") != FORMAT_VERSION:
        raise ValueError(f"unsupported scenario JSON format: {document.get('format')!r}")
    scenario = _decode(document.get("scenario"), ScenarioSpec)
    if not isinstance(scenario, ScenarioSpec):
        raise TypeError("document scenario is not a ScenarioSpec")
    validate_scenario_spec(scenario)
    return scenario


def save_scenario(spec: ScenarioSpec, path: Path) -> None:
    validate_scenario_spec(spec)
    from chongming.scenarios.authoring import demo_authoring, save_authoring

    if spec.scenario_id != "video-demo":
        raise ValueError("authoring export currently supports the virtual-demo scenario only")
    cameras = spec.scene_definition.cameras
    fps = round(
        cameras[0].exposure.frame_period_ns and 1_000_000_000 / cameras[0].exposure.frame_period_ns
    )
    save_authoring(
        demo_authoring(
            stations=len(spec.station_configs),
            fps=fps,
            duration_s=spec.timeline.end_time_ns / 1e9,
        ),
        path,
    )


def save_compiled_scenario(spec: ScenarioSpec, path: Path) -> None:
    """Write the verbose runtime graph for diagnostics, not authoring."""
    validate_scenario_spec(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(scenario_to_dict(spec), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def load_compiled_scenario(path: Path, *, asset_root: Path | None = None) -> ScenarioSpec:
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid scenario JSON: {error}") from error
    if not isinstance(document, dict):
        raise TypeError("scenario JSON root must be an object")
    spec = scenario_from_dict(document)
    verify_scenario_assets(spec, asset_root if asset_root is not None else path.parent)
    return spec


def load_scenario(path: Path, *, asset_root: Path | None = None) -> ScenarioSpec:
    """Load the compact external authoring document and compile it."""
    from chongming.scenarios.authoring import load_authoring

    return load_authoring(path, asset_root=asset_root)


def _rotation_is_level(rotation: tuple[tuple[float, float, float], ...]) -> bool:
    # Forward is column 3; down is column 2. Both pitch and roll must be zero.
    return (
        abs(rotation[2][2]) <= 1e-6
        and abs(rotation[2][0]) <= 1e-6
        and abs(rotation[2][1] + 1) <= 1e-6
    )


def validate_scenario_spec(spec: ScenarioSpec) -> None:
    """Validate editable configuration invariants before any rendering starts."""
    scene = spec.scene_definition
    # The typed decoder checks finite scalars, tuple dimensions and primitive
    # types for programmatic configurations as well as edited JSON.
    _decode(_encode(spec), ScenarioSpec)
    if spec.random_seed != SCENE_RANDOM_SEED:
        raise ValueError(f"virtual scenes require the fixed random_seed {SCENE_RANDOM_SEED}")
    if spec.timeline.logic_step_ns <= 0 or spec.timeline.end_time_ns < spec.timeline.start_time_ns:
        raise ValueError("invalid finite timeline")
    clock = spec.timeline.clock_domain
    if (clock.time_scale.value == "experiment_elapsed") != (clock.epoch_absolute_ns is None):
        raise ValueError("clock time scale and absolute epoch are inconsistent")
    if not scene.cameras:
        raise ValueError("scene must define at least one camera")
    if spec.imaging.exposure_samples <= 0 or spec.imaging.color_space != "srgb":
        raise ValueError("imaging requires positive exposure_samples and srgb")
    from chongming.scenarios.behaviors import validate_behavior

    for target in scene.flying_objects:
        if target.behavior is not None:
            validate_behavior(target.behavior)

    def unique(values: list[Any], label: str) -> None:
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {label}")

    unique([c.metadata.camera_id for c in scene.cameras], "camera ID")
    illumination = scene.illumination
    if (
        sum(value * value for value in illumination.sun_direction_enu_unit) <= 0
        or illumination.illuminance_lux < 0
        or any(not 0 <= value <= 1 for value in illumination.sky_linear_rgb)
        or (
            illumination.atmospheric_visibility_m is not None
            and illumination.atmospheric_visibility_m <= 0
        )
        or not 0 <= illumination.sun_angular_diameter_deg <= 5
        or not 0 <= illumination.glare_strength <= 2
    ):
        raise ValueError("invalid scene illumination/environment")
    effects = scene.render_effects
    if (
        not 0 <= effects.terrain_detail_strength <= 1
        or not 0 <= effects.ssao_intensity <= 4
        or not 0 <= effects.god_rays_strength <= 2
        or effects.vegetation_lod_near_m <= 0
        or effects.vegetation_lod_far_m <= effects.vegetation_lod_near_m
    ):
        raise ValueError("invalid render effects")
    if len(spec.perturbations.clouds) > 2 or any(
        cloud.layer_altitude_m <= 0 or not 0 <= cloud.optical_density <= 1
        for cloud in spec.perturbations.clouds
    ):
        raise ValueError("invalid cloud layers")
    camera_keys = {(camera.station_id, camera.camera_role) for camera in scene.cameras}
    unique(
        [(item.station_id, item.camera_role) for item in spec.perturbations.camera_jitter],
        "camera jitter",
    )
    for jitter in spec.perturbations.camera_jitter:
        if (
            (jitter.station_id, jitter.camera_role) not in camera_keys
            or jitter.frequency_hz <= 0
            or any(
                value < 0 for value in (*jitter.angular_amplitude_rad, *jitter.pixel_amplitude_px)
            )
            or not jitter.random_stream.strip()
        ):
            raise ValueError("invalid camera jitter")
    unique([c.station_id for c in spec.station_configs], "station config")
    unique([d.station_id for d in scene.edge_devices], "edge device")
    unique([t.truth_object_id for t in scene.flying_objects], "target ID")
    unique(
        [scene.terrain.terrain_id, *(o.object_id for o in scene.world_objects)], "world object ID"
    )
    unique([v.vegetation_id for v in scene.vegetation], "vegetation ID")
    if scene.osm_landscape is not None:
        from chongming.scenarios.osm import landscape_objects, landscape_vegetation
        from chongming.scenarios.osm_elevation import terrain_from_elevation

        landscape = scene.osm_landscape
        expected_buildings = landscape_objects(landscape.imported)
        actual_buildings = tuple(o for o in scene.world_objects if o.category.startswith("osm-"))
        if expected_buildings != actual_buildings:
            raise ValueError("OSM world geometry disagrees with the canonical asset")
        if landscape_vegetation(landscape.imported) != tuple(
            v for v in scene.vegetation if str(v.vegetation_id).startswith("osm/")
        ):
            raise ValueError("OSM vegetation disagrees with the canonical asset")
        if scene.terrain != terrain_from_elevation(scene.terrain, landscape.imported.elevation):
            raise ValueError("OSM terrain disagrees with the canonical asset")
        anchor = landscape.imported.config.anchor
        if scene.world.geodetic_anchor != anchor or any(
            c.geodetic_anchor != anchor for c in spec.station_configs
        ):
            raise ValueError("OSM and scene/station geodetic anchors must agree")
    unique(
        [
            scene.terrain.terrain_id,
            *(o.object_id for o in scene.world_objects),
            *(WorldObjectId(str(v.vegetation_id)) for v in scene.vegetation),
        ],
        "scene geometry ID",
    )
    for vegetation_item in scene.vegetation:
        if vegetation_item.motion_model.value not in {"static", "periodic", "wind_driven"}:
            raise ValueError(
                f"unsupported vegetation motion model: {vegetation_item.motion_model!r}"
            )
        visual = vegetation_item.visual
        if isinstance(visual, ShrubVisualSpec) and min(visual.height_m, visual.radius_m) <= 0:
            raise ValueError("invalid shrub visual")
        if isinstance(visual, GrassVisualSpec) and (
            min(visual.width_m, visual.depth_m, visual.blade_height_m, visual.density_per_m2) <= 0
            or visual.density_per_m2 > 200
        ):
            raise ValueError("invalid grass visual")
    vegetation_perturbation_ids = [item.object_id for item in spec.perturbations.vegetation]
    unique(vegetation_perturbation_ids, "vegetation perturbation")
    canopy_ids = {
        WorldObjectId(str(item.vegetation_id))
        for item in scene.vegetation
        if item.motion_model.value == "wind_driven"
    }
    if not set(vegetation_perturbation_ids) <= canopy_ids:
        raise ValueError("vegetation perturbations must reference a wind-driven vegetation object")
    trajectories = [c.optical_pose_trajectory for c in scene.cameras] + [
        t.trajectory for t in scene.flying_objects
    ]
    for trajectory in trajectories:
        times = [k.time_ns for k in trajectory.knots]
        if not times or any(b <= a for a, b in pairwise(times)):
            raise ValueError("trajectory knots must be nonempty and strictly ordered")
        for knot in trajectory.knots:
            rotation = knot.state.rotation_enu_from_body
            for i in range(3):
                for j in range(3):
                    if (
                        abs(sum(rotation[k][i] * rotation[k][j] for k in range(3)) - (i == j))
                        > 1e-6
                    ):
                        raise ValueError("trajectory rotation must be orthonormal")
            determinant = (
                rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
                - rotation[0][1]
                * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
                + rotation[0][2]
                * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
            )
            if abs(determinant - 1) > 1e-6:
                raise ValueError("trajectory rotation must be right handed")
    if spec.world_frame_id != scene.world.world_frame_id:
        raise ValueError("scenario and scene world_frame_id must match")
    station_ids = {item.station_id for item in scene.edge_devices}
    config_ids = {item.station_id for item in spec.station_configs}
    if station_ids != config_ids:
        raise ValueError("station configs and edge devices must have identical station IDs")
    cameras_by_station: dict[Any, list[SceneCameraSpec]] = {}
    for camera in scene.cameras:
        cameras_by_station.setdefault(camera.station_id, []).append(camera)
        intrinsics = camera.actual_intrinsics
        if intrinsics.image_width_px <= 0 or intrinsics.image_height_px <= 0:
            raise ValueError(f"camera {camera.metadata.camera_id} has invalid dimensions")
        if camera.exposure.frame_period_ns <= 0:
            raise ValueError(f"camera {camera.metadata.camera_id} has invalid frame period")
        if intrinsics.focal_x_px <= 0 or intrinsics.focal_y_px <= 0:
            raise ValueError("camera focal lengths must be positive")
        if (
            not 0 < camera.metadata.nominal_diagonal_fov_deg < 180
            or camera.metadata.nominal_frame_rate_hz <= 0
        ):
            raise ValueError("camera nominal FPS/FOV must be positive and FOV below 180 degrees")
        if not 0 <= camera.exposure.exposure_duration_ns <= camera.exposure.frame_period_ns:
            raise ValueError("exposure duration must fit within the frame period")
        standard = camera.metadata.profile_id == "standard-v1"
        experimental = camera.metadata.profile_id.startswith("experimental-")
        if not standard and not experimental:
            raise ValueError(
                f"camera {camera.metadata.camera_id} must use standard-v1 or an explicit experimental-* profile"
            )
        if standard:
            expected = (1920, 1080) if camera.camera_role is CameraRole.MAIN else (640, 480)
            expected_fov = (
                60.0
                if camera.camera_role is CameraRole.MAIN
                else math.degrees(2 * math.atan(1.25 * math.tan(math.radians(1.3))))
            )
            actual = (intrinsics.image_width_px, intrinsics.image_height_px)
            if actual != expected:
                raise ValueError(
                    f"standard camera {camera.metadata.camera_id} violates required resolution"
                )
            if abs(camera.metadata.nominal_diagonal_fov_deg - expected_fov) > 1e-6:
                raise ValueError(
                    f"standard camera {camera.metadata.camera_id} violates nominal FOV"
                )
            if (
                abs(
                    camera.exposure.frame_period_ns
                    - 1_000_000_000 / camera.metadata.nominal_frame_rate_hz
                )
                > 1
            ):
                raise ValueError("standard exposure period violates nominal FPS")
            if camera.camera_role is CameraRole.AUXILIARY and any(
                not _rotation_is_level(k.state.rotation_enu_from_body)
                for k in camera.optical_pose_trajectory.knots
            ):
                raise ValueError(
                    f"standard auxiliary camera {camera.metadata.camera_id} is not level"
                )
    if set(cameras_by_station) != station_ids:
        raise ValueError("cameras and devices must have identical station IDs")
    for station_id, cameras in cameras_by_station.items():
        roles = {camera.camera_role for camera in cameras}
        if len(cameras) != 2 or roles != {CameraRole.MAIN, CameraRole.AUXILIARY}:
            raise ValueError(f"station {station_id} must have one main and one auxiliary camera")
        device = next(d for d in scene.edge_devices if d.station_id == station_id)
        for camera in cameras:
            declared = (
                device.main_camera_id
                if camera.camera_role is CameraRole.MAIN
                else device.auxiliary_camera_id
            )
            if declared != camera.metadata.camera_id:
                raise ValueError("device camera ID does not match camera metadata")
    for config in spec.station_configs:
        if config.world_frame_id != spec.world_frame_id:
            raise ValueError("station config world frame must match scenario")
        reference = config.reference_anchor
        if getattr(reference, "image", None) is not None:
            raise ValueError("inline reference image is unsupported; use a surveyed reference")
    observations = spec.virtual_observations
    covariance_factor(
        observations.independent_angular_covariance_rad2, 2, "independent_angular_covariance_rad2"
    )
    covariance_factor(
        observations.independent_origin_covariance_m2, 3, "independent_origin_covariance_m2"
    )
    if (
        not 0 <= observations.detection_probability <= 1
        or observations.false_alarms_per_camera_hour < 0
    ):
        raise ValueError("invalid observation probability or false alarm rate")
    intervals: dict[Any, list[tuple[int, int]]] = {}
    for interval in observations.calibration_intervals:
        covariance_factor(
            interval.correction_covariance_rad2, 3, "calibration correction_covariance_rad2"
        )
        if interval.station_id not in station_ids or interval.end_time_ns <= interval.start_time_ns:
            raise ValueError("invalid calibration interval")
        intervals.setdefault(interval.station_id, []).append(
            (interval.start_time_ns, interval.end_time_ns)
        )
    for entries in intervals.values():
        ordered = sorted(entries)
        if any(b[0] < a[1] for a, b in pairwise(ordered)):
            raise ValueError("overlapping calibration intervals")
    artifacts = spec.imaging.sensor_artifacts
    if (
        not 0 <= artifacts.intensity_min < artifacts.intensity_max <= 1
        or artifacts.sensor_noise_std_intensity < 0
    ):
        raise ValueError("invalid sensor intensity/noise range")
    terrain = scene.terrain
    if terrain.maximum_relief_m < 0:
        raise ValueError("terrain relief cannot be negative")
    from chongming.contracts.scene import BoxGeometry, SphereGeometry, TriangleMeshGeometry

    geometries = (
        [o.geometry for o in scene.world_objects]
        + [o.geometry for o in scene.flying_objects]
        + [o.geometry for o in scene.vegetation]
        + ([terrain.heightfield_geometry] if terrain.heightfield_geometry is not None else [])
    )
    for geometry in geometries:
        if isinstance(geometry, SphereGeometry) and geometry.radius_m <= 0:
            raise ValueError("sphere radius must be positive")
        if isinstance(geometry, BoxGeometry) and any(v <= 0 for v in geometry.half_extents_body_m):
            raise ValueError("box extents must be positive")
        if isinstance(geometry, CylinderGeometry) and (
            geometry.radius_m <= 0 or geometry.height_m <= 0
        ):
            raise ValueError("cylinder dimensions must be positive")
        if isinstance(geometry, TriangleMeshGeometry) and (
            not geometry.vertices_body_m
            or not geometry.triangle_vertex_indices
            or any(
                len(set(triangle)) != 3
                or any(i < 0 or i >= len(geometry.vertices_body_m) for i in triangle)
                for triangle in geometry.triangle_vertex_indices
            )
        ):
            raise ValueError("mesh triangles require valid distinct vertex indices")
    for item in spec.perturbations.camera_pose:
        if item.station_id not in station_ids:
            raise ValueError("camera perturbation references an unknown station")
    if terrain.heightfield_asset_uri is not None and terrain.heightfield_asset_digest is None:
        raise ValueError("heightfield_asset_uri requires heightfield_asset_digest")
    if terrain.heightfield_asset_digest is not None and terrain.heightfield_asset_uri is None:
        raise ValueError("heightfield_asset_digest requires heightfield_asset_uri")
    if terrain.model is TerrainModel.FLAT and terrain.maximum_relief_m != 0.0:
        raise ValueError("flat terrain must have zero maximum relief")
    if spec.imaging.sensor_artifacts.output_pixel_format is not PixelFormat.RGB8:
        raise ValueError("the current first-party imager requires RGB8")


def verify_external_asset(path: Path, expected_digest: str) -> None:
    read_verified_asset(path, expected_digest)


def verify_scenario_assets(spec: ScenarioSpec, asset_root: Path) -> None:
    """Verify every currently supported external asset reference before a run."""
    landscape = spec.scene_definition.osm_landscape
    if landscape is not None:
        from chongming.scenarios.osm import load_landscape

        if load_landscape(landscape.asset_uri, landscape.asset_sha256, asset_root) != landscape:
            raise ValueError("compiled OSM metadata disagrees with the verified asset")
    terrain = spec.scene_definition.terrain
    if terrain.heightfield_asset_uri is None:
        return
    asset = resolve_external_asset(terrain.heightfield_asset_uri, asset_root)
    if terrain.heightfield_asset_digest is None:
        raise ValueError("heightfield asset is missing a digest")
    verify_external_asset(asset, terrain.heightfield_asset_digest)


def validate_supported_features(spec: ScenarioSpec) -> None:
    """Reject declared but unimplemented effects instead of silently ignoring them."""
    p = spec.perturbations
    a = spec.imaging.sensor_artifacts
    if spec.scene_definition.terrain.model is TerrainModel.HEIGHTFIELD:
        terrain = spec.scene_definition.terrain
        if terrain.heightfield_geometry is None or terrain.heightfield_origin_enu_m is None:
            raise ValueError("heightfield terrain requires compiled geometry and origin")
    elif spec.scene_definition.terrain.heightfield_asset_uri is not None:
        raise ValueError("flat terrain cannot use a heightfield asset")
    if any(
        c.actual_distortion.model.value != "none"
        or any(c.actual_distortion.radial_k1_k2_k3)
        or any(c.actual_distortion.tangential_p1_p2)
        for c in spec.scene_definition.cameras
    ):
        raise ValueError("Godot renderer supports distortion-free cameras only")
    if any(
        abs(c.actual_intrinsics.skew_px) > 1e-9
        or abs(c.actual_intrinsics.focal_x_px - c.actual_intrinsics.focal_y_px) > 1e-9
        for c in spec.scene_definition.cameras
    ):
        raise ValueError("Godot cameras require zero skew and square pixels")
    if p.fog or p.rain or p.communication:
        raise ValueError("configured fog/rain/transport effects are not implemented")
    if (
        a.auto_exposure_enabled
        or a.dynamic_range_stops is not None
        or a.saturation_level is not None
        or a.glare_enabled
        or a.bloom_strength is not None
    ):
        raise ValueError("configured advanced sensor effects are not implemented")
    if any(
        c.exposure.shutter_model.value != "global" or c.exposure.rolling_readout_ns not in (None, 0)
        for c in spec.scene_definition.cameras
    ):
        raise ValueError("Godot scene imaging currently supports global shutter only")
    if any(any(v != 0 for v in item.drift_rotation_camera_radps) for item in p.camera_pose):
        raise ValueError("camera pose drift is not implemented")


def runtime_metadata() -> dict[str, str]:
    return {"python_version": sys.version.split()[0], "serializer_format": FORMAT_VERSION}
