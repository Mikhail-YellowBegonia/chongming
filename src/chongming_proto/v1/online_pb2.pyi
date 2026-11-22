from chongming_proto.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionHello(_message.Message):
    __slots__ = ("protocol_version", "role", "instance_id", "session_id", "station_ids")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_IDS_FIELD_NUMBER: _ClassVar[int]
    protocol_version: str
    role: _common_pb2.ComponentRole
    instance_id: str
    session_id: str
    station_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, protocol_version: _Optional[str] = ..., role: _Optional[_Union[_common_pb2.ComponentRole, str]] = ..., instance_id: _Optional[str] = ..., session_id: _Optional[str] = ..., station_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class SessionAck(_message.Message):
    __slots__ = ("accepted", "reason", "run_id")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reason: str
    run_id: str
    def __init__(self, accepted: _Optional[bool] = ..., reason: _Optional[str] = ..., run_id: _Optional[str] = ...) -> None: ...

class CameraIntrinsics(_message.Message):
    __slots__ = ("image_width_px", "image_height_px", "matrix_px")
    IMAGE_WIDTH_PX_FIELD_NUMBER: _ClassVar[int]
    IMAGE_HEIGHT_PX_FIELD_NUMBER: _ClassVar[int]
    MATRIX_PX_FIELD_NUMBER: _ClassVar[int]
    image_width_px: int
    image_height_px: int
    matrix_px: _common_pb2.Mat3
    def __init__(self, image_width_px: _Optional[int] = ..., image_height_px: _Optional[int] = ..., matrix_px: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ...) -> None: ...

class CameraDistortion(_message.Message):
    __slots__ = ("model", "coefficients")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    COEFFICIENTS_FIELD_NUMBER: _ClassVar[int]
    model: str
    coefficients: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, model: _Optional[str] = ..., coefficients: _Optional[_Iterable[float]] = ...) -> None: ...

class CameraModel(_message.Message):
    __slots__ = ("intrinsics", "distortion")
    INTRINSICS_FIELD_NUMBER: _ClassVar[int]
    DISTORTION_FIELD_NUMBER: _ClassVar[int]
    intrinsics: CameraIntrinsics
    distortion: CameraDistortion
    def __init__(self, intrinsics: _Optional[_Union[CameraIntrinsics, _Mapping]] = ..., distortion: _Optional[_Union[CameraDistortion, _Mapping]] = ...) -> None: ...

class GeodeticAnchor(_message.Message):
    __slots__ = ("anchor_id", "datum", "datum_epoch", "latitude_deg", "longitude_deg", "height_m", "height_reference")
    ANCHOR_ID_FIELD_NUMBER: _ClassVar[int]
    DATUM_FIELD_NUMBER: _ClassVar[int]
    DATUM_EPOCH_FIELD_NUMBER: _ClassVar[int]
    LATITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_DEG_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_M_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_REFERENCE_FIELD_NUMBER: _ClassVar[int]
    anchor_id: str
    datum: str
    datum_epoch: str
    latitude_deg: float
    longitude_deg: float
    height_m: float
    height_reference: str
    def __init__(self, anchor_id: _Optional[str] = ..., datum: _Optional[str] = ..., datum_epoch: _Optional[str] = ..., latitude_deg: _Optional[float] = ..., longitude_deg: _Optional[float] = ..., height_m: _Optional[float] = ..., height_reference: _Optional[str] = ...) -> None: ...

class RigidTransform(_message.Message):
    __slots__ = ("rotation_a_from_b", "translation_a_m")
    ROTATION_A_FROM_B_FIELD_NUMBER: _ClassVar[int]
    TRANSLATION_A_M_FIELD_NUMBER: _ClassVar[int]
    rotation_a_from_b: _common_pb2.Mat3
    translation_a_m: _common_pb2.Vec3
    def __init__(self, rotation_a_from_b: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., translation_a_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ...) -> None: ...

class InstallationStateRef(_message.Message):
    __slots__ = ("station_id", "config_digest", "state_version")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    config_digest: str
    state_version: str
    def __init__(self, station_id: _Optional[str] = ..., config_digest: _Optional[str] = ..., state_version: _Optional[str] = ...) -> None: ...

