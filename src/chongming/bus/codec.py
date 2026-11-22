"""Bidirectional conversion between online contracts and Protobuf schemas."""

from __future__ import annotations

import math
from collections.abc import Sequence

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.visualization import ScoredVoxel, VoxelSpaceVisualization
from chongming_proto.online import common_pb2, online_pb2


def _enum_pair[E, P: int](mapping: dict[E, P], label: str) -> tuple[dict[E, P], dict[P, E]]:
    reverse = {value: key for key, value in mapping.items()}
    if len(reverse) != len(mapping):
        raise ValueError(f"duplicate protobuf enum codes in {label}")
    return mapping, reverse


_CAMERA_ROLE_TO_PB, _CAMERA_ROLE_FROM_PB = _enum_pair(
    {
        m.CameraRole.MAIN: common_pb2.CAMERA_ROLE_MAIN,
        m.CameraRole.AUXILIARY: common_pb2.CAMERA_ROLE_AUXILIARY,
    },
    "CameraRole",
)
_EVENT_TYPE_TO_PB, _EVENT_TYPE_FROM_PB = _enum_pair(
    {
        m.EventType.RAY: common_pb2.EVENT_TYPE_RAY,
        m.EventType.CALIBRATION_STATE: common_pb2.EVENT_TYPE_CALIBRATION_STATE,
        m.EventType.DIAGNOSTIC: common_pb2.EVENT_TYPE_DIAGNOSTIC,
    },
    "EventType",
)
_PROCESSING_STATUS_TO_PB, _PROCESSING_STATUS_FROM_PB = _enum_pair(
    {
        m.ProcessingStatus.PROCESSED: common_pb2.PROCESSING_STATUS_PROCESSED,
        m.ProcessingStatus.FAILED: common_pb2.PROCESSING_STATUS_FAILED,
    },
    "ProcessingStatus",
)
_SCORE_KIND_TO_PB, _SCORE_KIND_FROM_PB = _enum_pair(
    {
        m.ScoreKind.RANKING: common_pb2.SCORE_KIND_RANKING,
        m.ScoreKind.CALIBRATED_PROBABILITY: common_pb2.SCORE_KIND_CALIBRATED_PROBABILITY,
    },
    "ScoreKind",
)
_CALIBRATION_HEALTH_TO_PB, _CALIBRATION_HEALTH_FROM_PB = _enum_pair(
    {
        m.CalibrationHealth.INITIALIZING: common_pb2.CALIBRATION_HEALTH_INITIALIZING,
        m.CalibrationHealth.VALID: common_pb2.CALIBRATION_HEALTH_VALID,
        m.CalibrationHealth.FROZEN: common_pb2.CALIBRATION_HEALTH_FROZEN,
        m.CalibrationHealth.STALE: common_pb2.CALIBRATION_HEALTH_STALE,
        m.CalibrationHealth.RECOVERING: common_pb2.CALIBRATION_HEALTH_RECOVERING,
        m.CalibrationHealth.INVALID: common_pb2.CALIBRATION_HEALTH_INVALID,
    },
    "CalibrationHealth",
)
_CALIBRATION_USAGE_TO_PB, _CALIBRATION_USAGE_FROM_PB = _enum_pair(
    {
        m.CalibrationUsage.VALID: common_pb2.CALIBRATION_USAGE_VALID,
        m.CalibrationUsage.FROZEN: common_pb2.CALIBRATION_USAGE_FROZEN,
        m.CalibrationUsage.STALE: common_pb2.CALIBRATION_USAGE_STALE,
    },
    "CalibrationUsage",
)
_FUSION_STATUS_TO_PB, _FUSION_STATUS_FROM_PB = _enum_pair(
    {
        m.FusionStatus.LOCATED: common_pb2.FUSION_STATUS_LOCATED,
        m.FusionStatus.GEOMETRY_DEGENERATE: common_pb2.FUSION_STATUS_GEOMETRY_DEGENERATE,
        m.FusionStatus.ASSOCIATION_AMBIGUOUS: common_pb2.FUSION_STATUS_ASSOCIATION_AMBIGUOUS,
        m.FusionStatus.INSUFFICIENT_STATIONS: common_pb2.FUSION_STATUS_INSUFFICIENT_STATIONS,
        m.FusionStatus.NO_CANDIDATE_IN_VOLUME: common_pb2.FUSION_STATUS_NO_CANDIDATE_IN_VOLUME,
        m.FusionStatus.REJECTED_INPUT: common_pb2.FUSION_STATUS_REJECTED_INPUT,
    },
    "FusionStatus",
)
_TRACK_LIFECYCLE_TO_PB, _TRACK_LIFECYCLE_FROM_PB = _enum_pair(
    {
        m.TrackLifecycle.TENTATIVE: common_pb2.TRACK_LIFECYCLE_TENTATIVE,
        m.TrackLifecycle.CONFIRMED: common_pb2.TRACK_LIFECYCLE_CONFIRMED,
        m.TrackLifecycle.COASTING: common_pb2.TRACK_LIFECYCLE_COASTING,
        m.TrackLifecycle.DELETED: common_pb2.TRACK_LIFECYCLE_DELETED,
    },
    "TrackLifecycle",
)


def _finite(value: float, label: str) -> float:
    scalar = float(value)
    if not math.isfinite(scalar):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return scalar


def _non_empty(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _from_pb_enum[E, P: int](value: P, table: dict[P, E], label: str) -> E:
    resolved = table.get(value)
    if resolved is None:
        raise ValueError(f"unsupported {label} enum value: {value}")
    return resolved


def _vec2_to_proto(vec: m.Vec2Px, label: str = "vec2") -> common_pb2.Vec2:
    if len(vec) != 2:
        raise ValueError(f"{label} must have 2 elements")
    return common_pb2.Vec2(x=_finite(vec[0], f"{label}.x"), y=_finite(vec[1], f"{label}.y"))


def _vec2_from_proto(msg: common_pb2.Vec2, label: str = "vec2") -> m.Vec2Px:
    return (_finite(msg.x, f"{label}.x"), _finite(msg.y, f"{label}.y"))


def _vec3_to_proto(vec: m.Vec3, label: str = "vec3") -> common_pb2.Vec3:
    if len(vec) != 3:
        raise ValueError(f"{label} must have 3 elements")
    return common_pb2.Vec3(
        x=_finite(vec[0], f"{label}.x"),
        y=_finite(vec[1], f"{label}.y"),
        z=_finite(vec[2], f"{label}.z"),
    )


def _vec3_from_proto(msg: common_pb2.Vec3, label: str = "vec3") -> m.Vec3:
    return (
        _finite(msg.x, f"{label}.x"),
        _finite(msg.y, f"{label}.y"),
        _finite(msg.z, f"{label}.z"),
    )


def _flatten_matrix(matrix: Sequence[Sequence[float]], size: int, label: str) -> list[float]:
    if len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError(f"{label} must be {size}x{size}")
    return [_finite(item, label) for row in matrix for item in row]


def _mat2_to_proto(matrix: m.Matrix2x2, label: str = "mat2") -> common_pb2.Mat2:
    return common_pb2.Mat2(values=_flatten_matrix(matrix, 2, label))


def _mat2_from_proto(msg: common_pb2.Mat2, label: str = "mat2") -> m.Matrix2x2:
    if len(msg.values) != 4:
        raise ValueError(f"{label} must have 4 values, got {len(msg.values)}")
    v = [_finite(x, label) for x in msg.values]
    return ((v[0], v[1]), (v[2], v[3]))


def _mat3_to_proto(matrix: m.Matrix3x3, label: str = "mat3") -> common_pb2.Mat3:
    return common_pb2.Mat3(values=_flatten_matrix(matrix, 3, label))


def _mat3_from_proto(msg: common_pb2.Mat3, label: str = "mat3") -> m.Matrix3x3:
    if len(msg.values) != 9:
        raise ValueError(f"{label} must have 9 values, got {len(msg.values)}")
    v = [_finite(x, label) for x in msg.values]
    return ((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8]))


