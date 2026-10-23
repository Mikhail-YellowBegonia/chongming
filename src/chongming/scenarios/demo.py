"""Self-contained, deterministic scene for the first human video review.

The three semantic object classes are world objects, cameras, and flying
objects. Sphere, box and triangle mesh geometry are all exercised as well.
"""

from __future__ import annotations

import math
from uuid import uuid4

from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.modules.device import EventSequencerModule
from chongming.modules.scene import (
    CameraImagingModule,
    CameraProjectionModule,
    ExperimentClockModule,
    ScenarioModule,
    SceneLogicModule,
    TrajectoryModule,
    VirtualObservationModule,
    VisibilityModule,
)

IDENTITY: m.Matrix3x3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
ZERO_COVARIANCE: m.Matrix3x3 = ((0.0, 0.0, 0.0),) * 3
DURATION_NS = 24_000_000_000


def state(
    position: m.Vec3, rotation: m.Matrix3x3 = IDENTITY, velocity: m.Vec3 = (0.0, 0.0, 0.0)
) -> s.KinematicState:
    return s.KinematicState(position, velocity, rotation, (0.0, 0.0, 0.0))


def optical_rotation(yaw_rad: float, elevation_rad: float) -> m.Matrix3x3:
    """Yaw zero looks north; optical axes are right/down/forward in ENU."""
    sy, cy, se, ce = (
        math.sin(yaw_rad),
        math.cos(yaw_rad),
        math.sin(elevation_rad),
        math.cos(elevation_rad),
    )
    return ((cy, sy * se, sy * ce), (-sy, cy * se, cy * ce), (0.0, -ce, se))


def hold(pose: s.KinematicState) -> s.TrajectorySpec:
    return s.TrajectorySpec(s.TrajectoryInterpolation.HOLD, (s.TrajectoryKnot(0, pose),))


def calibration_panel(
    station_id: m.StationId, pose: s.KinematicState
) -> tuple[tuple[s.WorldObjectSpec, ...], tuple[s.ReferenceLandmarkSpec, ...]]:
    """One landscape A4 checkerboard, normal to its level auxiliary camera."""
    r = pose.rotation_enu_from_body
    rotation: m.Matrix3x3 = tuple(  # type: ignore[assignment]
        (r[k][0], r[k][2], -r[k][1]) for k in range(3)
    )
    center = tuple(pose.position_enu_m[k] + 10.0 * r[k][2] for k in range(3))
    white = s.SurfaceAppearance((0.92, 0.92, 0.92), 0.95)
    black = s.SurfaceAppearance((0.015, 0.015, 0.015), 0.95)

    def point(x: float, y: float, z: float) -> m.Vec3:
        return tuple(  # type: ignore[return-value]
            center[k] + rotation[k][0] * x + rotation[k][1] * y + rotation[k][2] * z
            for k in range(3)
        )

    panel_id = s.WorldObjectId(f"{station_id}-a4")
    objects = [
        s.WorldObjectSpec(
            panel_id,
            f"{station_id} A4",
            s.WorldObjectKind.FIXED_REFERENCE,
            "calibration-panel",
            state(point(0, 0, 0), rotation),
            s.BoxGeometry((0.297 / 2, 0.001, 0.210 / 2)),
            white,
            True,
        )
    ]
    for row in range(5):
        for col in range(7):
            if (row + col) % 2 == 0:
                objects.append(
                    s.WorldObjectSpec(
                        s.WorldObjectId(f"{panel_id}-{row}-{col}"),
                        f"{panel_id} mark",
                        s.WorldObjectKind.FIXED_REFERENCE,
                        "calibration-panel",
                        state(point((col - 3) * 0.035, -0.0015, (row - 2) * 0.035), rotation),
                        s.BoxGeometry((0.0175, 0.0002, 0.0175)),
                        black,
                        True,
                    )
                )
    landmarks = tuple(
        s.ReferenceLandmarkSpec(
            s.LandmarkId(f"{panel_id}-corner-{index}"), panel_id, point(x, -0.0011, z), white
        )
        for index, (x, z) in enumerate(
            ((-0.1485, -0.105), (0.1485, -0.105), (0.1485, 0.105), (-0.1485, 0.105))
        )
    )
    return tuple(objects), landmarks