class InstallationUncertainty(_message.Message):
    __slots__ = ("state_ref", "position_covariance_enu_m2", "orientation_covariance_enu_rad2", "fixed_extrinsic_covariance_aux_rad2", "measurement_source")
    STATE_REF_FIELD_NUMBER: _ClassVar[int]
    POSITION_COVARIANCE_ENU_M2_FIELD_NUMBER: _ClassVar[int]
    ORIENTATION_COVARIANCE_ENU_RAD2_FIELD_NUMBER: _ClassVar[int]
    FIXED_EXTRINSIC_COVARIANCE_AUX_RAD2_FIELD_NUMBER: _ClassVar[int]
    MEASUREMENT_SOURCE_FIELD_NUMBER: _ClassVar[int]
    state_ref: InstallationStateRef
    position_covariance_enu_m2: _common_pb2.Mat3
    orientation_covariance_enu_rad2: _common_pb2.Mat3
    fixed_extrinsic_covariance_aux_rad2: _common_pb2.Mat3
    measurement_source: str
    def __init__(self, state_ref: _Optional[_Union[InstallationStateRef, _Mapping]] = ..., position_covariance_enu_m2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., orientation_covariance_enu_rad2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., fixed_extrinsic_covariance_aux_rad2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., measurement_source: _Optional[str] = ...) -> None: ...

class StationConfig(_message.Message):
    __slots__ = ("station_id", "config_version", "config_digest", "world_frame_id", "geodetic_anchor", "main_camera_position_enu_m", "baseline_orientation_enu_from_main", "main_camera", "auxiliary_camera", "fixed_rotation_aux_from_main", "full_rigid_transform_aux_from_main", "reference_scene_version", "installation_uncertainty")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    GEODETIC_ANCHOR_FIELD_NUMBER: _ClassVar[int]
    MAIN_CAMERA_POSITION_ENU_M_FIELD_NUMBER: _ClassVar[int]
    BASELINE_ORIENTATION_ENU_FROM_MAIN_FIELD_NUMBER: _ClassVar[int]
    MAIN_CAMERA_FIELD_NUMBER: _ClassVar[int]
    AUXILIARY_CAMERA_FIELD_NUMBER: _ClassVar[int]
    FIXED_ROTATION_AUX_FROM_MAIN_FIELD_NUMBER: _ClassVar[int]
    FULL_RIGID_TRANSFORM_AUX_FROM_MAIN_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_SCENE_VERSION_FIELD_NUMBER: _ClassVar[int]
    INSTALLATION_UNCERTAINTY_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    config_version: str
    config_digest: str
    world_frame_id: str
    geodetic_anchor: GeodeticAnchor
    main_camera_position_enu_m: _common_pb2.Vec3
    baseline_orientation_enu_from_main: _common_pb2.Mat3
    main_camera: CameraModel
    auxiliary_camera: CameraModel
    fixed_rotation_aux_from_main: _common_pb2.Mat3
    full_rigid_transform_aux_from_main: RigidTransform
    reference_scene_version: str
    installation_uncertainty: InstallationUncertainty
    def __init__(self, station_id: _Optional[str] = ..., config_version: _Optional[str] = ..., config_digest: _Optional[str] = ..., world_frame_id: _Optional[str] = ..., geodetic_anchor: _Optional[_Union[GeodeticAnchor, _Mapping]] = ..., main_camera_position_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., baseline_orientation_enu_from_main: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., main_camera: _Optional[_Union[CameraModel, _Mapping]] = ..., auxiliary_camera: _Optional[_Union[CameraModel, _Mapping]] = ..., fixed_rotation_aux_from_main: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., full_rigid_transform_aux_from_main: _Optional[_Union[RigidTransform, _Mapping]] = ..., reference_scene_version: _Optional[str] = ..., installation_uncertainty: _Optional[_Union[InstallationUncertainty, _Mapping]] = ...) -> None: ...

class FramedVideoPacket(_message.Message):
    __slots__ = ("station_id", "station_session_id", "camera_role", "frame_id", "frame_index", "capture_time", "station_config_digest", "width_px", "height_px", "codec", "pixel_format", "row_stride_bytes", "is_keyframe", "payload")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CAMERA_ROLE_FIELD_NUMBER: _ClassVar[int]
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    FRAME_INDEX_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    WIDTH_PX_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_PX_FIELD_NUMBER: _ClassVar[int]
    CODEC_FIELD_NUMBER: _ClassVar[int]
    PIXEL_FORMAT_FIELD_NUMBER: _ClassVar[int]
    ROW_STRIDE_BYTES_FIELD_NUMBER: _ClassVar[int]
    IS_KEYFRAME_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    camera_role: _common_pb2.CameraRole
    frame_id: str
    frame_index: int
    capture_time: _common_pb2.CaptureTime
    station_config_digest: str
    width_px: int
    height_px: int
    codec: _common_pb2.VideoCodec
    pixel_format: _common_pb2.PixelFormat
    row_stride_bytes: int
    is_keyframe: bool
    payload: bytes
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., camera_role: _Optional[_Union[_common_pb2.CameraRole, str]] = ..., frame_id: _Optional[str] = ..., frame_index: _Optional[int] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., station_config_digest: _Optional[str] = ..., width_px: _Optional[int] = ..., height_px: _Optional[int] = ..., codec: _Optional[_Union[_common_pb2.VideoCodec, str]] = ..., pixel_format: _Optional[_Union[_common_pb2.PixelFormat, str]] = ..., row_stride_bytes: _Optional[int] = ..., is_keyframe: _Optional[bool] = ..., payload: _Optional[bytes] = ...) -> None: ...