def _mat6_to_proto(matrix: m.Matrix6x6, label: str = "mat6") -> common_pb2.Mat6:
    return common_pb2.Mat6(values=_flatten_matrix(matrix, 6, label))


def _mat6_from_proto(msg: common_pb2.Mat6, label: str = "mat6") -> m.Matrix6x6:
    if len(msg.values) != 36:
        raise ValueError(f"{label} must have 36 values, got {len(msg.values)}")
    v = [_finite(x, label) for x in msg.values]
    return (
        (v[0], v[1], v[2], v[3], v[4], v[5]),
        (v[6], v[7], v[8], v[9], v[10], v[11]),
        (v[12], v[13], v[14], v[15], v[16], v[17]),
        (v[18], v[19], v[20], v[21], v[22], v[23]),
        (v[24], v[25], v[26], v[27], v[28], v[29]),
        (v[30], v[31], v[32], v[33], v[34], v[35]),
    )


def _processing_time_to_proto(value: m.ProcessingTime) -> common_pb2.ProcessingTime:
    return common_pb2.ProcessingTime(
        clock_domain_id=_non_empty(value.clock_domain_id, "clock_domain_id"),
        time_ns=int(value.time_ns),
    )


def _processing_time_from_proto(msg: common_pb2.ProcessingTime) -> m.ProcessingTime:
    return m.ProcessingTime(
        clock_domain_id=_non_empty(msg.clock_domain_id, "clock_domain_id"),
        time_ns=int(msg.time_ns),
    )


def _time_interval_to_proto(value: m.TimeInterval) -> common_pb2.TimeInterval:
    if value.end_ns is not None and value.end_ns < value.start_ns:
        raise ValueError("TimeInterval end_ns must be >= start_ns")
    msg = common_pb2.TimeInterval(
        clock_domain_id=_non_empty(value.clock_domain_id, "clock_domain_id"),
        start_ns=int(value.start_ns),
    )
    if value.end_ns is not None:
        msg.end_ns = int(value.end_ns)
    return msg


def _time_interval_from_proto(msg: common_pb2.TimeInterval) -> m.TimeInterval:
    end_ns = int(msg.end_ns) if msg.HasField("end_ns") else None
    if end_ns is not None and end_ns < int(msg.start_ns):
        raise ValueError("TimeInterval end_ns must be >= start_ns")
    return m.TimeInterval(
        clock_domain_id=_non_empty(msg.clock_domain_id, "clock_domain_id"),
        start_ns=int(msg.start_ns),
        end_ns=end_ns,
    )


def _capture_time_to_proto(value: m.CaptureTime) -> common_pb2.CaptureTime:
    if value.exposure_end_ns < value.exposure_start_ns:
        raise ValueError("CaptureTime exposure_end_ns must be >= exposure_start_ns")
    if value.clock_uncertainty_ns < 0:
        raise ValueError("CaptureTime clock_uncertainty_ns must be non-negative")
    return common_pb2.CaptureTime(
        clock_domain_id=_non_empty(value.clock_domain_id, "clock_domain_id"),
        exposure_start_ns=int(value.exposure_start_ns),
        exposure_end_ns=int(value.exposure_end_ns),
        clock_uncertainty_ns=int(value.clock_uncertainty_ns),
        rolling_shutter_model=_non_empty(value.rolling_shutter_model, "rolling_shutter_model"),
    )


def _capture_time_from_proto(msg: common_pb2.CaptureTime) -> m.CaptureTime:
    if msg.exposure_end_ns < msg.exposure_start_ns:
        raise ValueError("CaptureTime exposure_end_ns must be >= exposure_start_ns")
    if msg.clock_uncertainty_ns < 0:
        raise ValueError("CaptureTime clock_uncertainty_ns must be non-negative")
    return m.CaptureTime(
        clock_domain_id=_non_empty(msg.clock_domain_id, "clock_domain_id"),
        exposure_start_ns=int(msg.exposure_start_ns),
        exposure_end_ns=int(msg.exposure_end_ns),
        clock_uncertainty_ns=int(msg.clock_uncertainty_ns),
        rolling_shutter_model=_non_empty(msg.rolling_shutter_model, "rolling_shutter_model"),
    )


def _event_id_to_proto(value: m.EventId) -> common_pb2.EventId:
    if value[2] < 0:
        raise ValueError("EventId sequence_no must be non-negative")
    return common_pb2.EventId(
        station_id=_non_empty(str(value[0]), "station_id"),
        station_session_id=_non_empty(str(value[1]), "station_session_id"),
        sequence_no=int(value[2]),
    )


def _event_id_from_proto(msg: common_pb2.EventId) -> m.EventId:
    return (
        m.StationId(_non_empty(msg.station_id, "station_id")),
        m.SessionId(_non_empty(msg.station_session_id, "station_session_id")),
        int(msg.sequence_no),
    )


def _envelope_to_proto(value: m.EventEnvelope) -> common_pb2.EventEnvelope:
    if value.sequence_no < 0:
        raise ValueError("EventEnvelope sequence_no must be non-negative")
    return common_pb2.EventEnvelope(
        schema_version=_non_empty(value.schema_version, "schema_version"),
        event_type=_EVENT_TYPE_TO_PB[value.event_type],
        station_id=_non_empty(str(value.station_id), "station_id"),
        station_session_id=_non_empty(str(value.station_session_id), "station_session_id"),
        sequence_no=int(value.sequence_no),
        station_config_digest=_non_empty(str(value.station_config_digest), "station_config_digest"),
    )


def _envelope_from_proto(msg: common_pb2.EventEnvelope) -> m.EventEnvelope:
    return m.EventEnvelope(
        schema_version=_non_empty(msg.schema_version, "schema_version"),
        event_type=_from_pb_enum(msg.event_type, _EVENT_TYPE_FROM_PB, "EventType"),
        station_id=m.StationId(_non_empty(msg.station_id, "station_id")),
        station_session_id=m.SessionId(_non_empty(msg.station_session_id, "station_session_id")),
        sequence_no=int(msg.sequence_no),
        station_config_digest=m.ConfigDigest(
            _non_empty(msg.station_config_digest, "station_config_digest")
        ),
    )


def _frame_ref_to_proto(value: m.FrameRef) -> common_pb2.FrameRef:
    return common_pb2.FrameRef(
        station_id=_non_empty(str(value.station_id), "station_id"),
        station_session_id=_non_empty(str(value.station_session_id), "station_session_id"),
        camera_role=_CAMERA_ROLE_TO_PB[value.camera_role],
        frame_id=_non_empty(str(value.frame_id), "frame_id"),
    )


def _frame_ref_from_proto(msg: common_pb2.FrameRef) -> m.FrameRef:
    return m.FrameRef(
        station_id=m.StationId(_non_empty(msg.station_id, "station_id")),
        station_session_id=m.SessionId(_non_empty(msg.station_session_id, "station_session_id")),
        camera_role=_from_pb_enum(msg.camera_role, _CAMERA_ROLE_FROM_PB, "CameraRole"),
        frame_id=m.FrameId(_non_empty(msg.frame_id, "frame_id")),
    )


