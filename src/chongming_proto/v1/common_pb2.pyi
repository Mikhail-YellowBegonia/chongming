from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CameraRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_ROLE_UNSPECIFIED: _ClassVar[CameraRole]
    CAMERA_ROLE_MAIN: _ClassVar[CameraRole]
    CAMERA_ROLE_AUXILIARY: _ClassVar[CameraRole]

class PixelFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PIXEL_FORMAT_UNSPECIFIED: _ClassVar[PixelFormat]
    PIXEL_FORMAT_GRAY8: _ClassVar[PixelFormat]
    PIXEL_FORMAT_RGB8: _ClassVar[PixelFormat]
    PIXEL_FORMAT_BGR8: _ClassVar[PixelFormat]
    PIXEL_FORMAT_YUV420P: _ClassVar[PixelFormat]
    PIXEL_FORMAT_YUV444P: _ClassVar[PixelFormat]

class VideoCodec(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VIDEO_CODEC_UNSPECIFIED: _ClassVar[VideoCodec]
    VIDEO_CODEC_RAW: _ClassVar[VideoCodec]
    VIDEO_CODEC_H264: _ClassVar[VideoCodec]

class EventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVENT_TYPE_UNSPECIFIED: _ClassVar[EventType]
    EVENT_TYPE_RAY: _ClassVar[EventType]
    EVENT_TYPE_CALIBRATION_STATE: _ClassVar[EventType]
    EVENT_TYPE_DIAGNOSTIC: _ClassVar[EventType]

class ProcessingStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROCESSING_STATUS_UNSPECIFIED: _ClassVar[ProcessingStatus]
    PROCESSING_STATUS_PROCESSED: _ClassVar[ProcessingStatus]
    PROCESSING_STATUS_FAILED: _ClassVar[ProcessingStatus]

class ScoreKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCORE_KIND_UNSPECIFIED: _ClassVar[ScoreKind]
    SCORE_KIND_RANKING: _ClassVar[ScoreKind]
    SCORE_KIND_CALIBRATED_PROBABILITY: _ClassVar[ScoreKind]

class CalibrationHealth(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CALIBRATION_HEALTH_UNSPECIFIED: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_INITIALIZING: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_VALID: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_FROZEN: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_STALE: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_RECOVERING: _ClassVar[CalibrationHealth]
    CALIBRATION_HEALTH_INVALID: _ClassVar[CalibrationHealth]

class CalibrationUsage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CALIBRATION_USAGE_UNSPECIFIED: _ClassVar[CalibrationUsage]
    CALIBRATION_USAGE_VALID: _ClassVar[CalibrationUsage]
    CALIBRATION_USAGE_FROZEN: _ClassVar[CalibrationUsage]
    CALIBRATION_USAGE_STALE: _ClassVar[CalibrationUsage]

class FusionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FUSION_STATUS_UNSPECIFIED: _ClassVar[FusionStatus]
    FUSION_STATUS_LOCATED: _ClassVar[FusionStatus]
    FUSION_STATUS_GEOMETRY_DEGENERATE: _ClassVar[FusionStatus]
    FUSION_STATUS_ASSOCIATION_AMBIGUOUS: _ClassVar[FusionStatus]
    FUSION_STATUS_INSUFFICIENT_STATIONS: _ClassVar[FusionStatus]
    FUSION_STATUS_NO_CANDIDATE_IN_VOLUME: _ClassVar[FusionStatus]
    FUSION_STATUS_REJECTED_INPUT: _ClassVar[FusionStatus]

class TrackLifecycle(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRACK_LIFECYCLE_UNSPECIFIED: _ClassVar[TrackLifecycle]
    TRACK_LIFECYCLE_TENTATIVE: _ClassVar[TrackLifecycle]
    TRACK_LIFECYCLE_CONFIRMED: _ClassVar[TrackLifecycle]
    TRACK_LIFECYCLE_COASTING: _ClassVar[TrackLifecycle]
    TRACK_LIFECYCLE_DELETED: _ClassVar[TrackLifecycle]

class ComponentRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COMPONENT_ROLE_UNSPECIFIED: _ClassVar[ComponentRole]
    COMPONENT_ROLE_PRODUCER: _ClassVar[ComponentRole]
    COMPONENT_ROLE_EDGE: _ClassVar[ComponentRole]
    COMPONENT_ROLE_CENTER: _ClassVar[ComponentRole]
    COMPONENT_ROLE_SCORER: _ClassVar[ComponentRole]
    COMPONENT_ROLE_VIEWER: _ClassVar[ComponentRole]
CAMERA_ROLE_UNSPECIFIED: CameraRole
CAMERA_ROLE_MAIN: CameraRole
CAMERA_ROLE_AUXILIARY: CameraRole
PIXEL_FORMAT_UNSPECIFIED: PixelFormat
PIXEL_FORMAT_GRAY8: PixelFormat
PIXEL_FORMAT_RGB8: PixelFormat
PIXEL_FORMAT_BGR8: PixelFormat
PIXEL_FORMAT_YUV420P: PixelFormat
PIXEL_FORMAT_YUV444P: PixelFormat
VIDEO_CODEC_UNSPECIFIED: VideoCodec
VIDEO_CODEC_RAW: VideoCodec
VIDEO_CODEC_H264: VideoCodec
EVENT_TYPE_UNSPECIFIED: EventType
EVENT_TYPE_RAY: EventType
EVENT_TYPE_CALIBRATION_STATE: EventType
EVENT_TYPE_DIAGNOSTIC: EventType
PROCESSING_STATUS_UNSPECIFIED: ProcessingStatus
PROCESSING_STATUS_PROCESSED: ProcessingStatus
PROCESSING_STATUS_FAILED: ProcessingStatus
SCORE_KIND_UNSPECIFIED: ScoreKind
SCORE_KIND_RANKING: ScoreKind
SCORE_KIND_CALIBRATED_PROBABILITY: ScoreKind
CALIBRATION_HEALTH_UNSPECIFIED: CalibrationHealth
CALIBRATION_HEALTH_INITIALIZING: CalibrationHealth
CALIBRATION_HEALTH_VALID: CalibrationHealth
CALIBRATION_HEALTH_FROZEN: CalibrationHealth
CALIBRATION_HEALTH_STALE: CalibrationHealth
CALIBRATION_HEALTH_RECOVERING: CalibrationHealth
CALIBRATION_HEALTH_INVALID: CalibrationHealth
CALIBRATION_USAGE_UNSPECIFIED: CalibrationUsage
CALIBRATION_USAGE_VALID: CalibrationUsage
CALIBRATION_USAGE_FROZEN: CalibrationUsage
CALIBRATION_USAGE_STALE: CalibrationUsage
FUSION_STATUS_UNSPECIFIED: FusionStatus
FUSION_STATUS_LOCATED: FusionStatus
FUSION_STATUS_GEOMETRY_DEGENERATE: FusionStatus
FUSION_STATUS_ASSOCIATION_AMBIGUOUS: FusionStatus
FUSION_STATUS_INSUFFICIENT_STATIONS: FusionStatus
FUSION_STATUS_NO_CANDIDATE_IN_VOLUME: FusionStatus
FUSION_STATUS_REJECTED_INPUT: FusionStatus
TRACK_LIFECYCLE_UNSPECIFIED: TrackLifecycle
TRACK_LIFECYCLE_TENTATIVE: TrackLifecycle
TRACK_LIFECYCLE_CONFIRMED: TrackLifecycle
TRACK_LIFECYCLE_COASTING: TrackLifecycle
TRACK_LIFECYCLE_DELETED: TrackLifecycle
COMPONENT_ROLE_UNSPECIFIED: ComponentRole
COMPONENT_ROLE_PRODUCER: ComponentRole
COMPONENT_ROLE_EDGE: ComponentRole
COMPONENT_ROLE_CENTER: ComponentRole
COMPONENT_ROLE_SCORER: ComponentRole
COMPONENT_ROLE_VIEWER: ComponentRole

class Vec2(_message.Message):
    __slots__ = ("x", "y")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    x: float
    y: float
    def __init__(self, x: _Optional[float] = ..., y: _Optional[float] = ...) -> None: ...

class Vec3(_message.Message):
    __slots__ = ("x", "y", "z")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    Z_FIELD_NUMBER: _ClassVar[int]
    x: float
    y: float
    z: float
    def __init__(self, x: _Optional[float] = ..., y: _Optional[float] = ..., z: _Optional[float] = ...) -> None: ...

class Mat2(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, values: _Optional[_Iterable[float]] = ...) -> None: ...

class Mat3(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, values: _Optional[_Iterable[float]] = ...) -> None: ...

class Mat6(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, values: _Optional[_Iterable[float]] = ...) -> None: ...

class ProcessingTime(_message.Message):
    __slots__ = ("clock_domain_id", "time_ns")
    CLOCK_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_NS_FIELD_NUMBER: _ClassVar[int]
    clock_domain_id: str
    time_ns: int
    def __init__(self, clock_domain_id: _Optional[str] = ..., time_ns: _Optional[int] = ...) -> None: ...

class TimeInterval(_message.Message):
    __slots__ = ("clock_domain_id", "start_ns", "end_ns")
    CLOCK_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    START_NS_FIELD_NUMBER: _ClassVar[int]
    END_NS_FIELD_NUMBER: _ClassVar[int]
    clock_domain_id: str
    start_ns: int
    end_ns: int
    def __init__(self, clock_domain_id: _Optional[str] = ..., start_ns: _Optional[int] = ..., end_ns: _Optional[int] = ...) -> None: ...

class CaptureTime(_message.Message):
    __slots__ = ("clock_domain_id", "exposure_start_ns", "exposure_end_ns", "clock_uncertainty_ns", "rolling_shutter_model")
    CLOCK_DOMAIN_ID_FIELD_NUMBER: _ClassVar[int]
    EXPOSURE_START_NS_FIELD_NUMBER: _ClassVar[int]
    EXPOSURE_END_NS_FIELD_NUMBER: _ClassVar[int]
    CLOCK_UNCERTAINTY_NS_FIELD_NUMBER: _ClassVar[int]
    ROLLING_SHUTTER_MODEL_FIELD_NUMBER: _ClassVar[int]
    clock_domain_id: str
    exposure_start_ns: int
    exposure_end_ns: int
    clock_uncertainty_ns: int
    rolling_shutter_model: str
    def __init__(self, clock_domain_id: _Optional[str] = ..., exposure_start_ns: _Optional[int] = ..., exposure_end_ns: _Optional[int] = ..., clock_uncertainty_ns: _Optional[int] = ..., rolling_shutter_model: _Optional[str] = ...) -> None: ...

class EventId(_message.Message):
    __slots__ = ("station_id", "station_session_id", "sequence_no")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NO_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    sequence_no: int
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., sequence_no: _Optional[int] = ...) -> None: ...

class EventEnvelope(_message.Message):
    __slots__ = ("schema_version", "event_type", "station_id", "station_session_id", "sequence_no", "station_config_digest")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NO_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    schema_version: str
    event_type: EventType
    station_id: str
    station_session_id: str
    sequence_no: int
    station_config_digest: str
    def __init__(self, schema_version: _Optional[str] = ..., event_type: _Optional[_Union[EventType, str]] = ..., station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., sequence_no: _Optional[int] = ..., station_config_digest: _Optional[str] = ...) -> None: ...

class FrameRef(_message.Message):
    __slots__ = ("station_id", "station_session_id", "camera_role", "frame_id")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CAMERA_ROLE_FIELD_NUMBER: _ClassVar[int]
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    camera_role: CameraRole
    frame_id: str
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., camera_role: _Optional[_Union[CameraRole, str]] = ..., frame_id: _Optional[str] = ...) -> None: ...

class ScoreDefinition(_message.Message):
    __slots__ = ("definition_id", "kind", "minimum", "maximum", "cross_station_comparison_group")
    DEFINITION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MINIMUM_FIELD_NUMBER: _ClassVar[int]
    MAXIMUM_FIELD_NUMBER: _ClassVar[int]
    CROSS_STATION_COMPARISON_GROUP_FIELD_NUMBER: _ClassVar[int]
    definition_id: str
    kind: ScoreKind
    minimum: float
    maximum: float
    cross_station_comparison_group: str
    def __init__(self, definition_id: _Optional[str] = ..., kind: _Optional[_Union[ScoreKind, str]] = ..., minimum: _Optional[float] = ..., maximum: _Optional[float] = ..., cross_station_comparison_group: _Optional[str] = ...) -> None: ...