class Observation2D(_message.Message):
    __slots__ = ("main_frame_ref", "capture_time", "centroid_px", "centroid_covariance_px2", "detection_score", "detection_score_definition", "local_track_id", "track_quality", "track_quality_definition")
    MAIN_FRAME_REF_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    CENTROID_PX_FIELD_NUMBER: _ClassVar[int]
    CENTROID_COVARIANCE_PX2_FIELD_NUMBER: _ClassVar[int]
    DETECTION_SCORE_FIELD_NUMBER: _ClassVar[int]
    DETECTION_SCORE_DEFINITION_FIELD_NUMBER: _ClassVar[int]
    LOCAL_TRACK_ID_FIELD_NUMBER: _ClassVar[int]
    TRACK_QUALITY_FIELD_NUMBER: _ClassVar[int]
    TRACK_QUALITY_DEFINITION_FIELD_NUMBER: _ClassVar[int]
    main_frame_ref: _common_pb2.FrameRef
    capture_time: _common_pb2.CaptureTime
    centroid_px: _common_pb2.Vec2
    centroid_covariance_px2: _common_pb2.Mat2
    detection_score: float
    detection_score_definition: _common_pb2.ScoreDefinition
    local_track_id: str
    track_quality: float
    track_quality_definition: _common_pb2.ScoreDefinition
    def __init__(self, main_frame_ref: _Optional[_Union[_common_pb2.FrameRef, _Mapping]] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., centroid_px: _Optional[_Union[_common_pb2.Vec2, _Mapping]] = ..., centroid_covariance_px2: _Optional[_Union[_common_pb2.Mat2, _Mapping]] = ..., detection_score: _Optional[float] = ..., detection_score_definition: _Optional[_Union[_common_pb2.ScoreDefinition, _Mapping]] = ..., local_track_id: _Optional[str] = ..., track_quality: _Optional[float] = ..., track_quality_definition: _Optional[_Union[_common_pb2.ScoreDefinition, _Mapping]] = ...) -> None: ...

class DetectionBatch(_message.Message):
    __slots__ = ("main_frame_ref", "capture_time", "processing_status", "observations", "reason_codes", "decoded_pixel_sha256", "decode_duration_ns", "cv_duration_ns")
    MAIN_FRAME_REF_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    PROCESSING_STATUS_FIELD_NUMBER: _ClassVar[int]
    OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    DECODED_PIXEL_SHA256_FIELD_NUMBER: _ClassVar[int]
    DECODE_DURATION_NS_FIELD_NUMBER: _ClassVar[int]
    CV_DURATION_NS_FIELD_NUMBER: _ClassVar[int]
    main_frame_ref: _common_pb2.FrameRef
    capture_time: _common_pb2.CaptureTime
    processing_status: _common_pb2.ProcessingStatus
    observations: _containers.RepeatedCompositeFieldContainer[Observation2D]
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    decoded_pixel_sha256: str
    decode_duration_ns: int
    cv_duration_ns: int
    def __init__(self, main_frame_ref: _Optional[_Union[_common_pb2.FrameRef, _Mapping]] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., processing_status: _Optional[_Union[_common_pb2.ProcessingStatus, str]] = ..., observations: _Optional[_Iterable[_Union[Observation2D, _Mapping]]] = ..., reason_codes: _Optional[_Iterable[str]] = ..., decoded_pixel_sha256: _Optional[str] = ..., decode_duration_ns: _Optional[int] = ..., cv_duration_ns: _Optional[int] = ...) -> None: ...

class CalibrationSolutionRef(_message.Message):
    __slots__ = ("station_id", "station_session_id", "solution_id")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_ID_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    solution_id: str
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., solution_id: _Optional[str] = ...) -> None: ...