def _score_definition_to_proto(value: m.ScoreDefinition) -> common_pb2.ScoreDefinition:
    msg = common_pb2.ScoreDefinition(
        definition_id=_non_empty(value.definition_id, "definition_id"),
        kind=_SCORE_KIND_TO_PB[value.kind],
    )
    if value.minimum is not None:
        msg.minimum = _finite(value.minimum, "score_definition.minimum")
    if value.maximum is not None:
        msg.maximum = _finite(value.maximum, "score_definition.maximum")
    if value.cross_station_comparison_group is not None:
        msg.cross_station_comparison_group = value.cross_station_comparison_group
    return msg


def _score_definition_from_proto(msg: common_pb2.ScoreDefinition) -> m.ScoreDefinition:
    return m.ScoreDefinition(
        definition_id=_non_empty(msg.definition_id, "definition_id"),
        kind=_from_pb_enum(msg.kind, _SCORE_KIND_FROM_PB, "ScoreKind"),
        minimum=_finite(msg.minimum, "score_definition.minimum")
        if msg.HasField("minimum")
        else None,
        maximum=_finite(msg.maximum, "score_definition.maximum")
        if msg.HasField("maximum")
        else None,
        cross_station_comparison_group=msg.cross_station_comparison_group
        if msg.HasField("cross_station_comparison_group")
        else None,
    )


def _camera_model_to_proto(camera: m.CameraModel) -> online_pb2.CameraModel:
    if camera.intrinsics.image_width_px <= 0 or camera.intrinsics.image_height_px <= 0:
        raise ValueError("CameraIntrinsics dimensions must be positive")
    return online_pb2.CameraModel(
        intrinsics=online_pb2.CameraIntrinsics(
            image_width_px=int(camera.intrinsics.image_width_px),
            image_height_px=int(camera.intrinsics.image_height_px),
            matrix_px=_mat3_to_proto(camera.intrinsics.matrix_px, "camera.matrix_px"),
        ),
        distortion=online_pb2.CameraDistortion(
            model=_non_empty(camera.distortion.model, "distortion.model"),
            coefficients=[
                _finite(c, "distortion.coefficients") for c in camera.distortion.coefficients
            ],
        ),
    )


def _camera_model_from_proto(msg: online_pb2.CameraModel) -> m.CameraModel:
    if not msg.HasField("intrinsics") or not msg.HasField("distortion"):
        raise ValueError("CameraModel requires intrinsics and distortion")
    if msg.intrinsics.image_width_px <= 0 or msg.intrinsics.image_height_px <= 0:
        raise ValueError("CameraIntrinsics dimensions must be positive")
    return m.CameraModel(
        intrinsics=m.CameraIntrinsics(
            image_width_px=int(msg.intrinsics.image_width_px),
            image_height_px=int(msg.intrinsics.image_height_px),
            matrix_px=_mat3_from_proto(msg.intrinsics.matrix_px, "camera.matrix_px"),
        ),
        distortion=m.CameraDistortion(
            model=_non_empty(msg.distortion.model, "distortion.model"),
            coefficients=tuple(
                _finite(c, "distortion.coefficients") for c in msg.distortion.coefficients
            ),
        ),
    )


def _installation_ref_to_proto(value: m.InstallationStateRef) -> online_pb2.InstallationStateRef:
    return online_pb2.InstallationStateRef(
        station_id=_non_empty(str(value.station_id), "station_id"),
        config_digest=_non_empty(str(value.config_digest), "config_digest"),
        state_version=_non_empty(value.state_version, "state_version"),
    )


def _installation_ref_from_proto(msg: online_pb2.InstallationStateRef) -> m.InstallationStateRef:
    return m.InstallationStateRef(
        station_id=m.StationId(_non_empty(msg.station_id, "station_id")),
        config_digest=m.ConfigDigest(_non_empty(msg.config_digest, "config_digest")),
        state_version=_non_empty(msg.state_version, "state_version"),
    )


def _calibration_ref_to_proto(
    value: m.CalibrationSolutionRef,
) -> online_pb2.CalibrationSolutionRef:
    return online_pb2.CalibrationSolutionRef(
        station_id=_non_empty(str(value.station_id), "station_id"),
        station_session_id=_non_empty(str(value.station_session_id), "station_session_id"),
        solution_id=_non_empty(str(value.solution_id), "solution_id"),
    )


def _calibration_ref_from_proto(
    msg: online_pb2.CalibrationSolutionRef,
) -> m.CalibrationSolutionRef:
    return m.CalibrationSolutionRef(
        station_id=m.StationId(_non_empty(msg.station_id, "station_id")),
        station_session_id=m.SessionId(_non_empty(msg.station_session_id, "station_session_id")),
        solution_id=m.CalibrationSolutionId(_non_empty(msg.solution_id, "solution_id")),
    )


def station_config_to_proto(config: m.StationConfig) -> online_pb2.StationConfig:
    """Convert an online StationConfig contract into Protobuf."""
    unc = config.installation_uncertainty
    msg = online_pb2.StationConfig(
        station_id=_non_empty(str(config.station_id), "station_id"),
        config_version=_non_empty(config.config_version, "config_version"),
        config_digest=_non_empty(str(config.config_digest), "config_digest"),
        world_frame_id=_non_empty(str(config.world_frame_id), "world_frame_id"),
        geodetic_anchor=online_pb2.GeodeticAnchor(
            anchor_id=_non_empty(config.geodetic_anchor.anchor_id, "geodetic_anchor.anchor_id"),
            datum=_non_empty(config.geodetic_anchor.datum, "geodetic_anchor.datum"),
            datum_epoch=config.geodetic_anchor.datum_epoch,
            latitude_deg=_finite(config.geodetic_anchor.latitude_deg, "latitude_deg"),
            longitude_deg=_finite(config.geodetic_anchor.longitude_deg, "longitude_deg"),
            height_m=_finite(config.geodetic_anchor.height_m, "height_m"),
            height_reference=config.geodetic_anchor.height_reference,
        ),
        main_camera_position_enu_m=_vec3_to_proto(
            config.main_camera_position_enu_m, "main_camera_position_enu_m"
        ),
        baseline_orientation_enu_from_main=_mat3_to_proto(
            config.baseline_orientation_enu_from_main, "baseline_orientation_enu_from_main"
        ),
        main_camera=_camera_model_to_proto(config.main_camera),
        auxiliary_camera=_camera_model_to_proto(config.auxiliary_camera),
        fixed_rotation_aux_from_main=_mat3_to_proto(
            config.fixed_rotation_aux_from_main, "fixed_rotation_aux_from_main"
        ),
        reference_scene_version=config.reference_scene_version,
        installation_uncertainty=online_pb2.InstallationUncertainty(
            state_ref=_installation_ref_to_proto(unc.state_ref),
            position_covariance_enu_m2=_mat3_to_proto(
                unc.position_covariance_enu_m2, "position_covariance_enu_m2"
            ),
            orientation_covariance_enu_rad2=_mat3_to_proto(
                unc.orientation_covariance_enu_rad2, "orientation_covariance_enu_rad2"
            ),
            fixed_extrinsic_covariance_aux_rad2=_mat3_to_proto(
                unc.fixed_extrinsic_covariance_aux_rad2, "fixed_extrinsic_covariance_aux_rad2"
            ),
            measurement_source=unc.measurement_source,
        ),
    )
    if config.full_rigid_transform_aux_from_main is not None:
        tf = config.full_rigid_transform_aux_from_main
        msg.full_rigid_transform_aux_from_main.CopyFrom(
            online_pb2.RigidTransform(
                rotation_a_from_b=_mat3_to_proto(tf.rotation_a_from_b, "rotation_a_from_b"),
                translation_a_m=_vec3_to_proto(tf.translation_a_m, "translation_a_m"),
            )
        )
    return msg