def orbit_path(
    center: m.Vec3,
    radius: float,
    period_s: float,
    phase: float,
    vertical_amplitude: float = 2.0,
) -> s.TrajectorySpec:
    if period_s <= 0 or radius <= 0:
        raise ValueError("orbit period and radius must be positive")
    knots = []
    period_ns = round(period_s * 1e9)
    omega = 2 * math.pi / period_s
    for index in range(17):
        angle = phase + 2 * math.pi * index / 16
        position = (
            center[0] + radius * math.sin(angle),
            center[1] + radius * math.cos(angle),
            center[2] + vertical_amplitude * math.sin(2 * angle),
        )
        velocity = (
            radius * omega * math.cos(angle),
            -radius * omega * math.sin(angle),
            2 * vertical_amplitude * omega * math.cos(2 * angle),
        )
        knots.append(s.TrajectoryKnot(index * period_ns // 16, state(position, velocity=velocity)))
    return s.TrajectorySpec(s.TrajectoryInterpolation.CUBIC_HERMITE, tuple(knots))


def flight_path(phase: float, height: float) -> s.TrajectorySpec:
    return orbit_path((0.0, 62.0, height), 22.0, DURATION_NS / 1e9, phase)


def demo_spec(
    *,
    station_count: int = 1,
    fps: int = 30,
    station_positions: tuple[m.Vec3, ...] | None = None,
    station_ids: tuple[str, ...] | None = None,
) -> s.ScenarioSpec:
    if not 1 <= station_count <= 4:
        raise ValueError("demo station count must be between 1 and 4")
    if not 1 <= fps <= 60:
        raise ValueError("fps must be between 1 and 60")
    if station_positions is not None and len(station_positions) != station_count:
        raise ValueError("station_positions must match station_count")
    if station_ids is not None and len(station_ids) != station_count:
        raise ValueError("station_ids must match station_count")
    world_id = m.WorldFrameId("demo-enu")
    anchor = m.GeodeticAnchor(
        anchor_id="demo-anchor",
        datum="WGS84",
        datum_epoch="2026.0",
        latitude_deg=31.23,
        longitude_deg=121.47,
        height_m=0.0,
        height_reference="ellipsoidal",
    )
    dark = s.SurfaceAppearance((0.16, 0.18, 0.21), 0.9)
    concrete = s.SurfaceAppearance((0.58, 0.57, 0.54), 0.9)
    objects = [
        s.WorldObjectSpec(
            s.WorldObjectId("building-01"),
            "中央建筑",
            s.WorldObjectKind.STATIC_STRUCTURE,
            "building",
            state((0.0, 35.0, 7.0)),
            s.BoxGeometry((4.5, 4.0, 7.0)),
            concrete,
            True,
        ),
        s.WorldObjectSpec(
            s.WorldObjectId("building-02"),
            "侧方建筑",
            s.WorldObjectKind.STATIC_STRUCTURE,
            "building",
            state((-17.0, 48.0, 4.0)),
            s.BoxGeometry((4.0, 5.0, 4.0)),
            dark,
            True,
        ),
        s.WorldObjectSpec(
            s.WorldObjectId("dome"),
            "球形地物",
            s.WorldObjectKind.OBSTACLE,
            "dome",
            state((16.0, 40.0, 2.0)),
            s.SphereGeometry(3.0),
            concrete,
            True,
        ),
    ]
    landmarks: list[s.ReferenceLandmarkSpec] = []
    aircraft = s.TriangleMeshGeometry(
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
        ((0, 1, 2), (0, 3, 1), (0, 2, 3), (1, 3, 2), (0, 4, 1), (0, 2, 5), (1, 6, 3), (2, 3, 7)),
    )
    flying = (
        s.FlyingObjectSpec(
            s.TruthObjectId("flight-sphere"),
            "飞行球体",
            "sphere",
            flight_path(-0.8, 14.0),
            None,
            s.SphereGeometry(1.25),
            dark,
            (0.0, 0.0, 0.0),
        ),
        s.FlyingObjectSpec(
            s.TruthObjectId("flight-box"),
            "飞行盒体",
            "box",
            flight_path(1.2, 22.0),
            None,
            s.BoxGeometry((1.5, 0.8, 0.6)),
            dark,
            (0.0, 0.0, 0.0),
        ),
        s.FlyingObjectSpec(
            s.TruthObjectId("flight-mesh"),
            "简易飞行器",
            "mesh",
            flight_path(2.7, 29.0),
            None,
            aircraft,
            dark,
            (0.0, 0.0, 0.0),
        ),
    )
    cameras, devices, configs = [], [], []
    for index in range(station_count):
        position = (
            station_positions[index]
            if station_positions is not None
            else (-10.0 + 12.0 * index, -22.0, 4.0)
        )
        sid = m.StationId(
            station_ids[index] if station_ids is not None else f"station-{index + 1:02d}"
        )
        main_yaw = math.atan2(-position[0], 84.0)
        main_rotation = optical_rotation(main_yaw, math.radians(9.0))
        aux_yaw = main_yaw
        aux_rotation = optical_rotation(aux_yaw, 0.0)
        station_cameras = []
        for role, rotation, fov in (
            (m.CameraRole.MAIN, main_rotation, 60.0),
            (m.CameraRole.AUXILIARY, aux_rotation, 2.6),
        ):
            width, height = (1920, 1080) if role is m.CameraRole.MAIN else (640, 480)
            focal = (math.hypot(width, height) if role is m.CameraRole.MAIN else width) / (
                2 * math.tan(math.radians(fov / 2))
            )
            diagonal_fov = math.degrees(2 * math.atan(math.hypot(width, height) / (2 * focal)))
            cid = s.CameraId(f"{sid}-{role.value}")
            intrinsics = s.SceneCameraIntrinsics(
                width, height, focal, focal, (width - 1) / 2, (height - 1) / 2, 0.0
            )
            camera = s.SceneCameraSpec(
                sid,
                role,
                s.CameraMetadata(
                    cid,
                    "主相机" if role is m.CameraRole.MAIN else "辅助相机",
                    "standard-v1",
                    "1/4 inch",
                    3.0,
                    diagonal_fov,
                    float(fps),
                ),
                intrinsics,
                s.SceneCameraDistortion(s.SceneDistortionModel.NONE, (0.0, 0.0, 0.0), (0.0, 0.0)),
                hold(
                    state(
                        position
                        if role is m.CameraRole.MAIN
                        else (position[0] + 0.2, position[1], position[2]),
                        rotation,
                    )
                ),
                s.CameraExposureSpec(
                    0, 1_000_000_000 // fps, 2_000_000, s.ShutterModel.GLOBAL, None
                ),
            )
            cameras.append(camera)
            station_cameras.append(
                m.CameraModel(
                    intrinsics=m.CameraIntrinsics(
                        image_width_px=width,
                        image_height_px=height,
                        matrix_px=(
                            (focal, 0.0, intrinsics.principal_x_px),
                            (0.0, focal, intrinsics.principal_y_px),
                            (0.0, 0.0, 1.0),
                        ),
                    ),
                    distortion=m.CameraDistortion(model="none", coefficients=()),
                )
            )
        panel_objects, panel_landmarks = calibration_panel(
            sid, cameras[-1].optical_pose_trajectory.knots[0].state
        )
        objects.extend(panel_objects)
        landmarks.extend(panel_landmarks)
        devices.append(
            s.EdgeDeviceMetadata(
                sid,
                f"摄像站 {index + 1:02d}",
                "virtual-camera-pair",
                cameras[-2].metadata.camera_id,
                cameras[-1].metadata.camera_id,
            )
        )
        digest = m.ConfigDigest(f"demo-config-{index}-standard-{fps}")
        # Auxiliary-from-main is a genuine rigid transform, not a copy of either pose.
        rotation_aux_from_main: m.Matrix3x3 = tuple(  # type: ignore[assignment]
            tuple(sum(aux_rotation[k][i] * main_rotation[k][j] for k in range(3)) for j in range(3))
            for i in range(3)
        )
        configs.append(
            m.StationConfig(
                station_id=sid,
                config_version="demo-v1",
                config_digest=digest,
                world_frame_id=world_id,
                geodetic_anchor=anchor,
                main_camera_position_enu_m=position,
                baseline_orientation_enu_from_main=main_rotation,
                main_camera=station_cameras[0],
                auxiliary_camera=station_cameras[1],
                fixed_rotation_aux_from_main=rotation_aux_from_main,
                full_rigid_transform_aux_from_main=m.RigidTransform(
                    rotation_a_from_b=rotation_aux_from_main,
                    translation_a_m=(
                        -0.2 * aux_rotation[0][0],
                        -0.2 * aux_rotation[0][1],
                        -0.2 * aux_rotation[0][2],
                    ),
                ),
                reference_anchor=m.SurveyedReference(
                    reference_id=f"{sid}-a4",
                    world_frame_id=world_id,
                    landmarks=tuple(
                        m.SurveyedLandmark(
                            landmark_id=str(item.landmark_id),
                            position_enu_m=item.position_enu_m,
                            covariance_enu_m2=ZERO_COVARIANCE,
                            visual_description="alternating gray panel",
                        )
                        for item in panel_landmarks
                    ),
                ),
                reference_scene_version="demo-v1",
                installation_uncertainty=m.InstallationUncertainty(
                    state_ref=m.InstallationStateRef(
                        station_id=sid, config_digest=digest, state_version="demo-v1"
                    ),
                    position_covariance_enu_m2=ZERO_COVARIANCE,
                    orientation_covariance_enu_rad2=ZERO_COVARIANCE,
                    fixed_extrinsic_covariance_aux_rad2=ZERO_COVARIANCE,
                    measurement_source="synthetic exact installation",
                ),
            )
        )
    vegetation: list[s.VegetationSpec] = []
    vegetation_perturbations: list[s.VegetationPerturbation] = []
    for index, position in enumerate(
        ((-25.0, 27.0, 0.0), (10.0, 30.0, 0.0), (-31.0, 48.0, 0.0), (23.0, 52.0, 0.0))
    ):
        tree_id = f"demo-tree-{index + 1:02d}"
        trunk_proxy = s.CylinderGeometry(max(0.12, 2.2 * 0.12), 8.0 * 0.58)
        canopy_proxy = s.CylinderGeometry(2.2, 8.0 * 0.50)
        pose = state(position)
        canopy_pose = state((position[0], position[1], position[2] + 8.0 * 0.42))
        trunk_id, canopy_id = (
            s.VegetationId(f"{tree_id}/trunk"),
            s.VegetationId(f"{tree_id}/canopy"),
        )
        vegetation.extend(
            (
                s.VegetationSpec(
                    trunk_id,
                    f"演示树 {index + 1} 树干",
                    pose,
                    trunk_proxy,
                    s.SurfaceAppearance((0.20, 0.105, 0.045), 0.9),
                    s.VegetationMotionModel.STATIC,
                    True,
                    s.TreeVisualSpec(s.TreePart.TRUNK, trunk_proxy.height_m, trunk_proxy.radius_m),
                ),
                s.VegetationSpec(
                    canopy_id,
                    f"演示树 {index + 1} 树冠",
                    canopy_pose,
                    canopy_proxy,
                    s.SurfaceAppearance((0.12, 0.31, 0.09), 0.9),
                    s.VegetationMotionModel.WIND_DRIVEN,
                    True,
                    s.TreeVisualSpec(
                        s.TreePart.CANOPY, canopy_proxy.height_m, canopy_proxy.radius_m
                    ),
                ),
            )
        )
        vegetation_perturbations.append(
            s.VegetationPerturbation(s.WorldObjectId(str(canopy_id)), (0.3, 0.0, 0.0), 0.22)
        )
    return s.ScenarioSpec(
        "scene-v1",
        "video-demo",
        m.ConfigDigest(f"demo-v1-standard-{station_count}-{fps}"),
        s.SCENE_RANDOM_SEED,
        world_id,
        m.VoxelSearchSpec(
            world_frame_id=world_id,
            min_enu_m=(-100.0, -100.0, 0.0),
            max_enu_m=(100.0, 100.0, 100.0),
            step_enu_m=(10.0, 10.0, 10.0),
            forward_range_m=None,
        ),
        tuple(configs),
        s.SceneDefinition(
            s.ENUWorldSpec(world_id, anchor),
            s.TerrainSpec(
                s.WorldObjectId("ground"),
                s.TerrainModel.FLAT,
                0.0,
                0.0,
                None,
                None,
                s.SurfaceAppearance((0.36, 0.41, 0.32), 0.9),
                True,
            ),
            tuple(devices),
            tuple(cameras),
            tuple(objects),
            flying,
            tuple(landmarks),
            s.IlluminationSpec((-0.4, -0.5, 0.76), 20_000.0, (0.78, 0.84, 0.91)),
            tuple(vegetation),
        ),
        s.TimelineSpec(
            s.ClockDomainSpec(
                "demo-clock", s.TimeScale.EXPERIMENT_ELAPSED, "demo starts at zero", None
            ),
            0,
            DURATION_NS - 1,
            1_000_000,
        ),
        s.ImagingSpec(
            "srgb",
            1,
            s.SensorArtifactSpec(m.PixelFormat.RGB8, 0.0, 0.0, 1.0, False, None, None, False, None),
        ),
        s.VirtualObservationSpec(
            1.0,
            0.0,
            ((0.0, 0.0), (0.0, 0.0)),
            ZERO_COVARIANCE,
            m.ScoreDefinition(
                definition_id="demo-ranking",
                kind=m.ScoreKind.RANKING,
                minimum=0.0,
                maximum=1.0,
                cross_station_comparison_group=None,
            ),
            tuple(
                s.VirtualCalibrationInterval(
                    station_id=config.station_id,
                    start_time_ns=0,
                    end_time_ns=DURATION_NS,
                    health=m.CalibrationHealth.VALID,
                    correction_error_camera_rad=(0.0, 0.0, 0.0),
                    correction_covariance_rad2=ZERO_COVARIANCE,
                    reason_codes=("virtual_clean_baseline",),
                )
                for config in configs
            ),
        ),
        s.ExperimentPerturbations(
            (),
            (),
            s.WindPerturbation((2.5, 1.1, 0.0), (0.15, 0.15, 0.0)),
            (),
            None,
            None,
            tuple(vegetation_perturbations),
            (),
        ),
    )


def demo_source(
    spec: s.ScenarioSpec, *, render_every: int = 1, video_enabled: bool = True
) -> ScenarioModule:
    """Fresh sessions per launch/replay; all frame contents remain deterministic."""
    sessions = tuple(
        m.StationSessionBinding(
            station_id=item.station_id,
            image_session_id=m.SessionId(str(uuid4())),
            virtual_session_id=m.SessionId(str(uuid4())),
        )
        for item in spec.scene_definition.edge_devices
    )
    sequencers = tuple(
        EventSequencerModule(
            station_id=item.station_id,
            station_session_id=item.virtual_session_id,
            schema_version="event-v1",
        )
        for item in sessions
    )
    logic = SceneLogicModule(TrajectoryModule(), CameraProjectionModule(), VisibilityModule())
    return ScenarioModule(
        ExperimentClockModule(),
        logic,
        CameraImagingModule(),
        VirtualObservationModule(sequencers),
        sessions,
        render_every=render_every,
        video_enabled=video_enabled,
    )