class CalibrationQuality(_message.Message):
    __slots__ = ("source", "matched_reference_count", "reprojection_rmse_px", "angular_residual_rad", "reason_codes", "rotation_only_parallax_px")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    MATCHED_REFERENCE_COUNT_FIELD_NUMBER: _ClassVar[int]
    REPROJECTION_RMSE_PX_FIELD_NUMBER: _ClassVar[int]
    ANGULAR_RESIDUAL_RAD_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    ROTATION_ONLY_PARALLAX_PX_FIELD_NUMBER: _ClassVar[int]
    source: str
    matched_reference_count: int
    reprojection_rmse_px: float
    angular_residual_rad: float
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    rotation_only_parallax_px: float
    def __init__(self, source: _Optional[str] = ..., matched_reference_count: _Optional[int] = ..., reprojection_rmse_px: _Optional[float] = ..., angular_residual_rad: _Optional[float] = ..., reason_codes: _Optional[_Iterable[str]] = ..., rotation_only_parallax_px: _Optional[float] = ...) -> None: ...

class CalibrationSolution(_message.Message):
    __slots__ = ("solution_ref", "station_config_digest", "world_frame_id", "reference_capture_time", "accepted_time", "valid_interval", "main_camera_position_enu_m", "rotation_enu_from_main", "correction_covariance_enu_rad2", "quality")
    SOLUTION_REF_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_TIME_FIELD_NUMBER: _ClassVar[int]
    VALID_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    MAIN_CAMERA_POSITION_ENU_M_FIELD_NUMBER: _ClassVar[int]
    ROTATION_ENU_FROM_MAIN_FIELD_NUMBER: _ClassVar[int]
    CORRECTION_COVARIANCE_ENU_RAD2_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FIELD_NUMBER: _ClassVar[int]
    solution_ref: CalibrationSolutionRef
    station_config_digest: str
    world_frame_id: str
    reference_capture_time: _common_pb2.CaptureTime
    accepted_time: _common_pb2.ProcessingTime
    valid_interval: _common_pb2.TimeInterval
    main_camera_position_enu_m: _common_pb2.Vec3
    rotation_enu_from_main: _common_pb2.Mat3
    correction_covariance_enu_rad2: _common_pb2.Mat3
    quality: CalibrationQuality
    def __init__(self, solution_ref: _Optional[_Union[CalibrationSolutionRef, _Mapping]] = ..., station_config_digest: _Optional[str] = ..., world_frame_id: _Optional[str] = ..., reference_capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., accepted_time: _Optional[_Union[_common_pb2.ProcessingTime, _Mapping]] = ..., valid_interval: _Optional[_Union[_common_pb2.TimeInterval, _Mapping]] = ..., main_camera_position_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., rotation_enu_from_main: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., correction_covariance_enu_rad2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., quality: _Optional[_Union[CalibrationQuality, _Mapping]] = ...) -> None: ...

class CalibrationState(_message.Message):
    __slots__ = ("station_id", "station_session_id", "station_config_digest", "state_version", "health", "solution_ref", "observed_at", "last_trusted_time", "usable_interval", "reason_codes")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_REF_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_TRUSTED_TIME_FIELD_NUMBER: _ClassVar[int]
    USABLE_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    station_config_digest: str
    state_version: int
    health: _common_pb2.CalibrationHealth
    solution_ref: CalibrationSolutionRef
    observed_at: _common_pb2.ProcessingTime
    last_trusted_time: _common_pb2.ProcessingTime
    usable_interval: _common_pb2.TimeInterval
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., station_config_digest: _Optional[str] = ..., state_version: _Optional[int] = ..., health: _Optional[_Union[_common_pb2.CalibrationHealth, str]] = ..., solution_ref: _Optional[_Union[CalibrationSolutionRef, _Mapping]] = ..., observed_at: _Optional[_Union[_common_pb2.ProcessingTime, _Mapping]] = ..., last_trusted_time: _Optional[_Union[_common_pb2.ProcessingTime, _Mapping]] = ..., usable_interval: _Optional[_Union[_common_pb2.TimeInterval, _Mapping]] = ..., reason_codes: _Optional[_Iterable[str]] = ...) -> None: ...

class CalibrationStateEvent(_message.Message):
    __slots__ = ("envelope", "state", "solution")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.EventEnvelope
    state: CalibrationState
    solution: CalibrationSolution
    def __init__(self, envelope: _Optional[_Union[_common_pb2.EventEnvelope, _Mapping]] = ..., state: _Optional[_Union[CalibrationState, _Mapping]] = ..., solution: _Optional[_Union[CalibrationSolution, _Mapping]] = ...) -> None: ...