def station_config_from_proto(
    msg: online_pb2.StationConfig,
    *,
    reference_anchor: m.ReferenceAnchor | None = None,
) -> m.StationConfig:
    """Convert a Protobuf StationConfig into the online contract dataclass."""
    for field_name in (
        "geodetic_anchor",
        "main_camera_position_enu_m",
        "baseline_orientation_enu_from_main",
        "main_camera",
        "auxiliary_camera",
        "fixed_rotation_aux_from_main",
        "installation_uncertainty",
    ):
        if not msg.HasField(field_name):
            raise ValueError(f"StationConfig missing required field {field_name!r}")
    world_id = m.WorldFrameId(_non_empty(msg.world_frame_id, "world_frame_id"))
    station_id = m.StationId(_non_empty(msg.station_id, "station_id"))
    anchor = (
        reference_anchor
        if reference_anchor is not None
        else m.SurveyedReference(
            reference_id=msg.reference_scene_version or f"{station_id}-ref",
            world_frame_id=world_id,
            landmarks=(),
        )
    )
    full_tf = (
        m.RigidTransform(
            rotation_a_from_b=_mat3_from_proto(
                msg.full_rigid_transform_aux_from_main.rotation_a_from_b, "rotation_a_from_b"
            ),
            translation_a_m=_vec3_from_proto(
                msg.full_rigid_transform_aux_from_main.translation_a_m, "translation_a_m"
            ),
        )
        if msg.HasField("full_rigid_transform_aux_from_main")
        else None
    )
    unc = msg.installation_uncertainty
    return m.StationConfig(
        station_id=station_id,
        config_version=_non_empty(msg.config_version, "config_version"),
        config_digest=m.ConfigDigest(_non_empty(msg.config_digest, "config_digest")),
        world_frame_id=world_id,
        geodetic_anchor=m.GeodeticAnchor(
            anchor_id=_non_empty(msg.geodetic_anchor.anchor_id, "geodetic_anchor.anchor_id"),
            datum=_non_empty(msg.geodetic_anchor.datum, "geodetic_anchor.datum"),
            datum_epoch=msg.geodetic_anchor.datum_epoch,
            latitude_deg=_finite(msg.geodetic_anchor.latitude_deg, "latitude_deg"),
            longitude_deg=_finite(msg.geodetic_anchor.longitude_deg, "longitude_deg"),
            height_m=_finite(msg.geodetic_anchor.height_m, "height_m"),
            height_reference=msg.geodetic_anchor.height_reference,
        ),
        main_camera_position_enu_m=_vec3_from_proto(
            msg.main_camera_position_enu_m, "main_camera_position_enu_m"
        ),
        baseline_orientation_enu_from_main=_mat3_from_proto(
            msg.baseline_orientation_enu_from_main, "baseline_orientation_enu_from_main"
        ),
        main_camera=_camera_model_from_proto(msg.main_camera),
        auxiliary_camera=_camera_model_from_proto(msg.auxiliary_camera),
        fixed_rotation_aux_from_main=_mat3_from_proto(
            msg.fixed_rotation_aux_from_main, "fixed_rotation_aux_from_main"
        ),
        full_rigid_transform_aux_from_main=full_tf,
        reference_anchor=anchor,
        reference_scene_version=msg.reference_scene_version,
        installation_uncertainty=m.InstallationUncertainty(
            state_ref=_installation_ref_from_proto(unc.state_ref),
            position_covariance_enu_m2=_mat3_from_proto(
                unc.position_covariance_enu_m2, "position_covariance_enu_m2"
            ),
            orientation_covariance_enu_rad2=_mat3_from_proto(
                unc.orientation_covariance_enu_rad2, "orientation_covariance_enu_rad2"
            ),
            fixed_extrinsic_covariance_aux_rad2=_mat3_from_proto(
                unc.fixed_extrinsic_covariance_aux_rad2, "fixed_extrinsic_covariance_aux_rad2"
            ),
            measurement_source=unc.measurement_source,
        ),
    )


def _observation_to_proto(obs: m.Observation2D) -> online_pb2.Observation2D:
    msg = online_pb2.Observation2D(
        main_frame_ref=_frame_ref_to_proto(obs.main_frame_ref),
        capture_time=_capture_time_to_proto(obs.capture_time),
        centroid_px=_vec2_to_proto(obs.centroid_px, "centroid_px"),
        detection_score=_finite(obs.detection_score, "detection_score"),
        detection_score_definition=_score_definition_to_proto(obs.detection_score_definition),
    )
    if obs.centroid_covariance_px2 is not None:
        msg.centroid_covariance_px2.CopyFrom(
            _mat2_to_proto(obs.centroid_covariance_px2, "centroid_covariance_px2")
        )
    if obs.local_track_id is not None:
        msg.local_track_id = str(obs.local_track_id)
    if obs.track_quality is not None:
        msg.track_quality = _finite(obs.track_quality, "track_quality")
    if obs.track_quality_definition is not None:
        msg.track_quality_definition.CopyFrom(
            _score_definition_to_proto(obs.track_quality_definition)
        )
    return msg


def _observation_from_proto(msg: online_pb2.Observation2D) -> m.Observation2D:
    return m.Observation2D(
        main_frame_ref=_frame_ref_from_proto(msg.main_frame_ref),
        capture_time=_capture_time_from_proto(msg.capture_time),
        centroid_px=_vec2_from_proto(msg.centroid_px, "centroid_px"),
        centroid_covariance_px2=_mat2_from_proto(
            msg.centroid_covariance_px2, "centroid_covariance_px2"
        )
        if msg.HasField("centroid_covariance_px2")
        else None,
        detection_score=_finite(msg.detection_score, "detection_score"),
        detection_score_definition=_score_definition_from_proto(msg.detection_score_definition),
        local_track_id=m.LocalTrackId(msg.local_track_id)
        if msg.HasField("local_track_id")
        else None,
        track_quality=_finite(msg.track_quality, "track_quality")
        if msg.HasField("track_quality")
        else None,
        track_quality_definition=_score_definition_from_proto(msg.track_quality_definition)
        if msg.HasField("track_quality_definition")
        else None,
    )


def detection_batch_to_proto(
    batch: m.DetectionBatch,
    *,
    decoded_pixel_sha256: str = "",
    decode_duration_ns: int = 0,
    cv_duration_ns: int = 0,
) -> online_pb2.DetectionBatch:
    """Convert DetectionBatch contract and timing/hash metadata into Protobuf."""
    if decode_duration_ns < 0 or cv_duration_ns < 0:
        raise ValueError("duration counters must be non-negative")
    return online_pb2.DetectionBatch(
        main_frame_ref=_frame_ref_to_proto(batch.main_frame_ref),
        capture_time=_capture_time_to_proto(batch.capture_time),
        processing_status=_PROCESSING_STATUS_TO_PB[batch.processing_status],
        observations=[_observation_to_proto(obs) for obs in batch.observations],
        reason_codes=list(batch.reason_codes),
        decoded_pixel_sha256=decoded_pixel_sha256,
        decode_duration_ns=int(decode_duration_ns),
        cv_duration_ns=int(cv_duration_ns),
    )


def detection_batch_from_proto(msg: online_pb2.DetectionBatch) -> m.DetectionBatch:
    """Convert a Protobuf DetectionBatch into the online contract dataclass."""
    return m.DetectionBatch(
        main_frame_ref=_frame_ref_from_proto(msg.main_frame_ref),
        capture_time=_capture_time_from_proto(msg.capture_time),
        processing_status=_from_pb_enum(
            msg.processing_status, _PROCESSING_STATUS_FROM_PB, "ProcessingStatus"
        ),
        observations=tuple(_observation_from_proto(obs) for obs in msg.observations),
        reason_codes=tuple(msg.reason_codes),
    )


