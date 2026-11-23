from chongming_proto.v1 import common_pb2 as _common_pb2
from chongming_proto.v1 import online_pb2 as _online_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OcclusionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OCCLUSION_STATUS_UNSPECIFIED: _ClassVar[OcclusionStatus]
    OCCLUSION_STATUS_VISIBLE: _ClassVar[OcclusionStatus]
    OCCLUSION_STATUS_OCCLUDED: _ClassVar[OcclusionStatus]
    OCCLUSION_STATUS_OUT_OF_FOV: _ClassVar[OcclusionStatus]
    OCCLUSION_STATUS_UNKNOWN: _ClassVar[OcclusionStatus]
OCCLUSION_STATUS_UNSPECIFIED: OcclusionStatus
OCCLUSION_STATUS_VISIBLE: OcclusionStatus
OCCLUSION_STATUS_OCCLUDED: OcclusionStatus
OCCLUSION_STATUS_OUT_OF_FOV: OcclusionStatus
OCCLUSION_STATUS_UNKNOWN: OcclusionStatus

class TargetTruth(_message.Message):
    __slots__ = ("target_id", "target_class", "position_enu_m", "velocity_enu_mps", "bounding_radius_m", "body_aabb_diagonal_m")
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_CLASS_FIELD_NUMBER: _ClassVar[int]
    POSITION_ENU_M_FIELD_NUMBER: _ClassVar[int]
    VELOCITY_ENU_MPS_FIELD_NUMBER: _ClassVar[int]
    BOUNDING_RADIUS_M_FIELD_NUMBER: _ClassVar[int]
    BODY_AABB_DIAGONAL_M_FIELD_NUMBER: _ClassVar[int]
    target_id: str
    target_class: str
    position_enu_m: _common_pb2.Vec3
    velocity_enu_mps: _common_pb2.Vec3
    bounding_radius_m: float
    body_aabb_diagonal_m: float
    def __init__(self, target_id: _Optional[str] = ..., target_class: _Optional[str] = ..., position_enu_m: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., velocity_enu_mps: _Optional[_Union[_common_pb2.Vec3, _Mapping]] = ..., bounding_radius_m: _Optional[float] = ..., body_aabb_diagonal_m: _Optional[float] = ...) -> None: ...

class StationTargetProjection(_message.Message):
    __slots__ = ("station_id", "target_id", "occlusion_status", "projected_centroid_px", "projected_diagonal_px", "range_m", "frame_id")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    OCCLUSION_STATUS_FIELD_NUMBER: _ClassVar[int]
    PROJECTED_CENTROID_PX_FIELD_NUMBER: _ClassVar[int]
    PROJECTED_DIAGONAL_PX_FIELD_NUMBER: _ClassVar[int]
    RANGE_M_FIELD_NUMBER: _ClassVar[int]
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    target_id: str
    occlusion_status: OcclusionStatus
    projected_centroid_px: _common_pb2.Vec2
    projected_diagonal_px: float
    range_m: float
    frame_id: str
    def __init__(self, station_id: _Optional[str] = ..., target_id: _Optional[str] = ..., occlusion_status: _Optional[_Union[OcclusionStatus, str]] = ..., projected_centroid_px: _Optional[_Union[_common_pb2.Vec2, _Mapping]] = ..., projected_diagonal_px: _Optional[float] = ..., range_m: _Optional[float] = ..., frame_id: _Optional[str] = ...) -> None: ...

class FrameIndexEntry(_message.Message):
    __slots__ = ("station_id", "station_session_id", "camera_role", "frame_index", "frame_id", "capture_time", "station_config_digest", "video_relpath", "width_px", "height_px")
    STATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATION_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CAMERA_ROLE_FIELD_NUMBER: _ClassVar[int]
    FRAME_INDEX_FIELD_NUMBER: _ClassVar[int]
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_FIELD_NUMBER: _ClassVar[int]
    STATION_CONFIG_DIGEST_FIELD_NUMBER: _ClassVar[int]
    VIDEO_RELPATH_FIELD_NUMBER: _ClassVar[int]
    WIDTH_PX_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_PX_FIELD_NUMBER: _ClassVar[int]
    station_id: str
    station_session_id: str
    camera_role: _common_pb2.CameraRole
    frame_index: int
    frame_id: str
    capture_time: _common_pb2.CaptureTime
    station_config_digest: str
    video_relpath: str
    width_px: int
    height_px: int
    def __init__(self, station_id: _Optional[str] = ..., station_session_id: _Optional[str] = ..., camera_role: _Optional[_Union[_common_pb2.CameraRole, str]] = ..., frame_index: _Optional[int] = ..., frame_id: _Optional[str] = ..., capture_time: _Optional[_Union[_common_pb2.CaptureTime, _Mapping]] = ..., station_config_digest: _Optional[str] = ..., video_relpath: _Optional[str] = ..., width_px: _Optional[int] = ..., height_px: _Optional[int] = ...) -> None: ...