class CalibrationUseEvidence(_message.Message):
    __slots__ = ("state_version", "last_trusted_time", "solution_accepted_time", "solution_reference_capture_time", "solution_valid_interval", "usable_interval", "reason_codes")
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    LAST_TRUSTED_TIME_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_ACCEPTED_TIME_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_REFERENCE_CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    SOLUTION_VALID_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    USABLE_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    state_version: int
    last_trusted_time: _common_pb2.ProcessingTime
    solution_accepted_time: _common_pb2.ProcessingTime
    solution_reference_capture_time: _common_pb2.CaptureTime
    solution_valid_interval: _common_pb2.TimeInterval
    usable_interval: _common_pb2.TimeInterval
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, state_version: _Optional[int] = ..., last_trusted_time: _Optional[_Union[_common_pb2.ProcessingTime, _Mapping]] = ..., solution_accepted_time: _Optional[_Union[_common_pb2.ProcessingTime, _Mapping]] = ..., solution_reference_capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., solution_valid_interval: _Optional[_Union[_common_pb2.TimeInterval, _Mapping]] = ..., usable_interval: _Optional[_Union[_common_pb2.TimeInterval, _Mapping]] = ..., reason_codes: _Optional[_Iterable[str]] = ...) -> None: ...

class RayUncertainty(_message.Message):
    __slots__ = ("model", "tangent_u_enu_unit", "tangent_v_enu_unit", "independent_angular_covariance_rad2", "independent_origin_covariance_m2", "shared_installation_state_ref", "shared_calibration_solution_ref")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TANGENT_U_ENU_UNIT_FIELD_NUMBER: _ClassVar[int]
    TANGENT_V_ENU_UNIT_FIELD_NUMBER: _ClassVar[int]
    INDEPENDENT_ANGULAR_COVARIANCE_RAD2_FIELD_NUMBER: _ClassVar[int]
    INDEPENDENT_ORIGIN_COVARIANCE_M2_FIELD_NUMBER: _ClassVar[int]
    SHARED_INSTALLATION_STATE_REF_FIELD_NUMBER: _ClassVar[int]
    SHARED_CALIBRATION_SOLUTION_REF_FIELD_NUMBER: _ClassVar[int]
    model: str
    tangent_u_enu_unit: _common_pb2.Vec3
    tangent_v_enu_unit: _common_pb2.Vec3
    independent_angular_covariance_rad2: _common_pb2.Mat2
    independent_origin_covariance_m2: _common_pb2.Mat3
    shared_installation_state_ref: InstallationStateRef
    shared_calibration_solution_ref: CalibrationSolutionRef
    def __init__(self, model: _Optional[str] = ..., tangent_u_enu_unit: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., tangent_v_enu_unit: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., independent_angular_covariance_rad2: _Optional[_Union[_common_pb2.Mat2, _Mapping]] = ..., independent_origin_covariance_m2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., shared_installation_state_ref: _Optional[_Union[InstallationStateRef, _Mapping]] = ..., shared_calibration_solution_ref: _Optional[_Union[CalibrationSolutionRef, _Mapping]] = ...) -> None: ...

class RayEvent(_message.Message):
    __slots__ = ("envelope", "frame_id", "local_track_id", "capture_time", "world_frame_id", "calibration_solution_ref", "calibration_usage", "calibration_evidence", "ray_origin_enu_m", "ray_direction_enu_unit", "uncertainty", "detection_score", "detection_score_definition", "track_quality", "track_quality_definition", "quality_flags")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    LOCAL_TRACK_ID_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    CALIBRATION_SOLUTION_REF_FIELD_NUMBER: _ClassVar[int]
    CALIBRATION_USAGE_FIELD_NUMBER: _ClassVar[int]
    CALIBRATION_EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    RAY_ORIGIN_ENU_M_FIELD_NUMBER: _ClassVar[int]
    RAY_DIRECTION_ENU_UNIT_FIELD_NUMBER: _ClassVar[int]
    UNCERTAINTY_FIELD_NUMBER: _ClassVar[int]
    DETECTION_SCORE_FIELD_NUMBER: _ClassVar[int]
    DETECTION_SCORE_DEFINITION_FIELD_NUMBER: _ClassVar[int]
    TRACK_QUALITY_FIELD_NUMBER: _ClassVar[int]
    TRACK_QUALITY_DEFINITION_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FLAGS_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.EventEnvelope
    frame_id: str
    local_track_id: str
    capture_time: _common_pb2.CaptureTime
    world_frame_id: str
    calibration_solution_ref: CalibrationSolutionRef
    calibration_usage: _common_pb2.CalibrationUsage
    calibration_evidence: CalibrationUseEvidence
    ray_origin_enu_m: _common_pb2.Vec3
    ray_direction_enu_unit: _common_pb2.Vec3
    uncertainty: RayUncertainty
    detection_score: float
    detection_score_definition: _common_pb2.ScoreDefinition
    track_quality: float
    track_quality_definition: _common_pb2.ScoreDefinition
    quality_flags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, envelope: _Optional[_Union[_common_pb2.EventEnvelope, _Mapping]] = ..., frame_id: _Optional[str] = ..., local_track_id: _Optional[str] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., world_frame_id: _Optional[str] = ..., calibration_solution_ref: _Optional[_Union[CalibrationSolutionRef, _Mapping]] = ..., calibration_usage: _Optional[_Union[_common_pb2.CalibrationUsage, str]] = ..., calibration_evidence: _Optional[_Union[CalibrationUseEvidence, _Mapping]] = ..., ray_origin_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., ray_direction_enu_unit: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., uncertainty: _Optional[_Union[RayUncertainty, _Mapping]] = ..., detection_score: _Optional[float] = ..., detection_score_definition: _Optional[_Union[_common_pb2.ScoreDefinition, _Mapping]] = ..., track_quality: _Optional[float] = ..., track_quality_definition: _Optional[_Union[_common_pb2.ScoreDefinition, _Mapping]] = ..., quality_flags: _Optional[_Iterable[str]] = ...) -> None: ...