def calibration_solution_to_proto(
    solution: m.CalibrationSolution,
) -> online_pb2.CalibrationSolution:
    """Convert a CalibrationSolution contract into Protobuf."""
    quality = online_pb2.CalibrationQuality(
        source=solution.quality.source,
        matched_reference_count=int(solution.quality.matched_reference_count),
        reason_codes=list(solution.quality.reason_codes),
    )
    if solution.quality.reprojection_rmse_px is not None:
        quality.reprojection_rmse_px = _finite(
            solution.quality.reprojection_rmse_px, "reprojection_rmse_px"
        )
    if solution.quality.angular_residual_rad is not None:
        quality.angular_residual_rad = _finite(
            solution.quality.angular_residual_rad, "angular_residual_rad"
        )
    if solution.quality.rotation_only_parallax_px is not None:
        quality.rotation_only_parallax_px = _finite(
            solution.quality.rotation_only_parallax_px, "rotation_only_parallax_px"
        )
    return online_pb2.CalibrationSolution(
        solution_ref=_calibration_ref_to_proto(solution.solution_ref),
        station_config_digest=_non_empty(
            str(solution.station_config_digest), "station_config_digest"
        ),
        world_frame_id=_non_empty(str(solution.world_frame_id), "world_frame_id"),
        reference_capture_time=_capture_time_to_proto(solution.reference_capture_time),
        accepted_time=_processing_time_to_proto(solution.accepted_time),
        valid_interval=_time_interval_to_proto(solution.valid_interval),
        main_camera_position_enu_m=_vec3_to_proto(
            solution.main_camera_position_enu_m, "main_camera_position_enu_m"
        ),
        rotation_enu_from_main=_mat3_to_proto(
            solution.rotation_enu_from_main, "rotation_enu_from_main"
        ),
        correction_covariance_enu_rad2=_mat3_to_proto(
            solution.correction_covariance_enu_rad2, "correction_covariance_enu_rad2"
        ),
        quality=quality,
    )


def calibration_solution_from_proto(
    msg: online_pb2.CalibrationSolution,
) -> m.CalibrationSolution:
    """Convert a Protobuf CalibrationSolution into the online contract dataclass."""
    q = msg.quality
    return m.CalibrationSolution(
        solution_ref=_calibration_ref_from_proto(msg.solution_ref),
        station_config_digest=m.ConfigDigest(
            _non_empty(msg.station_config_digest, "station_config_digest")
        ),
        world_frame_id=m.WorldFrameId(_non_empty(msg.world_frame_id, "world_frame_id")),
        reference_capture_time=_capture_time_from_proto(msg.reference_capture_time),
        accepted_time=_processing_time_from_proto(msg.accepted_time),
        valid_interval=_time_interval_from_proto(msg.valid_interval),
        main_camera_position_enu_m=_vec3_from_proto(
            msg.main_camera_position_enu_m, "main_camera_position_enu_m"
        ),
        rotation_enu_from_main=_mat3_from_proto(
            msg.rotation_enu_from_main, "rotation_enu_from_main"
        ),
        correction_covariance_enu_rad2=_mat3_from_proto(
            msg.correction_covariance_enu_rad2, "correction_covariance_enu_rad2"
        ),
        quality=m.CalibrationQuality(
            source=q.source,
            matched_reference_count=int(q.matched_reference_count),
            reprojection_rmse_px=_finite(q.reprojection_rmse_px, "reprojection_rmse_px")
            if q.HasField("reprojection_rmse_px")
            else None,
            angular_residual_rad=_finite(q.angular_residual_rad, "angular_residual_rad")
            if q.HasField("angular_residual_rad")
            else None,
            reason_codes=tuple(q.reason_codes),
            rotation_only_parallax_px=_finite(
                q.rotation_only_parallax_px, "rotation_only_parallax_px"
            )
            if q.HasField("rotation_only_parallax_px")
            else None,
        ),
    )


def calibration_state_to_proto(state: m.CalibrationState) -> online_pb2.CalibrationState:
    """Convert a CalibrationState contract into Protobuf."""
    if state.state_version < 0:
        raise ValueError("CalibrationState state_version must be non-negative")
    msg = online_pb2.CalibrationState(
        station_id=_non_empty(str(state.station_id), "station_id"),
        station_session_id=_non_empty(str(state.station_session_id), "station_session_id"),
        station_config_digest=_non_empty(str(state.station_config_digest), "station_config_digest"),
        state_version=int(state.state_version),
        health=_CALIBRATION_HEALTH_TO_PB[state.health],
        observed_at=_processing_time_to_proto(state.observed_at),
        reason_codes=list(state.reason_codes),
    )
    if state.solution_ref is not None:
        msg.solution_ref.CopyFrom(_calibration_ref_to_proto(state.solution_ref))
    if state.last_trusted_time is not None:
        msg.last_trusted_time.CopyFrom(_processing_time_to_proto(state.last_trusted_time))
    if state.usable_interval is not None:
        msg.usable_interval.CopyFrom(_time_interval_to_proto(state.usable_interval))
    return msg


def calibration_state_from_proto(msg: online_pb2.CalibrationState) -> m.CalibrationState:
    """Convert a Protobuf CalibrationState into the online contract dataclass."""
    return m.CalibrationState(
        station_id=m.StationId(_non_empty(msg.station_id, "station_id")),
        station_session_id=m.SessionId(_non_empty(msg.station_session_id, "station_session_id")),
        station_config_digest=m.ConfigDigest(
            _non_empty(msg.station_config_digest, "station_config_digest")
        ),
        state_version=int(msg.state_version),
        health=_from_pb_enum(msg.health, _CALIBRATION_HEALTH_FROM_PB, "CalibrationHealth"),
        solution_ref=_calibration_ref_from_proto(msg.solution_ref)
        if msg.HasField("solution_ref")
        else None,
        observed_at=_processing_time_from_proto(msg.observed_at),
        last_trusted_time=_processing_time_from_proto(msg.last_trusted_time)
        if msg.HasField("last_trusted_time")
        else None,
        usable_interval=_time_interval_from_proto(msg.usable_interval)
        if msg.HasField("usable_interval")
        else None,
        reason_codes=tuple(msg.reason_codes),
    )


def calibration_state_event_to_proto(
    event: m.CalibrationStateEvent,
) -> online_pb2.CalibrationStateEvent:
    """Convert a CalibrationStateEvent contract into Protobuf."""
    msg = online_pb2.CalibrationStateEvent(
        envelope=_envelope_to_proto(event.envelope),
        state=calibration_state_to_proto(event.state),
    )
    if event.solution is not None:
        msg.solution.CopyFrom(calibration_solution_to_proto(event.solution))
    return msg


def calibration_state_event_from_proto(
    msg: online_pb2.CalibrationStateEvent,
) -> m.CalibrationStateEvent:
    """Convert a Protobuf CalibrationStateEvent into the online contract dataclass."""
    return m.CalibrationStateEvent(
        envelope=_envelope_from_proto(msg.envelope),
        state=calibration_state_from_proto(msg.state),
        solution=calibration_solution_from_proto(msg.solution)
        if msg.HasField("solution")
        else None,
    )


