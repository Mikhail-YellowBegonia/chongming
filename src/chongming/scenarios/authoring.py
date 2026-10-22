"""Compact, semantic scenario authoring format.

The authoring document is intentionally smaller than the runtime ScenarioSpec.
It describes scene intent (stations, targets and motion), while the compiler
derives optical matrices, reference landmarks and runtime-only configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.contracts.scene import BehaviorId
from chongming.scenarios.assets import read_verified_asset, resolve_external_asset
from chongming.scenarios.demo import (
    calibration_panel,
    demo_spec,
    hold,
    optical_rotation,
    orbit_path,
    state,
)

AUTHORING_FORMAT = "scenario-authoring-v2"
STANDARD_MAIN_RESOLUTION = (1920, 1080)
STANDARD_AUXILIARY_RESOLUTION = (640, 480)
STANDARD_MAIN_FOV_DEG = 60.0
STANDARD_AUXILIARY_FOV_DEG = 2.6  # horizontal, main is diagonal


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain three numbers")
    return tuple(_number(item, name) for item in value)  # type: ignore[return-value]


def _required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f"authoring field {key!r} is required")
    return mapping[key]


def _fields(value: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} fields: {sorted(unknown)}")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _geometry(value: dict[str, Any]) -> s.GeometrySpec:
    kind = _required(value, "type")
    _fields(
        value,
        {"type"}
        | {"sphere": {"radius_m"}, "box": {"half_extents_m"}, "aircraft": set()}.get(kind, set()),
        "geometry",
    )
    if kind == "sphere":
        return s.SphereGeometry(_number(_required(value, "radius_m"), "radius_m"))
    if kind == "box":
        extents = _required(value, "half_extents_m")
        if not isinstance(extents, list) or len(extents) != 3:
            raise ValueError("half_extents_m must contain three numbers")
        return s.BoxGeometry(tuple(_number(x, "half_extents_m") for x in extents))  # type: ignore[arg-type]
    if kind == "aircraft":
        return s.TriangleMeshGeometry(
            (
                (0.0, 2.8, 0.0),
                (-0.35, -1.7, -0.25),
                (0.35, -1.7, -0.25),
                (0.0, -1.7, 0.4),
                (-3.0, -0.6, 0.0),
                (3.0, -0.6, 0.0),
                (-1.2, -1.8, 0.0),
                (1.2, -1.8, 0.0),
            ),
            (
                (0, 1, 2),
                (0, 3, 1),
                (0, 2, 3),
                (1, 3, 2),
                (0, 4, 1),
                (0, 2, 5),
                (1, 6, 3),
                (2, 3, 7),
            ),
        )
    raise ValueError(f"unsupported authoring geometry type: {kind!r}")


def _terrain(value: Any, base: s.TerrainSpec, asset_root: Path | None) -> s.TerrainSpec:
    if value is None:
        return base
    if not isinstance(value, dict):
        raise TypeError("terrain must be an object")
    kind = value.get("type", "flat")
    _fields(
        value,
        {"type"} if kind == "flat" else {"type", "origin_enu_m", "spacing_m", "heights_m", "asset"},
        "terrain",
    )
    if kind == "flat":
        return base
    if kind != "heightfield":
        raise ValueError(f"unsupported terrain type: {kind!r}")
    origin = value.get("origin_enu_m", [0.0, 0.0])
    spacing = _number(value.get("spacing_m", 10.0), "terrain.spacing_m")
    if spacing <= 0:
        raise ValueError("terrain.spacing_m must be positive")
    if ("heights_m" in value) == ("asset" in value):
        raise ValueError("heightfield requires exactly one of heights_m or asset")
    asset_uri, asset_digest = None, None
    if "asset" in value:
        asset = _fields(value["asset"], {"uri", "sha256"}, "terrain.asset")
        asset_uri = _identifier(_required(asset, "uri"), "terrain.asset.uri")
        asset_digest = _identifier(_required(asset, "sha256"), "terrain.asset.sha256")
        if asset_root is None:
            raise ValueError("compiling external terrain requires asset_root")
        content = read_verified_asset(resolve_external_asset(asset_uri, asset_root), asset_digest)
        try:
            grid = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(f"invalid heightfield asset JSON: {error}") from error
        grid = _fields(grid, {"heights_m"}, "heightfield asset")
        heights = _required(grid, "heights_m")
    else:
        heights = value["heights_m"]
    if not isinstance(origin, list) or len(origin) != 2 or not isinstance(heights, list):
        raise ValueError("heightfield requires origin_enu_m and heights_m")
    if any(not isinstance(row, list) for row in heights):
        raise ValueError("heightfield rows must be arrays")
    rows = heights
    if len(rows) < 2 or len(rows[0]) < 2 or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("heightfield requires a rectangular grid of at least 2x2")
    east, north = (_number(item, "terrain.origin_enu_m") for item in origin)
    # Mesh coordinates are local; visibility and rendering apply the terrain pose.
    vertices = tuple(
        (col * spacing, row * spacing, _number(height, "terrain.height"))
        for row, values in enumerate(rows)
        for col, height in enumerate(values)
    )
    if any(vertex[2] < 0 for vertex in vertices):
        raise ValueError("heightfield elevations cannot be below base elevation")
    columns = len(rows[0])
    triangles: list[tuple[int, int, int]] = []
    for row in range(len(rows) - 1):
        for col in range(columns - 1):
            index = row * columns + col
            triangles.extend(
                (
                    (index, index + 1, index + columns + 1),
                    (index, index + columns + 1, index + columns),
                )
            )
    geometry = s.TriangleMeshGeometry(vertices, tuple(triangles))
    return replace(
        base,
        model=s.TerrainModel.HEIGHTFIELD,
        maximum_relief_m=max(vertex[2] for vertex in vertices),
        heightfield_geometry=geometry,
        heightfield_origin_enu_m=(east, north),
        heightfield_asset_uri=asset_uri,
        heightfield_asset_digest=asset_digest,
    )


def _appearance(value: dict[str, Any] | None) -> s.SurfaceAppearance:
    if value is None:
        return s.SurfaceAppearance((0.16, 0.18, 0.21), 0.9)
    _fields(value, {"linear_rgb", "diffuse_reflectance"}, "appearance")
    rgb = _required(value, "linear_rgb")
    if not isinstance(rgb, list) or len(rgb) != 3:
        raise ValueError("linear_rgb must contain three numbers")
    return s.SurfaceAppearance(
        tuple(_number(x, "linear_rgb") for x in rgb),  # type: ignore[arg-type]
        _number(value.get("diffuse_reflectance", 0.9), "diffuse_reflectance"),
    )


def _target(spec: s.FlyingObjectSpec, document: dict[str, Any]) -> s.FlyingObjectSpec:
    _fields(document, {"id", "display_name", "model_name", "geometry", "appearance", "motion"}, "target")
    target_id = _identifier(_required(document, "id"), "target.id")
    geometry_value = document.get("geometry")
    geometry = spec.geometry if geometry_value is None else _geometry(geometry_value)
    appearance = _appearance(document.get("appearance"))
    model_name = str(document.get("model_name", spec.model_name))
    if model_name not in {"procedural", "sphere", "box", "mesh", "la11", "la11triangled", "f86", "f86triangled"}:
        raise ValueError(f"unsupported target model_name: {model_name!r}")
    motion = _required(document, "motion")
    if not isinstance(motion, dict):
        raise TypeError("motion must be an object")
    motion_type = _required(motion, "type")
    _fields(
        motion,
        {"type"}
        | {
            "orbit": {"center_enu_m", "radius_m", "period_s", "phase_rad", "vertical_amplitude_m"},
            "constant_velocity": {"position_enu_m", "velocity_enu_mps"},
        }.get(motion_type, set()),
        "motion",
    )
    behavior: s.FlightBehaviorSpec | None = None
    if motion_type == "orbit":
        trajectory = orbit_path(
            _vec3(_required(motion, "center_enu_m"), "center_enu_m"),
            _number(_required(motion, "radius_m"), "radius_m"),
            _number(_required(motion, "period_s"), "period_s"),
            _number(motion.get("phase_rad", 0.0), "phase_rad"),
            _number(motion.get("vertical_amplitude_m", 2.0), "vertical_amplitude_m"),
        )
        center = _vec3(motion["center_enu_m"], "center_enu_m")
        radius = _number(motion["radius_m"], "radius_m")
        behavior = s.FlightBehaviorSpec(
            BehaviorId("orbit"),
            "1",
            (
                ("center_e", center[0]),
                ("center_n", center[1]),
                ("center_u", center[2]),
                ("radius_e", radius),
                ("radius_n", radius),
                ("period_s", _number(motion["period_s"], "period_s")),
                ("phase_rad", _number(motion.get("phase_rad", 0), "phase_rad")),
                (
                    "vertical_amplitude_m",
                    _number(motion.get("vertical_amplitude_m", 2), "vertical_amplitude_m"),
                ),
            ),
            f"flight/{target_id}",
        )
        trajectory = replace(
            trajectory, interpolation=s.TrajectoryInterpolation.HOLD, knots=trajectory.knots[:1]
        )
    elif motion_type == "constant_velocity":
        position = _vec3(_required(motion, "position_enu_m"), "position_enu_m")
        velocity = _vec3(_required(motion, "velocity_enu_mps"), "velocity_enu_mps")
        trajectory = s.TrajectorySpec(
            s.TrajectoryInterpolation.HOLD,
            (
                s.TrajectoryKnot(
                    0,
                    s.KinematicState(
                        position,
                        velocity,
                        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                        (0.0, 0.0, 0.0),
                    ),
                ),
            ),
        )
        behavior = s.FlightBehaviorSpec(
            behavior_id=BehaviorId("constant-velocity"),
            implementation_version="1",
            parameters=(("vx", velocity[0]), ("vy", velocity[1]), ("vz", velocity[2])),
            random_stream=f"flight/{target_id}",
        )
    else:
        raise ValueError(f"unsupported motion type: {motion_type!r}")
    return replace(
        spec,
        truth_object_id=s.TruthObjectId(target_id),
        display_name=str(document.get("display_name", target_id)),
        model_name=model_name,
        trajectory=trajectory,
        behavior=behavior,
        geometry=geometry,
        appearance=appearance,
    )


def compile_authoring(
    document: dict[str, Any], *, asset_root: Path | None = None
) -> s.ScenarioSpec:
    """Compile a semantic authoring document into the strict runtime model."""
    if set(document) - {
        "format",
        "scenario_id",
        "random_seed",
        "duration_s",
        "fps",
        "stations",
        "targets",
        "vegetation",
        "wind",
        "terrain",
        "world_objects",
        "search",
        "environment",
        "camera_jitter",
        "clouds",
        "rendering",
        "osm",
    }:
        unknown = set(document) - {
            "format",
            "scenario_id",
            "random_seed",
            "duration_s",
            "fps",
            "stations",
            "targets",
            "vegetation",
            "wind",
            "terrain",
            "world_objects",
            "search",
            "environment",
            "camera_jitter",
            "clouds",
            "rendering",
            "osm",
        }
        raise ValueError(f"unknown authoring fields: {sorted(unknown)}")
    if document.get("format") != AUTHORING_FORMAT:
        raise ValueError(f"unsupported authoring format: {document.get('format')!r}")
    stations = document.get("stations", [{"id": "station-01", "position_enu_m": [-10, -22, 4]}])
    if not isinstance(stations, list) or not 1 <= len(stations) <= 4:
        raise ValueError("stations must contain between one and four entries")
    positions: list[tuple[float, float, float]] = []
    ids: list[str] = []
    for station in stations:
        if not isinstance(station, dict):
            raise TypeError("station entries must be objects")
        _fields(
            station,
            {
                "id",
                "position_enu_m",
                "main_yaw_deg",
                "main_elevation_deg",
                "auxiliary_yaw_deg",
                "fps",
                "start_s",
            },
            "station",
        )
        ids.append(_identifier(_required(station, "id"), "station.id"))
        positions.append(_vec3(_required(station, "position_enu_m"), "position_enu_m"))
    if len(set(ids)) != len(ids):
        raise ValueError("station IDs must be unique")
    fps = _integer(document.get("fps", 30), "fps")
    if fps <= 0:
        raise ValueError("fps must be positive")
    spec = demo_spec(
        station_count=len(stations),
        fps=fps,
        station_positions=tuple(positions),
        station_ids=tuple(ids),
    )
    environment_doc = document.get("environment")
    illumination = spec.scene_definition.illumination
    if environment_doc is not None:
        environment = _fields(
            environment_doc,
            {
                "sun_direction_enu_unit",
                "illuminance_lux",
                "sky_linear_rgb",
                "atmospheric_visibility_m",
                "sun_angular_diameter_deg",
                "glare_strength",
            },
            "environment",
        )
        sun_direction = _vec3(
            environment.get("sun_direction_enu_unit", list(illumination.sun_direction_enu_unit)),
            "environment.sun_direction_enu_unit",
        )
        sun_length = math.sqrt(sum(value * value for value in sun_direction))
        if sun_length <= 0:
            raise ValueError("environment sun direction must be nonzero")
        sky_value = environment.get("sky_linear_rgb", list(illumination.sky_linear_rgb))
        sky = _vec3(sky_value, "environment.sky_linear_rgb")
        visibility_value = environment.get("atmospheric_visibility_m")
        visibility = (
            illumination.atmospheric_visibility_m
            if visibility_value is None
            else _number(visibility_value, "environment.atmospheric_visibility_m")
        )
        sun_diameter = _number(
            environment.get("sun_angular_diameter_deg", illumination.sun_angular_diameter_deg),
            "environment.sun_angular_diameter_deg",
        )
        glare = _number(
            environment.get("glare_strength", illumination.glare_strength),
            "environment.glare_strength",
        )
        illuminance = _number(
            environment.get("illuminance_lux", illumination.illuminance_lux),
            "environment.illuminance_lux",
        )
        if (visibility is not None and visibility <= 0) or not 0 <= sun_diameter <= 5:
            raise ValueError("environment visibility must be positive and sun diameter within 0..5 degrees")
        if illuminance < 0 or not 0 <= glare <= 2 or any(not 0 <= value <= 1 for value in sky):
            raise ValueError("environment illuminance, glare or sky color is invalid")
        illumination = s.IlluminationSpec(
            tuple(value / sun_length for value in sun_direction),  # type: ignore[arg-type]
            illuminance,
            sky,
            visibility,
            sun_diameter,
            glare,
        )
    render_effects = spec.scene_definition.render_effects
    rendering_doc = document.get("rendering")
    if rendering_doc is not None:
        rendering = _fields(
            rendering_doc,
            {
                "terrain_detail_strength",
                "ssao_intensity",
                "fxaa_enabled",
                "god_rays_strength",
                "vegetation_lod_near_m",
                "vegetation_lod_far_m",
            },
            "rendering",
        )
        fxaa = rendering.get("fxaa_enabled", render_effects.fxaa_enabled)
        if not isinstance(fxaa, bool):
            raise ValueError("rendering.fxaa_enabled must be a boolean")
        render_effects = s.RenderEffectsSpec(
            _number(
                rendering.get("terrain_detail_strength", render_effects.terrain_detail_strength),
                "rendering.terrain_detail_strength",
            ),
            _number(
                rendering.get("ssao_intensity", render_effects.ssao_intensity),
                "rendering.ssao_intensity",
            ),
            fxaa,
            _number(
                rendering.get("god_rays_strength", render_effects.god_rays_strength),
                "rendering.god_rays_strength",
            ),
            _number(
                rendering.get("vegetation_lod_near_m", render_effects.vegetation_lod_near_m),
                "rendering.vegetation_lod_near_m",
            ),
            _number(
                rendering.get("vegetation_lod_far_m", render_effects.vegetation_lod_far_m),
                "rendering.vegetation_lod_far_m",
            ),
        )
        if (
            not 0 <= render_effects.terrain_detail_strength <= 1
            or not 0 <= render_effects.ssao_intensity <= 4
            or not 0 <= render_effects.god_rays_strength <= 2
            or render_effects.vegetation_lod_near_m <= 0
            or render_effects.vegetation_lod_far_m <= render_effects.vegetation_lod_near_m
        ):
            raise ValueError("rendering effects or vegetation LOD distances are invalid")
    duration_s = _number(document.get("duration_s", 24.0), "duration_s")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    cameras = []
    configs = []
    panels: list[s.WorldObjectSpec] = []
    landmarks: list[s.ReferenceLandmarkSpec] = []
    for station, config in zip(stations, spec.station_configs, strict=True):
        rate = _integer(station.get("fps", fps), "station.fps")
        if not 1 <= rate <= 60:
            raise ValueError("station.fps must be between 1 and 60")
        start_s = _number(station.get("start_s", 0), "station.start_s")
        if start_s < 0:
            raise ValueError("station.start_s cannot be negative")
        rotations = {}
        for camera in spec.scene_definition.cameras:
            if camera.station_id != config.station_id:
                continue
            pose = camera.optical_pose_trajectory.knots[0].state
            if camera.camera_role is m.CameraRole.MAIN:
                yaw = math.degrees(
                    math.atan2(pose.rotation_enu_from_body[0][2], pose.rotation_enu_from_body[1][2])
                )
                rotation = optical_rotation(
                    math.radians(_number(station.get("main_yaw_deg", yaw), "main_yaw_deg")),
                    math.radians(
                        _number(station.get("main_elevation_deg", 9), "main_elevation_deg")
                    ),
                )
            else:
                yaw = math.degrees(
                    math.atan2(pose.rotation_enu_from_body[0][2], pose.rotation_enu_from_body[1][2])
                )
                rotation = optical_rotation(
                    math.radians(
                        _number(station.get("auxiliary_yaw_deg", yaw), "auxiliary_yaw_deg")
                    ),
                    0,
                )
            rotations[camera.camera_role] = rotation
            cameras.append(
                replace(
                    camera,
                    optical_pose_trajectory=hold(replace(pose, rotation_enu_from_body=rotation)),
                    metadata=replace(camera.metadata, nominal_frame_rate_hz=float(rate)),
                    exposure=replace(
                        camera.exposure,
                        frame_period_ns=1_000_000_000 // rate,
                        first_exposure_start_ns=round(start_s * 1e9),
                    ),
                )
            )
        main, aux = rotations[m.CameraRole.MAIN], rotations[m.CameraRole.AUXILIARY]
        panel_objects, panel_landmarks = calibration_panel(
            config.station_id,
            next(
                c
                for c in cameras
                if c.station_id == config.station_id and c.camera_role is m.CameraRole.AUXILIARY
            )
            .optical_pose_trajectory.knots[0]
            .state,
        )
        panels.extend(panel_objects)
        landmarks.extend(panel_landmarks)
        relative = tuple(
            tuple(sum(aux[k][i] * main[k][j] for k in range(3)) for j in range(3)) for i in range(3)
        )
        assert isinstance(config.reference_anchor, m.SurveyedReference)
        compiled_config = replace(
            config,
            reference_anchor=replace(
                config.reference_anchor,
                landmarks=tuple(
                    replace(
                        old, landmark_id=str(new.landmark_id), position_enu_m=new.position_enu_m
                    )
                    for old, new in zip(
                        config.reference_anchor.landmarks, panel_landmarks, strict=True
                    )
                ),
            ),
            baseline_orientation_enu_from_main=main,
            fixed_rotation_aux_from_main=relative,  # type: ignore[arg-type]
            full_rigid_transform_aux_from_main=m.RigidTransform(
                rotation_a_from_b=relative,  # type: ignore[arg-type]
                translation_a_m=(-0.2 * aux[0][0], -0.2 * aux[0][1], -0.2 * aux[0][2]),
            ),
        )
        digest = m.ConfigDigest(
            hashlib.sha256(
                json.dumps(asdict(compiled_config), sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        )
        configs.append(
            replace(
                compiled_config,
                config_digest=digest,
                installation_uncertainty=replace(
                    compiled_config.installation_uncertainty,
                    state_ref=replace(
                        compiled_config.installation_uncertainty.state_ref, config_digest=digest
                    ),
                ),
            )
        )
    spec = replace(
        spec,
        station_configs=tuple(configs),
        scene_definition=replace(
            spec.scene_definition,
            cameras=tuple(cameras),
            world_objects=tuple(
                o
                for o in spec.scene_definition.world_objects
                if o.kind is not s.WorldObjectKind.FIXED_REFERENCE
            )
            + tuple(panels),
            reference_landmarks=tuple(landmarks),
        ),
    )
    if "world_objects" in document:
        objects = document["world_objects"]
        if not isinstance(objects, list):
            raise ValueError("world_objects must be an array")
        compiled_objects = []
        for item in objects:
            item = _fields(
                item,
                {
                    "id",
                    "display_name",
                    "position_enu_m",
                    "geometry",
                    "appearance",
                    "occludes_sight_lines",
                },
                "world_object",
            )
            object_id = _identifier(_required(item, "id"), "world_object.id")
            occludes = item.get("occludes_sight_lines", True)
            if type(occludes) is not bool:
                raise ValueError("occludes_sight_lines must be boolean")
            compiled_objects.append(
                s.WorldObjectSpec(
                    s.WorldObjectId(object_id),
                    str(item.get("display_name", object_id)),
                    s.WorldObjectKind.OBSTACLE,
                    "authored",
                    state(_vec3(_required(item, "position_enu_m"), "position_enu_m")),
                    _geometry(_required(item, "geometry")),
                    _appearance(item.get("appearance")),
                    occludes,
                )
            )
        spec = replace(
            spec,
            scene_definition=replace(
                spec.scene_definition, world_objects=tuple(compiled_objects) + tuple(panels)
            ),
        )
    if "search" in document:
        search = _fields(document["search"], {"min_enu_m", "max_enu_m", "step_enu_m"}, "search")
        spec = replace(
            spec,
            voxel_search=replace(
                spec.voxel_search,
                min_enu_m=_vec3(_required(search, "min_enu_m"), "search.min_enu_m"),
                max_enu_m=_vec3(_required(search, "max_enu_m"), "search.max_enu_m"),
                step_enu_m=_vec3(_required(search, "step_enu_m"), "search.step_enu_m"),
            ),
        )
    targets = document.get("targets")
    if targets is not None:
        if not isinstance(targets, list):
            raise ValueError("targets must be an array")
        by_id = {str(item.truth_object_id): item for item in spec.scene_definition.flying_objects}
        compiled: list[s.FlyingObjectSpec] = []
        template = next(iter(by_id.values()))
        for item in targets:
            if not isinstance(item, dict):
                raise TypeError("target entries must be objects")
            target_id = str(_required(item, "id"))
            compiled.append(_target(by_id.get(target_id, template), item))
        spec = replace(
            spec,
            scene_definition=replace(spec.scene_definition, flying_objects=tuple(compiled)),
        )
    vegetation_doc = document.get("vegetation")
    use_demo_vegetation = vegetation_doc is None
    if use_demo_vegetation:
        vegetation_doc = []
    if not isinstance(vegetation_doc, list):
        raise TypeError("vegetation must be an array")
    vegetation: list[s.VegetationSpec] = (
        list(spec.scene_definition.vegetation) if use_demo_vegetation else []
    )
    vegetation_perturbations: list[s.VegetationPerturbation] = (
        list(spec.perturbations.vegetation) if use_demo_vegetation else []
    )
    vegetation_ids: set[str] = set()
    for item in vegetation_doc:
        if not isinstance(item, dict):
            raise TypeError("vegetation entries must be objects")
        kind = item.get("type", "tree")
        common = {"id", "type", "display_name", "position_enu_m", "appearance",
                  "occludes_sight_lines"}
        allowed = {
            "tree": common | {"height_m", "crown_radius_m", "sway_amplitude_m",
                              "sway_frequency_hz"},
            "shrub": common | {"height_m", "radius_m"},
            "grass": common | {"size_m", "blade_height_m", "density_per_m2"},
        }
        if kind not in allowed:
            raise ValueError(f"unsupported vegetation type: {kind!r}")
        _fields(item, allowed[kind], "vegetation")
        plant_id = _identifier(_required(item, "id"), "vegetation.id")
        if plant_id in vegetation_ids:
            raise ValueError(f"vegetation IDs must be unique: {plant_id}")
        vegetation_ids.add(plant_id)
        position = _vec3(_required(item, "position_enu_m"), "vegetation.position_enu_m")
        occludes = item.get("occludes_sight_lines", kind != "grass")
        if not isinstance(occludes, bool):
            raise TypeError("vegetation.occludes_sight_lines must be a boolean")
        display_name = str(item.get("display_name", plant_id))
        pose = state(position)
        if kind == "tree":
            height = _number(_required(item, "height_m"), "vegetation.height_m")
            radius = _number(_required(item, "crown_radius_m"), "vegetation.crown_radius_m")
            amplitude = _number(
                item.get("sway_amplitude_m", 0.25), "vegetation.sway_amplitude_m"
            )
            frequency = _number(
                item.get("sway_frequency_hz", 0.22), "vegetation.sway_frequency_hz"
            )
            if height <= 0 or radius <= 0 or amplitude < 0 or frequency <= 0:
                raise ValueError(
                    "tree dimensions/frequency must be positive and sway amplitude nonnegative"
                )
            trunk_proxy = s.CylinderGeometry(max(0.12, radius * 0.12), height * 0.58)
            canopy_proxy = s.CylinderGeometry(radius, height * 0.50)
            trunk_id = s.VegetationId(f"{plant_id}/trunk")
            canopy_id = s.VegetationId(f"{plant_id}/canopy")
            canopy_pose = state((position[0], position[1], position[2] + height * 0.42))
            vegetation.extend((
                s.VegetationSpec(
                    trunk_id, f"{display_name} 树干", pose, trunk_proxy,
                    s.SurfaceAppearance((0.20, 0.105, 0.045), 0.9),
                    s.VegetationMotionModel.STATIC, occludes,
                    s.TreeVisualSpec(s.TreePart.TRUNK, trunk_proxy.height_m, trunk_proxy.radius_m),
                ),
                s.VegetationSpec(
                    canopy_id, f"{display_name} 树冠", canopy_pose, canopy_proxy,
                    _appearance(item.get("appearance")) if "appearance" in item else
                    s.SurfaceAppearance((0.12, 0.31, 0.09), 0.9),
                    s.VegetationMotionModel.WIND_DRIVEN, occludes,
                    s.TreeVisualSpec(s.TreePart.CANOPY, canopy_proxy.height_m, canopy_proxy.radius_m),
                ),
            ))
            vegetation_perturbations.append(
                s.VegetationPerturbation(
                    s.WorldObjectId(str(canopy_id)), (amplitude, 0.0, 0.0), frequency
                )
            )
        elif kind == "shrub":
            height = _number(_required(item, "height_m"), "vegetation.height_m")
            radius = _number(_required(item, "radius_m"), "vegetation.radius_m")
            if height <= 0 or radius <= 0:
                raise ValueError("shrub dimensions must be positive")
            geometry = s.CylinderGeometry(radius, height)
            vegetation.append(s.VegetationSpec(
                s.VegetationId(f"{plant_id}/shrub"), display_name, pose, geometry,
                _appearance(item.get("appearance")) if "appearance" in item else
                s.SurfaceAppearance((0.10, 0.27, 0.07), 0.85),
                s.VegetationMotionModel.STATIC, occludes,
                s.ShrubVisualSpec(height, radius),
            ))
        else:
            size = _required(item, "size_m")
            if not isinstance(size, list) or len(size) != 2:
                raise ValueError("vegetation.size_m must contain width and depth")
            width, depth = (_number(value, "vegetation.size_m") for value in size)
            blade_height = _number(
                item.get("blade_height_m", 0.45), "vegetation.blade_height_m"
            )
            density = _number(
                item.get("density_per_m2", 16.0), "vegetation.density_per_m2"
            )
            if min(width, depth, blade_height, density) <= 0 or density > 200:
                raise ValueError("grass dimensions/density must be positive and density at most 200")
            radius = math.hypot(width, depth) * 0.5
            vegetation.append(s.VegetationSpec(
                s.VegetationId(f"{plant_id}/grass"), display_name, pose,
                s.CylinderGeometry(radius, blade_height),
                _appearance(item.get("appearance")) if "appearance" in item else
                s.SurfaceAppearance((0.16, 0.34, 0.08), 0.75),
                s.VegetationMotionModel.STATIC, occludes,
                s.GrassVisualSpec(width, depth, blade_height, density),
            ))
    wind_doc = document.get("wind")
    wind = spec.perturbations.wind if "wind" not in document else None
    if wind_doc is not None:
        _fields(wind_doc, {"mean_velocity_enu_mps", "turbulent_velocity_std_enu_mps"}, "wind")
        wind = s.WindPerturbation(
            _vec3(wind_doc.get("mean_velocity_enu_mps", [0, 0, 0]), "wind.mean_velocity_enu_mps"),
            _vec3(wind_doc.get("turbulent_velocity_std_enu_mps", [0, 0, 0]),
                  "wind.turbulent_velocity_std_enu_mps"),
        )
        if min(wind.turbulent_velocity_std_enu_mps) < 0:
            raise ValueError("wind turbulence standard deviations must be nonnegative")
    clouds_doc = document.get("clouds")
    clouds = spec.perturbations.clouds if "clouds" not in document else ()
    if clouds_doc is not None:
        if not isinstance(clouds_doc, list) or len(clouds_doc) > 2:
            raise ValueError("clouds must contain at most two layers")
        compiled_clouds = []
        for index, item in enumerate(clouds_doc):
            cloud = _fields(
                item,
                {"layer_altitude_m", "optical_density", "drift_velocity_enu_mps"},
                f"clouds[{index}]",
            )
            layer = s.CloudPerturbation(
                _number(_required(cloud, "layer_altitude_m"), "cloud.layer_altitude_m"),
                _number(_required(cloud, "optical_density"), "cloud.optical_density"),
                _vec3(
                    cloud.get("drift_velocity_enu_mps", [0, 0, 0]),
                    "cloud.drift_velocity_enu_mps",
                ),
            )
            if layer.layer_altitude_m <= 0 or not 0 <= layer.optical_density <= 1:
                raise ValueError("cloud altitude must be positive and density within 0..1")
            compiled_clouds.append(layer)
        clouds = tuple(compiled_clouds)
    jitter_doc = document.get("camera_jitter")
    camera_jitter = spec.perturbations.camera_jitter if "camera_jitter" not in document else ()
    if jitter_doc is not None:
        jitter = _fields(
            jitter_doc,
            {"angular_amplitude_deg", "pixel_amplitude_px", "frequency_hz", "roles"},
            "camera_jitter",
        )
        angular_deg = _vec3(
            jitter.get("angular_amplitude_deg", [0, 0, 0]),
            "camera_jitter.angular_amplitude_deg",
        )
        pixel_value = jitter.get("pixel_amplitude_px", [0, 0])
        if not isinstance(pixel_value, list) or len(pixel_value) != 2:
            raise ValueError("camera_jitter.pixel_amplitude_px must contain two numbers")
        pixels = tuple(
            _number(value, "camera_jitter.pixel_amplitude_px") for value in pixel_value
        )
        frequency = _number(jitter.get("frequency_hz", 7.0), "camera_jitter.frequency_hz")
        role_values = jitter.get("roles", ["main", "auxiliary"])
        if not isinstance(role_values, list) or not role_values:
            raise ValueError("camera_jitter.roles must be a nonempty array")
        try:
            roles = tuple(m.CameraRole(value) for value in role_values)
        except (TypeError, ValueError) as error:
            raise ValueError("camera_jitter.roles contains an unknown camera role") from error
        if len(set(roles)) != len(roles):
            raise ValueError("camera_jitter.roles must be unique")
        if frequency <= 0 or any(value < 0 for value in (*angular_deg, *pixels)):
            raise ValueError("camera jitter amplitudes must be nonnegative and frequency positive")
        camera_jitter = tuple(
            s.CameraJitterPerturbation(
                camera.station_id,
                camera.camera_role,
                tuple(math.radians(value) for value in angular_deg),  # type: ignore[arg-type]
                pixels,  # type: ignore[arg-type]
                frequency,
                f"camera-jitter/{camera.station_id}/{camera.camera_role.value}",
            )
            for camera in spec.scene_definition.cameras
            if camera.camera_role in roles
        )
    spec = replace(
        spec,
        scenario_id=_identifier(document.get("scenario_id", "scenario"), "scenario_id"),
        config_digest=m.ConfigDigest(
            hashlib.sha256(
                json.dumps(document, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        ),
        random_seed=_integer(document.get("random_seed", s.SCENE_RANDOM_SEED), "random_seed"),
        perturbations=replace(
            spec.perturbations,
            camera_jitter=camera_jitter,
            clouds=clouds,
            wind=wind,
            vegetation=tuple(vegetation_perturbations),
        ),
        timeline=replace(spec.timeline, end_time_ns=round(duration_s * 1e9) - 1),
        scene_definition=replace(
            spec.scene_definition,
            terrain=_terrain(document.get("terrain"), spec.scene_definition.terrain, asset_root),
            illumination=illumination,
            vegetation=tuple(vegetation),
            render_effects=render_effects,
        ),
    )
    spec = replace(
        spec,
        virtual_observations=replace(
            spec.virtual_observations,
            calibration_intervals=tuple(
                replace(interval, end_time_ns=spec.timeline.end_time_ns + 1)
                for interval in spec.virtual_observations.calibration_intervals
            ),
        ),
    )
    if "osm" in document:
        from chongming.scenarios.osm import attach_landscape, load_landscape

        osm = _fields(document["osm"], {"asset"}, "osm")
        asset = _fields(_required(osm, "asset"), {"uri", "sha256"}, "osm.asset")
        if asset_root is None:
            raise ValueError("compiling OSM requires asset_root")
        spec = attach_landscape(
            spec,
            load_landscape(
                _identifier(_required(asset, "uri"), "osm.asset.uri"),
                _identifier(_required(asset, "sha256"), "osm.asset.sha256"),
                asset_root,
            ),
        )

    from chongming.scenarios.serialization import validate_scenario_spec

    validate_scenario_spec(spec)
    return spec


def load_authoring(path: Path, *, asset_root: Path | None = None) -> s.ScenarioSpec:
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid authoring JSON: {error}") from error
    if not isinstance(document, dict):
        raise TypeError("authoring JSON root must be an object")
    return compile_authoring(
        document, asset_root=asset_root if asset_root is not None else path.parent
    )


def save_authoring(document: dict[str, Any], path: Path) -> None:
    if document.get("format") != AUTHORING_FORMAT:
        raise ValueError("document is not scenario-authoring-v2")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    # Publishing is a transaction: an editor crash or a failed validation must
    # never leave a half-written authoring document visible to the loader.
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def demo_authoring(*, stations: int = 1, fps: int = 30, duration_s: float = 24.0) -> dict[str, Any]:
    """Return the compact review template used by the exporter."""
    names = ("flight-sphere", "flight-box", "flight-mesh")
    geometries = (
        {"type": "sphere", "radius_m": 1.25},
        {"type": "box", "half_extents_m": [1.5, 0.8, 0.6]},
        {"type": "aircraft"},
    )
    motions = (
        {
            "type": "orbit",
            "center_enu_m": [0, 62, 14],
            "radius_m": 22,
            "period_s": 24,
            "phase_rad": -0.8,
        },
        {
            "type": "orbit",
            "center_enu_m": [0, 62, 22],
            "radius_m": 22,
            "period_s": 24,
            "phase_rad": 1.2,
        },
        {
            "type": "orbit",
            "center_enu_m": [0, 62, 29],
            "radius_m": 22,
            "period_s": 24,
            "phase_rad": 2.7,
        },
    )
    return {
        "format": AUTHORING_FORMAT,
        "scenario_id": "video-demo",
        "random_seed": s.SCENE_RANDOM_SEED,
        "duration_s": duration_s,
        "fps": fps,
        "stations": [
            {"id": f"station-{i + 1:02d}", "position_enu_m": [-10 + 12 * i, -22, 4]}
            for i in range(stations)
        ],
        "targets": [
            {"id": name, "display_name": label, "geometry": geometry, "motion": motion}
            for name, label, geometry, motion in zip(
                names, ("飞行球体", "飞行盒体", "简易飞行器"), geometries, motions, strict=True
            )
        ],
    }