class DiagnosticEvent(_message.Message):
    __slots__ = ("envelope", "related_frame_ref", "capture_time", "category", "reason_codes", "related_config_ref", "related_calibration_ref")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    RELATED_FRAME_REF_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    RELATED_CONFIG_REF_FIELD_NUMBER: _ClassVar[int]
    RELATED_CALIBRATION_REF_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.EventEnvelope
    related_frame_ref: _common_pb2.FrameRef
    capture_time: _common_pb2.CaptureTime
    category: str
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    related_config_ref: str
    related_calibration_ref: CalibrationSolutionRef
    def __init__(self, envelope: _Optional[_Union[_common_pb2.EventEnvelope, _Mapping]] = ..., related_frame_ref: _Optional[_Union[_common_pb2.FrameRef, _Mapping]] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., category: _Optional[str] = ..., reason_codes: _Optional[_Iterable[str]] = ..., related_config_ref: _Optional[str] = ..., related_calibration_ref: _Optional[_Union[CalibrationSolutionRef, _Mapping]] = ...) -> None: ...

class MeasurementRef(_message.Message):
    __slots__ = ("center_session_id", "measurement_id")
    CENTER_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MEASUREMENT_ID_FIELD_NUMBER: _ClassVar[int]
    center_session_id: str
    measurement_id: str
    def __init__(self, center_session_id: _Optional[str] = ..., measurement_id: _Optional[str] = ...) -> None: ...

class MotionAlignmentEvidence(_message.Message):
    __slots__ = ("prior_track_id", "velocity_enu_mps", "velocity_projection_s", "independent_position_covariance_enu_m2")
    PRIOR_TRACK_ID_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_ENU_MPS_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_PROJECTION_S_FIELD_NUMBER: _ClassVar[int]
    INDEPENDENT_POSITION_COVARIANCE_ENU_M2_FIELD_NUMBER: _ClassVar[int]
    prior_track_id: str
    velocity_enu_mps: _common_pb2.Vec3
    velocity_projection_s: _common_pb2.Mat3
    independent_position_covariance_enu_m2: _common_pb2.Mat3
    def __init__(self, prior_track_id: _Optional[str] = ..., velocity_enu_mps: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., velocity_projection_s: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., independent_position_covariance_enu_m2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ...) -> None: ...