def ray_event_to_proto(event: m.RayEvent) -> online_pb2.RayEvent:
    """Convert a RayEvent contract into Protobuf."""
    ev = event.calibration_evidence
    unc = event.uncertainty
    msg = online_pb2.RayEvent(
        envelope=_envelope_to_proto(event.envelope),
        frame_id=_non_empty(str(event.frame_id), "frame_id"),
        capture_time=_capture_time_to_proto(event.capture_time),
        world_frame_id=_non_empty(str(event.world_frame_id), "world_frame_id"),
        calibration_solution_ref=_calibration_ref_to_proto(event.calibration_solution_ref),
        calibration_usage=_CALIBRATION_USAGE_TO_PB[event.calibration_usage],
        calibration_evidence=online_pb2.CalibrationUseEvidence(
            state_version=int(ev.state_version),
            last_trusted_time=_processing_time_to_proto(ev.last_trusted_time),
            solution_accepted_time=_processing_time_to_proto(ev.solution_accepted_time),
            solution_reference_capture_time=_capture_time_to_proto(
                ev.solution_reference_capture_time
            ),
            solution_valid_interval=_time_interval_to_proto(ev.solution_valid_interval),
            usable_interval=_time_interval_to_proto(ev.usable_interval),
            reason_codes=list(ev.reason_codes),
        ),
        ray_origin_enu_m=_vec3_to_proto(event.ray_origin_enu_m, "ray_origin_enu_m"),
        ray_direction_enu_unit=_vec3_to_proto(
            event.ray_direction_enu_unit, "ray_direction_enu_unit"
        ),
        uncertainty=online_pb2.RayUncertainty(
            model=_non_empty(unc.model, "uncertainty.model"),
            tangent_u_enu_unit=_vec3_to_proto(unc.tangent_u_enu_unit, "tangent_u_enu_unit"),
            tangent_v_enu_unit=_vec3_to_proto(unc.tangent_v_enu_unit, "tangent_v_enu_unit"),
            independent_angular_covariance_rad2=_mat2_to_proto(
                unc.independent_angular_covariance_rad2, "independent_angular_covariance_rad2"
            ),
            independent_origin_covariance_m2=_mat3_to_proto(
                unc.independent_origin_covariance_m2, "independent_origin_covariance_m2"
            ),
            shared_installation_state_ref=_installation_ref_to_proto(
                unc.shared_installation_state_ref
            ),
            shared_calibration_solution_ref=_calibration_ref_to_proto(
                unc.shared_calibration_solution_ref
            ),
        ),
        detection_score=_finite(event.detection_score, "detection_score"),
        detection_score_definition=_score_definition_to_proto(event.detection_score_definition),
        quality_flags=list(event.quality_flags),
    )
    if event.local_track_id is not None:
        msg.local_track_id = str(event.local_track_id)
    if event.track_quality is not None:
        msg.track_quality = _finite(event.track_quality, "track_quality")
    if event.track_quality_definition is not None:
        msg.track_quality_definition.CopyFrom(
            _score_definition_to_proto(event.track_quality_definition)
        )
    return msg


def ray_event_from_proto(msg: online_pb2.RayEvent) -> m.RayEvent:
    """Convert a Protobuf RayEvent into the online contract dataclass."""
    ev = msg.calibration_evidence
    unc = msg.uncertainty
    return m.RayEvent(
        envelope=_envelope_from_proto(msg.envelope),
        frame_id=m.FrameId(_non_empty(msg.frame_id, "frame_id")),
        local_track_id=m.LocalTrackId(msg.local_track_id)
        if msg.HasField("local_track_id")
        else None,
        capture_time=_capture_time_from_proto(msg.capture_time),
        world_frame_id=m.WorldFrameId(_non_empty(msg.world_frame_id, "world_frame_id")),
        calibration_solution_ref=_calibration_ref_from_proto(msg.calibration_solution_ref),
        calibration_usage=_from_pb_enum(
            msg.calibration_usage, _CALIBRATION_USAGE_FROM_PB, "CalibrationUsage"
        ),
        calibration_evidence=m.CalibrationUseEvidence(
            state_version=int(ev.state_version),
            last_trusted_time=_processing_time_from_proto(ev.last_trusted_time),
            solution_accepted_time=_processing_time_from_proto(ev.solution_accepted_time),
            solution_reference_capture_time=_capture_time_from_proto(
                ev.solution_reference_capture_time
            ),
            solution_valid_interval=_time_interval_from_proto(ev.solution_valid_interval),
            usable_interval=_time_interval_from_proto(ev.usable_interval),
            reason_codes=tuple(ev.reason_codes),
        ),
        ray_origin_enu_m=_vec3_from_proto(msg.ray_origin_enu_m, "ray_origin_enu_m"),
        ray_direction_enu_unit=_vec3_from_proto(
            msg.ray_direction_enu_unit, "ray_direction_enu_unit"
        ),
        uncertainty=m.RayUncertainty(
            model=_non_empty(unc.model, "uncertainty.model"),
            tangent_u_enu_unit=_vec3_from_proto(unc.tangent_u_enu_unit, "tangent_u_enu_unit"),
            tangent_v_enu_unit=_vec3_from_proto(unc.tangent_v_enu_unit, "tangent_v_enu_unit"),
            independent_angular_covariance_rad2=_mat2_from_proto(
                unc.independent_angular_covariance_rad2, "independent_angular_covariance_rad2"
            ),
            independent_origin_covariance_m2=_mat3_from_proto(
                unc.independent_origin_covariance_m2, "independent_origin_covariance_m2"
            ),
            shared_installation_state_ref=_installation_ref_from_proto(
                unc.shared_installation_state_ref
            ),
            shared_calibration_solution_ref=_calibration_ref_from_proto(
                unc.shared_calibration_solution_ref
            ),
        ),
        detection_score=_finite(msg.detection_score, "detection_score"),
        detection_score_definition=_score_definition_from_proto(msg.detection_score_definition),
        track_quality=_finite(msg.track_quality, "track_quality")
        if msg.HasField("track_quality")
        else None,
        track_quality_definition=_score_definition_from_proto(msg.track_quality_definition)
        if msg.HasField("track_quality_definition")
        else None,
        quality_flags=tuple(msg.quality_flags),
    )


def diagnostic_event_to_proto(event: m.DiagnosticEvent) -> online_pb2.DiagnosticEvent:
    """Convert a DiagnosticEvent contract into Protobuf."""
    msg = online_pb2.DiagnosticEvent(
        envelope=_envelope_to_proto(event.envelope),
        category=_non_empty(event.category, "category"),
        reason_codes=list(event.reason_codes),
    )
    if event.related_frame_ref is not None:
        msg.related_frame_ref.CopyFrom(_frame_ref_to_proto(event.related_frame_ref))
    if event.capture_time is not None:
        msg.capture_time.CopyFrom(_capture_time_to_proto(event.capture_time))
    if event.related_config_ref is not None:
        msg.related_config_ref = str(event.related_config_ref)
    if event.related_calibration_ref is not None:
        msg.related_calibration_ref.CopyFrom(
            _calibration_ref_to_proto(event.related_calibration_ref)
        )
    return msg


def diagnostic_event_from_proto(msg: online_pb2.DiagnosticEvent) -> m.DiagnosticEvent:
    """Convert a Protobuf DiagnosticEvent into the online contract dataclass."""
    return m.DiagnosticEvent(
        envelope=_envelope_from_proto(msg.envelope),
        related_frame_ref=_frame_ref_from_proto(msg.related_frame_ref)
        if msg.HasField("related_frame_ref")
        else None,
        capture_time=_capture_time_from_proto(msg.capture_time)
        if msg.HasField("capture_time")
        else None,
        category=_non_empty(msg.category, "category"),
        reason_codes=tuple(msg.reason_codes),
        related_config_ref=m.ConfigDigest(msg.related_config_ref)
        if msg.HasField("related_config_ref")
        else None,
        related_calibration_ref=_calibration_ref_from_proto(msg.related_calibration_ref)
        if msg.HasField("related_calibration_ref")
        else None,
    )