class TruthFrame(_message.Message):
    __slots__ = ("world_frame_id", "experiment_time_ns", "step_index", "targets", "projections")
    WORLD_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENT_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    STEP_INDEX_FIELD_NUMBER: _ClassVar[int]
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    PROJECTIONS_FIELD_NUMBER: _ClassVar[int]
    world_frame_id: str
    experiment_time_ns: int
    step_index: int
    targets: _containers.RepeatedCompositeFieldContainer[TargetTruth]
    projections: _containers.RepeatedCompositeFieldContainer[StationTargetProjection]
    def __init__(self, world_frame_id: _Optional[str] = ..., experiment_time_ns: _Optional[int] = ..., step_index: _Optional[int] = ..., targets: _Optional[_Iterable[_Union[TargetTruth, _Mapping]]] = ..., projections: _Optional[_Iterable[_Union[StationTargetProjection, _Mapping]]] = ...) -> None: ...

class ScoreMetric(_message.Message):
    __slots__ = ("name", "value", "unit")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: float
    unit: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[float] = ..., unit: _Optional[str] = ...) -> None: ...

class ScoreEvent(_message.Message):
    __slots__ = ("run_id", "experiment_time_ns", "matched_count", "unmatched_count", "unevaluable_count", "mean_error", "rmse", "max_error", "error_unit", "extra_metrics")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    EXPERIMENT_TIME_NS_FIELD_NUMBER: _ClassVar[int]
    MATCHED_COUNT_FIELD_NUMBER: _ClassVar[int]
    UNMATCHED_COUNT_FIELD_NUMBER: _ClassVar[int]
    UNEVALUABLE_COUNT_FIELD_NUMBER: _ClassVar[int]
    MEAN_ERROR_FIELD_NUMBER: _ClassVar[int]
    RMSE_FIELD_NUMBER: _ClassVar[int]
    MAX_ERROR_FIELD_NUMBER: _ClassVar[int]
    ERROR_UNIT_FIELD_NUMBER: _ClassVar[int]
    EXTRA_METRICS_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    experiment_time_ns: int
    matched_count: int
    unmatched_count: int
    unevaluable_count: int
    mean_error: float
    rmse: float
    max_error: float
    error_unit: str
    extra_metrics: _containers.RepeatedCompositeFieldContainer[ScoreMetric]
    def __init__(self, run_id: _Optional[str] = ..., experiment_time_ns: _Optional[int] = ..., matched_count: _Optional[int] = ..., unmatched_count: _Optional[int] = ..., unevaluable_count: _Optional[int] = ..., mean_error: _Optional[float] = ..., rmse: _Optional[float] = ..., max_error: _Optional[float] = ..., error_unit: _Optional[str] = ..., extra_metrics: _Optional[_Iterable[_Union[ScoreMetric, _Mapping]]] = ...) -> None: ...

class TestFrameworkMessage(_message.Message):
    __slots__ = ("sequence_no", "truth_frame", "score_event", "bus_message")
    SEQUENCE_NO_FIELD_NUMBER: _ClassVar[int]
    TRUTH_FRAME_FIELD_NUMBER: _ClassVar[int]
    SCORE_EVENT_FIELD_NUMBER: _ClassVar[int]
    BUS_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    sequence_no: int
    truth_frame: TruthFrame
    score_event: ScoreEvent
    bus_message: _online_pb2.BusMessage
    def __init__(self, sequence_no: _Optional[int] = ..., truth_frame: _Optional[_Union[TruthFrame, _Mapping]] = ..., score_event: _Optional[_Union[ScoreEvent, _Mapping]] = ..., bus_message: _Optional[_Union[_online_pb2.BusMessage, _Mapping]] = ...) -> None: ...