class FusionMeasurement(_message.Message):
    __slots__ = ("center_session_id", "measurement_id", "clock_domain_id", "world_frame_id", "reference_time_ns", "support_exposure_span_ns", "status", "position_enu_m", "conditional_position_covariance_enu_m2", "motion_alignment", "supporting_ray_event_ids", "supporting_station_ids", "residuals_m", "geometry_condition", "minimum_intersection_angle_rad", "association_hypothesis_id", "reason_codes")
    CENTER_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MEASUREMENT_ID_FIELD_NUMBER: _ClassVar[int]
    CLOCK_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    SUPPORT_EXPOSURE_SPAN_NS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    POSITION_ENU_M_FIELD_NUMBER: _ClassVar[int]
    CONDITIONAL_POSITION_COVARIANCE_ENU_M2_FIELD_NUMBER: _ClassVar[int]
    MOTION_ALIGNMENT_FIELD_NUMBER: _ClassVar[int]
    SUPPORTING_RAY_EVENT_IDS_FIELD_NUMBER: _ClassVar[int]
    SUPPORTING_STATION_IDS_FIELD_NUMBER: _ClassVar[int]
    RESIDUALS_M_FIELD_NUMBER: _ClassVar[int]
    GEOMETRY_CONDITION_FIELD_NUMBER: _ClassVar[int]
    MINIMUM_INTERSECTION_ANGLE_RAD_FIELD_NUMBER: _ClassVar[int]
    ASSOCIATION_HYPOTHESIS_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    center_session_id: str
    measurement_id: str
    clock_domain_id: str
    world_frame_id: str
    reference_time_ns: int
    support_exposure_span_ns: int
    status: _common_pb2.FusionStatus
    position_enu_m: _common_pb2.Vec3
    conditional_position_covariance_enu_m2: _common_pb2.Mat3
    motion_alignment: MotionAlignmentEvidence
    supporting_ray_event_ids: _containers.RepeatedCompositeFieldContainer[_common_pb2.EventId]
    supporting_station_ids: _containers.RepeatedScalarFieldContainer[str]
    residuals_m: _containers.RepeatedScalarFieldContainer[float]
    geometry_condition: float
    minimum_intersection_angle_rad: float
    association_hypothesis_id: str
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, center_session_id: _Optional[str] = ..., measurement_id: _Optional[str] = ..., clock_domain_id: _Optional[str] = ..., world_frame_id: _Optional[str] = ..., reference_time_ns: _Optional[int] = ..., support_exposure_span_ns: _Optional[int] = ..., status: _Optional[_Union[_common_pb2.FusionStatus, str]] = ..., position_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., conditional_position_covariance_enu_m2: _Optional[_Union[_common_pb2.Mat3, _Mapping]] = ..., motion_alignment: _Optional[_Union[MotionAlignmentEvidence, _Mapping]] = ..., supporting_ray_event_ids: _Optional[_Iterable[_Union[_common_pb2.EventId, _Mapping]]] = ..., supporting_station_ids: _Optional[_Iterable[str]] = ..., residuals_m: _Optional[_Iterable[float]] = ..., geometry_condition: _Optional[float] = ..., minimum_intersection_angle_rad: _Optional[float] = ..., association_hypothesis_id: _Optional[str] = ..., reason_codes: _Optional[_Iterable[str]] = ...) -> None: ...

class TrackEvent(_message.Message):
    __slots__ = ("center_session_id", "track_id", "clock_domain_id", "world_frame_id", "event_time_ns", "lifecycle", "position_enu_m", "velocity_enu_mps", "conditional_state_covariance", "source_measurement_refs", "reason_codes")
    CENTER_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACK_ID_FIELD_NUMBER: _ClassVar[int]
    CLOCK_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_FIELD_NUMBER: _ClassVar[int]
    POSITION_ENU_M_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_ENU_MPS_FIELD_NUMBER: _ClassVar[int]
    CONDITIONAL_STATE_COVARIANCE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_MEASUREMENT_REFS_FIELD_NUMBER: _ClassVar[int]
    REASON_CODES_FIELD_NUMBER: _ClassVar[int]
    center_session_id: str
    track_id: str
    clock_domain_id: str
    world_frame_id: str
    event_time_ns: int
    lifecycle: _common_pb2.TrackLifecycle
    position_enu_m: _common_pb2.Vec3
    velocity_enu_mps: _common_pb2.Vec3
    conditional_state_covariance: _common_pb2.Mat6
    source_measurement_refs: _containers.RepeatedCompositeFieldContainer[MeasurementRef]
    reason_codes: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, center_session_id: _Optional[str] = ..., track_id: _Optional[str] = ..., clock_domain_id: _Optional[str] = ..., world_frame_id: _Optional[str] = ..., event_time_ns: _Optional[int] = ..., lifecycle: _Optional[_Union[_common_pb2.TrackLifecycle, str]] = ..., position_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., velocity_enu_mps: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., conditional_state_covariance: _Optional[_Union[_common_pb2.Mat6, _Mapping]] = ..., source_measurement_refs: _Optional[_Iterable[_Union[MeasurementRef, _Mapping]]] = ..., reason_codes: _Optional[_Iterable[str]] = ...) -> None: ...

class SparseVoxelCell(_message.Message):
    __slots__ = ("ix", "iy", "iz", "station_votes")
    IX_FIELD_NUMBER: _ClassVar[int]
    IY_FIELD_NUMBER: _ClassVar[int]
    IZ_FIELD_NUMBER: _ClassVar[int]
    STATION_VOTES_FIELD_NUMBER: _ClassVar[int]
    ix: int
    iy: int
    iz: int
    station_votes: int
    def __init__(self, ix: _Optional[int] = ..., iy: _Optional[int] = ..., iz: _Optional[int] = ..., station_votes: _Optional[int] = ...) -> None: ...