def station_event_to_proto(event: m.StationEvent) -> dict[str, object]:
    """Return BusMessage payload kwargs for any StationEvent variant."""
    if isinstance(event, m.RayEvent):
        return {"ray_event": ray_event_to_proto(event)}
    if isinstance(event, m.CalibrationStateEvent):
        return {"calibration_state": calibration_state_event_to_proto(event)}
    return {"diagnostic_event": diagnostic_event_to_proto(event)}


def station_event_from_proto(msg: online_pb2.BusMessage) -> m.StationEvent:
    """Extract a StationEvent variant from a BusMessage."""
    kind = msg.WhichOneof("payload")
    if kind == "ray_event":
        return ray_event_from_proto(msg.ray_event)
    if kind == "calibration_state":
        return calibration_state_event_from_proto(msg.calibration_state)
    if kind == "diagnostic_event":
        return diagnostic_event_from_proto(msg.diagnostic_event)
    raise ValueError(f"BusMessage payload {kind!r} is not a StationEvent")


def fusion_measurement_to_proto(
    measurement: m.FusionMeasurement,
) -> online_pb2.FusionMeasurement:
    """Convert a FusionMeasurement contract into Protobuf."""
    msg = online_pb2.FusionMeasurement(
        center_session_id=_non_empty(str(measurement.center_session_id), "center_session_id"),
        measurement_id=_non_empty(str(measurement.measurement_id), "measurement_id"),
        clock_domain_id=_non_empty(measurement.clock_domain_id, "clock_domain_id"),
        world_frame_id=_non_empty(str(measurement.world_frame_id), "world_frame_id"),
        reference_time_ns=int(measurement.reference_time_ns),
        support_exposure_span_ns=int(measurement.support_exposure_span_ns),
        status=_FUSION_STATUS_TO_PB[measurement.status],
        supporting_ray_event_ids=[
            _event_id_to_proto(eid) for eid in measurement.supporting_ray_event_ids
        ],
        supporting_station_ids=[str(sid) for sid in measurement.supporting_station_ids],
        residuals_m=[_finite(r, "residuals_m") for r in measurement.residuals_m],
        reason_codes=list(measurement.reason_codes),
    )
    if measurement.position_enu_m is not None:
        msg.position_enu_m.CopyFrom(_vec3_to_proto(measurement.position_enu_m, "position_enu_m"))
    if measurement.conditional_position_covariance_enu_m2 is not None:
        msg.conditional_position_covariance_enu_m2.CopyFrom(
            _mat3_to_proto(
                measurement.conditional_position_covariance_enu_m2,
                "conditional_position_covariance_enu_m2",
            )
        )
    if measurement.motion_alignment is not None:
        ma = measurement.motion_alignment
        msg.motion_alignment.CopyFrom(
            online_pb2.MotionAlignmentEvidence(
                prior_track_id=_non_empty(str(ma.prior_track_id), "prior_track_id"),
                velocity_enu_mps=_vec3_to_proto(ma.velocity_enu_mps, "velocity_enu_mps"),
                velocity_projection_s=_mat3_to_proto(
                    ma.velocity_projection_s, "velocity_projection_s"
                ),
                independent_position_covariance_enu_m2=_mat3_to_proto(
                    ma.independent_position_covariance_enu_m2,
                    "independent_position_covariance_enu_m2",
                ),
            )
        )
    if measurement.geometry_condition is not None:
        msg.geometry_condition = _finite(measurement.geometry_condition, "geometry_condition")
    if measurement.minimum_intersection_angle_rad is not None:
        msg.minimum_intersection_angle_rad = _finite(
            measurement.minimum_intersection_angle_rad, "minimum_intersection_angle_rad"
        )
    if measurement.association_hypothesis_id is not None:
        msg.association_hypothesis_id = measurement.association_hypothesis_id
    return msg


def fusion_measurement_from_proto(
    msg: online_pb2.FusionMeasurement,
) -> m.FusionMeasurement:
    """Convert a Protobuf FusionMeasurement into the online contract dataclass."""
    motion = (
        m.MotionAlignmentEvidence(
            prior_track_id=m.TrackId(
                _non_empty(msg.motion_alignment.prior_track_id, "prior_track_id")
            ),
            velocity_enu_mps=_vec3_from_proto(
                msg.motion_alignment.velocity_enu_mps, "velocity_enu_mps"
            ),
            velocity_projection_s=_mat3_from_proto(
                msg.motion_alignment.velocity_projection_s, "velocity_projection_s"
            ),
            independent_position_covariance_enu_m2=_mat3_from_proto(
                msg.motion_alignment.independent_position_covariance_enu_m2,
                "independent_position_covariance_enu_m2",
            ),
        )
        if msg.HasField("motion_alignment")
        else None
    )
    return m.FusionMeasurement(
        center_session_id=m.SessionId(_non_empty(msg.center_session_id, "center_session_id")),
        measurement_id=m.MeasurementId(_non_empty(msg.measurement_id, "measurement_id")),
        clock_domain_id=_non_empty(msg.clock_domain_id, "clock_domain_id"),
        world_frame_id=m.WorldFrameId(_non_empty(msg.world_frame_id, "world_frame_id")),
        reference_time_ns=int(msg.reference_time_ns),
        support_exposure_span_ns=int(msg.support_exposure_span_ns),
        status=_from_pb_enum(msg.status, _FUSION_STATUS_FROM_PB, "FusionStatus"),
        position_enu_m=_vec3_from_proto(msg.position_enu_m, "position_enu_m")
        if msg.HasField("position_enu_m")
        else None,
        conditional_position_covariance_enu_m2=_mat3_from_proto(
            msg.conditional_position_covariance_enu_m2,
            "conditional_position_covariance_enu_m2",
        )
        if msg.HasField("conditional_position_covariance_enu_m2")
        else None,
        motion_alignment=motion,
        supporting_ray_event_ids=tuple(
            _event_id_from_proto(eid) for eid in msg.supporting_ray_event_ids
        ),
        supporting_station_ids=tuple(m.StationId(sid) for sid in msg.supporting_station_ids),
        residuals_m=tuple(_finite(r, "residuals_m") for r in msg.residuals_m),
        geometry_condition=_finite(msg.geometry_condition, "geometry_condition")
        if msg.HasField("geometry_condition")
        else None,
        minimum_intersection_angle_rad=_finite(
            msg.minimum_intersection_angle_rad, "minimum_intersection_angle_rad"
        )
        if msg.HasField("minimum_intersection_angle_rad")
        else None,
        association_hypothesis_id=msg.association_hypothesis_id
        if msg.HasField("association_hypothesis_id")
        else None,
        reason_codes=tuple(msg.reason_codes),
    )


def track_event_to_proto(track: m.TrackEvent) -> online_pb2.TrackEvent:
    """Convert a TrackEvent contract into Protobuf."""
    msg = online_pb2.TrackEvent(
        center_session_id=_non_empty(str(track.center_session_id), "center_session_id"),
        track_id=_non_empty(str(track.track_id), "track_id"),
        clock_domain_id=_non_empty(track.clock_domain_id, "clock_domain_id"),
        world_frame_id=_non_empty(str(track.world_frame_id), "world_frame_id"),
        event_time_ns=int(track.event_time_ns),
        lifecycle=_TRACK_LIFECYCLE_TO_PB[track.lifecycle],
        source_measurement_refs=[
            online_pb2.MeasurementRef(
                center_session_id=_non_empty(str(ref.center_session_id), "center_session_id"),
                measurement_id=_non_empty(str(ref.measurement_id), "measurement_id"),
            )
            for ref in track.source_measurement_refs
        ],
        reason_codes=list(track.reason_codes),
    )
    if track.position_enu_m is not None:
        msg.position_enu_m.CopyFrom(_vec3_to_proto(track.position_enu_m, "position_enu_m"))
    if track.velocity_enu_mps is not None:
        msg.velocity_enu_mps.CopyFrom(_vec3_to_proto(track.velocity_enu_mps, "velocity_enu_mps"))
    if track.conditional_state_covariance is not None:
        msg.conditional_state_covariance.CopyFrom(
            _mat6_to_proto(track.conditional_state_covariance, "conditional_state_covariance")
        )
    return msg


