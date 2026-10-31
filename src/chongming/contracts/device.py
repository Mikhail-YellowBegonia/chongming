"""Device line records and deferred policies; no implementation or truth inputs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.model import (
    CalibrationQuality,
    CaptureTime,
    DetectionBatch,
    FrameRef,
    LocalTrackId,
    Matrix2x2,
    Matrix3x3,
    Observation2D,
    ProcessingStatus,
    ProcessingTime,
    Rotation3x3,
    ScoreDefinition,
    StationEvent,
    Vec2Px,
)


@dataclass(frozen=True, kw_only=True)
class DetectionPolicy:
    algorithm_id: str
    score_definition: ScoreDefinition
    minimum_component_area_px2: float
    maximum_component_area_px2: float | None
    # None means unconfigured; MOG2 execution requires every background parameter.
    background_warmup_frame_count: int | None = None
    background_history_frame_count: int | None = None
    background_variance_threshold: float | None = None
    background_learning_rate: float | None = None
    background_detect_shadows: bool | None = None


@dataclass(frozen=True, kw_only=True)
class Tracking2DPolicy:
    algorithm_id: str
    association_gate_px: float
    confirmation_frame_count: int
    maximum_missing_duration_ns: int
    quality_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class Candidate2D:
    centroid_px: Vec2Px
    centroid_covariance_px2: Matrix2x2 | None
    detection_score: float
    detection_score_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class Candidate2DBatch:
    main_frame_ref: FrameRef
    capture_time: CaptureTime
    processing_status: ProcessingStatus
    candidates: tuple[Candidate2D, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class Track2DState:
    """Local lifecycle; no identity recovery after leaving and returning promised."""

    main_frame_ref: FrameRef
    local_track_id: LocalTrackId
    capture_time: CaptureTime
    centroid_px: Vec2Px
    velocity_pxps: Vec2Px | None
    lifecycle: str
    track_quality: float
    track_quality_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class Tracking2DResult:
    """Observations use actual candidates from this frame only.

    Coasting/predicted positions belong to tracks, never to observations. An empty
    candidate batch cannot manufacture a detection or a downstream RayEvent.
    """

    observations: tuple[Observation2D, ...]
    tracks: tuple[Track2DState, ...]


@dataclass(frozen=True, kw_only=True)
class MainDetectionResult:
    """Keep measured detections and tracker state distinct, including negative frames."""

    detections: DetectionBatch
    tracks: tuple[Track2DState, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationPolicy:
    update_period_ns: int
    stale_after_ns: int
    recovery_success_count: int
    minimum_reference_count: int
    maximum_reprojection_rmse_px: float
    maximum_rotation_only_parallax_px: float | None
    estimator_id: str


@dataclass(frozen=True, kw_only=True)
class ReferenceObservation:
    reference_id: str
    centroid_px: Vec2Px
    centroid_covariance_px2: Matrix2x2 | None
    match_score: float
    match_score_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class ReferenceObservationBatch:
    auxiliary_frame_ref: FrameRef
    capture_time: CaptureTime
    processing_status: ProcessingStatus
    observations: tuple[ReferenceObservation, ...]
    reason_codes: tuple[str, ...]


class CalibrationAttemptStatus(StrEnum):
    ESTIMATED = "estimated"
    REFERENCE_UNAVAILABLE = "reference_unavailable"
    GEOMETRY_INVALID = "geometry_invalid"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class CalibrationEstimate:
    """An unaccepted proposal; only calibration state management trusts a new ID."""

    reference_capture_time: CaptureTime
    rotation_enu_from_main: Rotation3x3
    correction_covariance_enu_rad2: Matrix3x3
    quality: CalibrationQuality


@dataclass(frozen=True, kw_only=True)
class CalibrationAttempt:
    status: CalibrationAttemptStatus
    estimate: CalibrationEstimate | None
    reference_frame_ref: FrameRef
    reference_capture_time: CaptureTime
    completed_at: ProcessingTime
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class RayGenerationPolicy:
    uncertainty_model_id: str
    fallback_centroid_covariance_px2: Matrix2x2 | None
    unmodelled_high_frequency_jitter_covariance_rad2: Matrix2x2 | None
    unmodelled_relative_flex_covariance_rad2: Matrix2x2 | None
    # None is unknown, never zero. Shared installation/solution errors stay separate.
    # Jitter/flex terms are residual experiment models, not automatically independent.
    # Slow shared flex is scanned separately; repeated frames cannot shrink it away.
    # Nonzero residuals require a declared mutual/pixel-independence model basis.
    residual_independence_basis: str | None = None


@dataclass(frozen=True, kw_only=True)
class DevicePolicy:
    detection: DetectionPolicy
    tracking: Tracking2DPolicy
    calibration: CalibrationPolicy
    rays: RayGenerationPolicy


@dataclass(frozen=True, kw_only=True)
class DeviceFrameResult:
    completed_at: ProcessingTime
    detections: DetectionBatch
    tracks: tuple[Track2DState, ...]
    events: tuple[StationEvent, ...]
    progress: StationEventTimeProgress
    # Report progress after all events for that frame are emitted, even with no rays.