class SparseVoxelGrid(_message.Message):
    __slots__ = ("center_session_id", "world_frame_id", "window_start_ns", "window_end_ns", "min_enu_m", "step_enu_m", "cells")
    CENTER_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    WINDOW_START_NS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_END_NS_FIELD_NUMBER: _ClassVar[int]
    MIN_ENU_M_FIELD_NUMBER: _ClassVar[int]
    STEP_ENU_M_FIELD_NUMBER: _ClassVar[int]
    CELLS_FIELD_NUMBER: _ClassVar[int]
    center_session_id: str
    world_frame_id: str
    window_start_ns: int
    window_end_ns: int
    min_enu_m: _common_pb2.Vec3
    step_enu_m: _common_pb2.Vec3
    cells: _containers.RepeatedCompositeFieldContainer[SparseVoxelCell]
    def __init__(self, center_session_id: _Optional[str] = ..., world_frame_id: _Optional[str] = ..., window_start_ns: _Optional[int] = ..., window_end_ns: _Optional[int] = ..., min_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., step_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., cells: _Optional[_Iterable[_Union[SparseVoxelCell, _Mapping]]] = ...) -> None: ...

class ProgressReport(_message.Message):
    __slots__ = ("source_role", "source_id", "session_id", "completed_time_ns", "completed_sequence_no", "finite_end_reached")
    SOURCE_ROLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQUENCE_NO_FIELD_NUMBER: _ClassVar[int]
    FINITE_END_REACHED_FIELD_NUMBER: _ClassVar[int]
    source_role: _common_pb2.ComponentRole
    source_id: str
    session_id: str
    completed_time_ns: int
    completed_sequence_no: int
    finite_end_reached: bool
    def __init__(self, source_role: _Optional[_Union[_common_pb2.ComponentRole, str]] = ..., source_id: _Optional[str] = ..., session_id: _Optional[str] = ..., completed_time_ns: _Optional[int] = ..., completed_sequence_no: _Optional[int] = ..., finite_end_reached: _Optional[bool] = ...) -> None: ...

class BusMessage(_message.Message):
    __slots__ = ("bus_sequence_no", "sent_wall_time_ns", "session_hello", "session_ack", "station_config", "video_packet", "detection_batch", "calibration_state", "ray_event", "diagnostic_event", "fusion_measurement", "track_event", "progress_report", "sparse_voxel_grid")
    BUS_SEQUENCE_NO_FIELD_NUMBER: _ClassVar[int]
    SENT_WALL_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    SESSION_HELLO_FIELD_NUMBER: _ClassVar[int]
    SESSION_ACK_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_FIELD_NUMBER: _ClassVar[int]
    VIDEO_PACKET_FIELD_NUMBER: _ClassVar[int]
    DETECTION_BATCH_FIELD_NUMBER: _ClassVar[int]
    CALIBRATION_STATE_FIELD_NUMBER: _ClassVar[int]
    RAY_EVENT_FIELD_NUMBER: _ClassVar[int]
    DIAGNOSTIC_EVENT_FIELD_NUMBER: _ClassVar[int]
    FUSION_MEASUREMENT_FIELD_NUMBER: _ClassVar[int]
    TRACK_EVENT_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_REPORT_FIELD_NUMBER: _ClassVar[int]
    SPARSE_VOXEL_GRID_FIELD_NUMBER: _ClassVar[int]
    bus_sequence_no: int
    sent_wall_time_ns: int
    session_hello: SessionHello
    session_ack: SessionAck
    station_config: StationConfig
    video_packet: FramedVideoPacket
    detection_batch: DetectionBatch
    calibration_state: CalibrationStateEvent
    ray_event: RayEvent
    diagnostic_event: DiagnosticEvent
    fusion_measurement: FusionMeasurement
    track_event: TrackEvent
    progress_report: ProgressReport
    sparse_voxel_grid: SparseVoxelGrid
    def __init__(self, bus_sequence_no: _Optional[int] = ..., sent_wall_time_ns: _Optional[int] = ..., session_hello: _Optional[_Union[SessionHello, _Mapping]] = ..., session_ack: _Optional[_Union[SessionAck, _Mapping]] = ..., station_config: _Optional[_Union[StationConfig, _Mapping]] = ..., video_packet: _Optional[_Union[FramedVideoPacket, _Mapping]] = ..., detection_batch: _Optional[_Union[DetectionBatch, _Mapping]] = ..., calibration_state: _Optional[_Union[CalibrationStateEvent, _Mapping]] = ..., ray_event: _Optional[_Union[RayEvent, _Mapping]] = ..., diagnostic_event: _Optional[_Union[DiagnosticEvent, _Mapping]] = ..., fusion_measurement: _Optional[_Union[FusionMeasurement, _Mapping]] = ..., track_event: _Optional[_Union[TrackEvent, _Mapping]] = ..., progress_report: _Optional[_Union[ProgressReport, _Mapping]] = ..., sparse_voxel_grid: _Optional[_Union[SparseVoxelGrid, _Mapping]] = ...) -> None: ...