def track_event_from_proto(msg: online_pb2.TrackEvent) -> m.TrackEvent:
    """Convert a Protobuf TrackEvent into the online contract dataclass."""
    return m.TrackEvent(
        center_session_id=m.SessionId(_non_empty(msg.center_session_id, "center_session_id")),
        track_id=m.TrackId(_non_empty(msg.track_id, "track_id")),
        clock_domain_id=_non_empty(msg.clock_domain_id, "clock_domain_id"),
        world_frame_id=m.WorldFrameId(_non_empty(msg.world_frame_id, "world_frame_id")),
        event_time_ns=int(msg.event_time_ns),
        lifecycle=_from_pb_enum(msg.lifecycle, _TRACK_LIFECYCLE_FROM_PB, "TrackLifecycle"),
        position_enu_m=_vec3_from_proto(msg.position_enu_m, "position_enu_m")
        if msg.HasField("position_enu_m")
        else None,
        velocity_enu_mps=_vec3_from_proto(msg.velocity_enu_mps, "velocity_enu_mps")
        if msg.HasField("velocity_enu_mps")
        else None,
        conditional_state_covariance=_mat6_from_proto(
            msg.conditional_state_covariance, "conditional_state_covariance"
        )
        if msg.HasField("conditional_state_covariance")
        else None,
        source_measurement_refs=tuple(
            m.MeasurementRef(
                center_session_id=m.SessionId(
                    _non_empty(ref.center_session_id, "center_session_id")
                ),
                measurement_id=m.MeasurementId(_non_empty(ref.measurement_id, "measurement_id")),
            )
            for ref in msg.source_measurement_refs
        ),
        reason_codes=tuple(msg.reason_codes),
    )


def progress_report_to_proto(
    progress: f.StationEventTimeProgress,
    *,
    source_role: common_pb2.ComponentRole = common_pb2.COMPONENT_ROLE_EDGE,
    completed_sequence_no: int = 0,
) -> online_pb2.ProgressReport:
    """Convert StationEventTimeProgress into a Protobuf ProgressReport."""
    if completed_sequence_no < 0:
        raise ValueError("completed_sequence_no must be non-negative")
    completed_ns = 0
    if progress.completed_capture_time is not None:
        cap = progress.completed_capture_time
        completed_ns = (cap.exposure_start_ns + cap.exposure_end_ns) // 2
    elif not progress.stream_ended:
        raise ValueError("ordinary StationEventTimeProgress requires completed_capture_time")
    return online_pb2.ProgressReport(
        source_role=source_role,
        source_id=_non_empty(str(progress.station_id), "station_id"),
        session_id=_non_empty(str(progress.station_session_id), "station_session_id"),
        completed_time_ns=int(completed_ns),
        completed_sequence_no=int(completed_sequence_no),
        finite_end_reached=bool(progress.stream_ended),
    )


def progress_report_from_proto(
    msg: online_pb2.ProgressReport,
    *,
    station_config_digest: m.ConfigDigest,
    capture_clock_domain_id: str,
    received_at: m.ProcessingTime,
    has_completed_frame: bool = True,
) -> f.StationEventTimeProgress:
    """Convert an incoming station ProgressReport into StationEventTimeProgress."""
    if msg.source_role not in (
        common_pb2.COMPONENT_ROLE_EDGE,
        common_pb2.COMPONENT_ROLE_PRODUCER,
    ):
        raise ValueError(f"expected station ProgressReport role, got {msg.source_role}")
    capture_time: m.CaptureTime | None = None
    if has_completed_frame or not msg.finite_end_reached:
        capture_time = m.CaptureTime(
            clock_domain_id=_non_empty(capture_clock_domain_id, "capture_clock_domain_id"),
            exposure_start_ns=int(msg.completed_time_ns),
            exposure_end_ns=int(msg.completed_time_ns),
            clock_uncertainty_ns=0,
            rolling_shutter_model="none",
        )
    return f.StationEventTimeProgress(
        station_id=m.StationId(_non_empty(msg.source_id, "source_id")),
        station_session_id=m.SessionId(_non_empty(msg.session_id, "session_id")),
        station_config_digest=station_config_digest,
        completed_capture_time=capture_time,
        received_at=received_at,
        stream_ended=bool(msg.finite_end_reached),
    )


def sparse_voxel_grid_to_proto(
    *,
    center_session_id: m.SessionId,
    world_frame_id: m.WorldFrameId,
    window_start_ns: int,
    window_end_ns: int,
    snapshot: VoxelSpaceVisualization,
) -> online_pb2.SparseVoxelGrid:
    """Convert a VoxelSpaceVisualization snapshot into Protobuf SparseVoxelGrid."""
    return online_pb2.SparseVoxelGrid(
        center_session_id=_non_empty(str(center_session_id), "center_session_id"),
        world_frame_id=_non_empty(str(world_frame_id), "world_frame_id"),
        window_start_ns=int(window_start_ns),
        window_end_ns=int(window_end_ns),
        min_enu_m=_vec3_to_proto(snapshot.search_box_min_enu_m, "min_enu_m"),
        step_enu_m=_vec3_to_proto(snapshot.voxel_step_enu_m, "step_enu_m"),
        cells=[
            online_pb2.SparseVoxelCell(
                ix=int(voxel.grid_index[0]),
                iy=int(voxel.grid_index[1]),
                iz=int(voxel.grid_index[2]),
                station_votes=int(voxel.supporting_station_count),
            )
            for voxel in snapshot.scored_voxels
        ],
    )


def sparse_voxel_grid_from_proto(msg: online_pb2.SparseVoxelGrid) -> VoxelSpaceVisualization:
    """Convert a Protobuf SparseVoxelGrid back into VoxelSpaceVisualization."""
    min_enu = _vec3_from_proto(msg.min_enu_m, "min_enu_m")
    step_enu = _vec3_from_proto(msg.step_enu_m, "step_enu_m")
    if any(step <= 0 for step in step_enu):
        raise ValueError("SparseVoxelGrid step_enu_m must be strictly positive")
    voxels = tuple(
        ScoredVoxel(
            grid_index=(int(cell.ix), int(cell.iy), int(cell.iz)),
            score=int(cell.station_votes),
            supporting_station_count=int(cell.station_votes),
        )
        for cell in msg.cells
    )
    max_ix = max((v.grid_index[0] + 1 for v in voxels), default=0)
    max_iy = max((v.grid_index[1] + 1 for v in voxels), default=0)
    max_iz = max((v.grid_index[2] + 1 for v in voxels), default=0)
    max_enu = (
        min_enu[0] + max_ix * step_enu[0],
        min_enu[1] + max_iy * step_enu[1],
        min_enu[2] + max_iz * step_enu[2],
    )
    return VoxelSpaceVisualization(
        search_box_min_enu_m=min_enu,
        search_box_max_enu_m=max_enu,
        voxel_step_enu_m=step_enu,
        scored_voxels=voxels,
        max_score=max((v.score for v in voxels), default=0),
    )
