"""Clean online step boundary. No generation parameters or correct answers."""

from dataclasses import dataclass

from chongming.contracts.device import Track2DState
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.model import (
    CalibrationSolution,
    CalibrationUpdate,
    DetectionBatch,
    ProcessingTime,
    ReceivedEvent,
    SessionId,
    StationConfig,
    StationEvent,
    VoxelSearchSpec,
)


@dataclass(frozen=True, kw_only=True)
class CenterRegistration:
    stations: tuple[StationConfig, ...]
    search: VoxelSearchSpec
    clock_domain_id: str
    center_session_id: str


@dataclass(frozen=True, kw_only=True)
class CenterInputStep:
    step_no: int
    time: ProcessingTime
    solutions: tuple[CalibrationSolution, ...]
    events: tuple[ReceivedEvent, ...]
    progress: tuple[StationEventTimeProgress, ...]
    stream_ended: bool


@dataclass(frozen=True, kw_only=True)
class DeviceStationRegistration:
    config: StationConfig
    station_session_id: SessionId


@dataclass(frozen=True, kw_only=True)
class DeviceRegistration:
    """Only online installation estimates and active image sessions."""

    stations: tuple[DeviceStationRegistration, ...]
    clock_domain_id: str
    run_id: str


@dataclass(frozen=True, kw_only=True)
class DeviceInputStep:
    """Tick after this sample's available frames, or once at finite end.

    Video disabled means no image callbacks, never a processed negative frame.
    """

    step_no: int
    time: ProcessingTime
    video_enabled: bool
    stream_ended: bool


@dataclass(frozen=True, kw_only=True)
class DeviceAlgorithmOutput:
    """Measured detections, tracker states and events remain distinct.

    An unimplemented plugin returns diagnostics/reason codes, never fabricated
    detections, calibration success or rays. A processed negative frame is an
    explicit PROCESSED DetectionBatch with no observations.
    """

    detections: tuple[DetectionBatch, ...] = ()
    tracks: tuple[Track2DState, ...] = ()
    calibration_updates: tuple[CalibrationUpdate, ...] = ()
    events: tuple[StationEvent, ...] = ()
    reason_codes: tuple[str, ...] = ()
