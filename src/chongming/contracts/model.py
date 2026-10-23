"""Online contracts: ENU metres, camera right/down/forward, exposure time in ns.

Immutable declarations only. No validation, geometry, state transitions or defaults
that fabricate a measurement. Configuration/solution references must be resolvable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

Vec2Px = tuple[float, float]
Vec3 = tuple[float, float, float]
Vec3M = Vec3
Matrix2x2 = tuple[tuple[float, float], tuple[float, float]]
Matrix3x3 = tuple[Vec3, Vec3, Vec3]
Rotation3x3 = Matrix3x3
Row6 = tuple[float, float, float, float, float, float]
Matrix6x6 = tuple[Row6, Row6, Row6, Row6, Row6, Row6]

StationId = NewType("StationId", str)
SessionId = NewType("SessionId", str)
FrameId = NewType("FrameId", str)
LocalTrackId = NewType("LocalTrackId", str)
TrackId = NewType("TrackId", str)  # center track ID, scoped to center session
MeasurementId = NewType("MeasurementId", str)
WorldFrameId = NewType("WorldFrameId", str)
ConfigDigest = NewType("ConfigDigest", str)
CalibrationSolutionId = NewType("CalibrationSolutionId", str)
EventId = tuple[StationId, SessionId, int]


class CameraRole(StrEnum):
    MAIN = "main"
    AUXILIARY = "auxiliary"


class PixelFormat(StrEnum):
    GRAY8 = "gray8"
    RGB8 = "rgb8"
    BGR8 = "bgr8"


class EventType(StrEnum):
    RAY = "ray"
    CALIBRATION_STATE = "calibration_state"
    DIAGNOSTIC = "diagnostic"


class ProcessingStatus(StrEnum):
    PROCESSED = "processed"
    FAILED = "failed"


class ScoreKind(StrEnum):
    RANKING = "ranking"
    CALIBRATED_PROBABILITY = "calibrated_probability"


class CalibrationHealth(StrEnum):
    INITIALIZING = "initializing"
    VALID = "valid"
    FROZEN = "frozen"
    STALE = "stale"
    RECOVERING = "recovering"
    INVALID = "invalid"


class CalibrationUsage(StrEnum):
    VALID = "valid"
    FROZEN = "frozen"
    STALE = "stale"


class FusionStatus(StrEnum):
    LOCATED = "located"
    GEOMETRY_DEGENERATE = "geometry_degenerate"
    ASSOCIATION_AMBIGUOUS = "association_ambiguous"
    INSUFFICIENT_STATIONS = "insufficient_stations"
    NO_CANDIDATE_IN_VOLUME = "no_candidate_in_volume"
    REJECTED_INPUT = "rejected_input"


class TrackLifecycle(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    COASTING = "coasting"
    DELETED = "deleted"


class ReferenceAnchorKind(StrEnum):
    INITIAL_IMAGE_FEATURES = "initial_image_features"
    SURVEYED_3D_LANDMARKS = "surveyed_3d_landmarks"


@dataclass(frozen=True, kw_only=True)
class ProcessingTime:
    """Accepted/arrival/health time; compare only after explicit clock mapping."""

    clock_domain_id: str
    time_ns: int


@dataclass(frozen=True, kw_only=True)
class TimeInterval:
    """Half-open interval [start, end); None end means no specified expiry."""

    clock_domain_id: str
    start_ns: int
    end_ns: int | None


@dataclass(frozen=True, kw_only=True)
class CaptureTime:
    """Event time is integer midpoint (start + end) // 2, never arrival time.

    rolling_shutter_model is 'none' for a known global shutter, 'unknown' for
    unmodelled readout, or a versioned model identifier. It cannot be omitted.
    """

    clock_domain_id: str
    exposure_start_ns: int
    exposure_end_ns: int
    clock_uncertainty_ns: int
    rolling_shutter_model: str


@dataclass(frozen=True, kw_only=True)
class EventEnvelope:
    """All outgoing event kinds share a monotonic sequence within one session."""

    schema_version: str
    event_type: EventType
    station_id: StationId
    station_session_id: SessionId
    sequence_no: int
    station_config_digest: ConfigDigest


@dataclass(frozen=True, kw_only=True)
class FrameRef:
    station_id: StationId
    station_session_id: SessionId
    camera_role: CameraRole
    frame_id: FrameId


@dataclass(frozen=True, kw_only=True)
class StationSessionBinding:
    """Fresh and distinct IDs per run/branch, never reused across restarts."""

    station_id: StationId
    image_session_id: SessionId
    virtual_session_id: SessionId


@dataclass(frozen=True, kw_only=True)
class ImagePlane:
    """Backend-neutral raw pixels, row stride in bytes; no ndarray dependency.

    The producer owns storage and must keep it unchanged while consumers hold it.
    """

    width_px: int
    height_px: int
    pixel_format: PixelFormat
    row_stride_bytes: int
    pixels: bytes | memoryview


@dataclass(frozen=True, kw_only=True)
class CameraIntrinsics:
    """Pixel origin is the top-left pixel centre (0, 0)."""

    image_width_px: int
    image_height_px: int
    matrix_px: Matrix3x3


@dataclass(frozen=True, kw_only=True)
class CameraDistortion:
    """Model ID specifies coefficient order; choose the backend in stage 3."""

    model: str
    coefficients: tuple[float, ...]


@dataclass(frozen=True, kw_only=True)
class CameraModel:
    intrinsics: CameraIntrinsics
    distortion: CameraDistortion


@dataclass(frozen=True, kw_only=True)
class CameraFrame:
    station_id: StationId
    station_session_id: SessionId
    camera_role: CameraRole
    frame_id: FrameId
    capture_time: CaptureTime
    image: ImagePlane
    camera_config_ref: ConfigDigest


@dataclass(frozen=True, kw_only=True)
class ScoreDefinition:
    definition_id: str
    kind: ScoreKind
    minimum: float | None
    maximum: float | None
    cross_station_comparison_group: str | None


@dataclass(frozen=True, kw_only=True)
class Observation2D:
    """Online observation; local track IDs are scoped to station+session."""

    main_frame_ref: FrameRef
    capture_time: CaptureTime
    centroid_px: Vec2Px
    centroid_covariance_px2: Matrix2x2 | None
    detection_score: float
    detection_score_definition: ScoreDefinition
    local_track_id: LocalTrackId | None
    track_quality: float | None
    track_quality_definition: ScoreDefinition | None


@dataclass(frozen=True, kw_only=True)
class DetectionBatch:
    """PROCESSED + empty observations is a completed negative frame, not failure."""

    main_frame_ref: FrameRef
    capture_time: CaptureTime
    processing_status: ProcessingStatus
    observations: tuple[Observation2D, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class GeodeticAnchor:
    anchor_id: str
    datum: str  # WGS84/CGCS2000 must remain distinct
    datum_epoch: str
    latitude_deg: float
    longitude_deg: float
    height_m: float
    height_reference: str  # ellipsoidal/normal; conversion is explicit


@dataclass(frozen=True, kw_only=True)
class RigidTransform:
    """T_A_from_B: p_A = R_A_from_B p_B + translation_A_m."""

    rotation_a_from_b: Rotation3x3
    translation_a_m: Vec3M


@dataclass(frozen=True, kw_only=True)
class InstallationStateRef:
    station_id: StationId
    config_digest: ConfigDigest
    state_version: str


@dataclass(frozen=True, kw_only=True)
class InstallationUncertainty:
    """Shared across rays; marginal covariances, no origin-angle cross term yet."""

    state_ref: InstallationStateRef
    position_covariance_enu_m2: Matrix3x3
    orientation_covariance_enu_rad2: Matrix3x3
    fixed_extrinsic_covariance_aux_rad2: Matrix3x3
    measurement_source: str


@dataclass(frozen=True, kw_only=True)
class ReferenceFeature:
    feature_id: str
    centroid_px: Vec2Px
    descriptor: bytes | None


@dataclass(frozen=True, kw_only=True)
class InitialImageReference:
    reference_id: str
    capture_time: CaptureTime
    image: ImagePlane | None
    features: tuple[ReferenceFeature, ...]


@dataclass(frozen=True, kw_only=True)
class SurveyedLandmark:
    landmark_id: str
    position_enu_m: Vec3M
    covariance_enu_m2: Matrix3x3
    visual_description: str


@dataclass(frozen=True, kw_only=True)
class SurveyedReference:
    reference_id: str
    world_frame_id: WorldFrameId
    landmarks: tuple[SurveyedLandmark, ...]


ReferenceAnchor = InitialImageReference | SurveyedReference


@dataclass(frozen=True, kw_only=True)
class StationConfig:
    """Online estimates, independent of scene-generation truth parameters."""

    station_id: StationId
    config_version: str
    config_digest: ConfigDigest
    world_frame_id: WorldFrameId
    geodetic_anchor: GeodeticAnchor
    main_camera_position_enu_m: Vec3M
    baseline_orientation_enu_from_main: Rotation3x3
    main_camera: CameraModel
    auxiliary_camera: CameraModel
    fixed_rotation_aux_from_main: Rotation3x3
    full_rigid_transform_aux_from_main: RigidTransform | None
    reference_anchor: ReferenceAnchor
    reference_scene_version: str
    installation_uncertainty: InstallationUncertainty


@dataclass(frozen=True, kw_only=True)
class CalibrationSolutionRef:
    station_id: StationId
    station_session_id: SessionId
    solution_id: CalibrationSolutionId


@dataclass(frozen=True, kw_only=True)
class CalibrationQuality:
    source: str
    matched_reference_count: int
    reprojection_rmse_px: float | None
    angular_residual_rad: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationSolution:
    """Immutable trusted solution; health-only changes keep this same reference.

    Corrections accept strictly increasing reference capture times. The output
    rotation is R_enu_from_main, not the inverse auxiliary image rotation.
    The rotation-only first version keeps the configured optical-centre position;
    a position correction requires an upgraded shared pose covariance contract.

    Virtual held solutions under deterministic camera jitter may declare a
    conservative marginal second-moment envelope in correction_covariance,
    identified by quality reason codes. This is not an exact Gaussian or joint
    temporal distribution; shared uncertainty cannot be averaged away per ray.
    """

    solution_ref: CalibrationSolutionRef
    station_config_digest: ConfigDigest
    world_frame_id: WorldFrameId
    reference_capture_time: CaptureTime
    accepted_time: ProcessingTime
    valid_interval: TimeInterval
    main_camera_position_enu_m: Vec3M
    rotation_enu_from_main: Rotation3x3
    correction_covariance_enu_rad2: Matrix3x3
    quality: CalibrationQuality


@dataclass(frozen=True, kw_only=True)
class CalibrationState:
    """Health snapshot, distinct from a solution and its validity interval."""

    station_id: StationId
    station_session_id: SessionId
    station_config_digest: ConfigDigest
    state_version: int
    health: CalibrationHealth
    solution_ref: CalibrationSolutionRef | None
    observed_at: ProcessingTime
    last_trusted_time: ProcessingTime | None
    usable_interval: TimeInterval | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationStateEvent:
    envelope: EventEnvelope
    state: CalibrationState
    solution: CalibrationSolution | None  # registration snapshot for a new solution


@dataclass(frozen=True, kw_only=True)
class CalibrationUpdate:
    solution: CalibrationSolution | None
    state_event: CalibrationStateEvent
    diagnostics: tuple[DiagnosticEvent, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationSnapshot:
    """Chosen as of MAIN exposure, never a newer solution available at processing.

    Both source exposure and accepted time must be <= main exposure midpoint
    after clock mapping; state.observed_at must also be <= exposure midpoint.
    During recovering, an old solution remains frozen/stale;
    no old solution means solution/usage are None and only diagnostics are legal.
    """

    main_capture_time: CaptureTime
    state: CalibrationState
    solution: CalibrationSolution | None
    usage: CalibrationUsage | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationUseEvidence:
    """Historical evidence in each ray; later health cannot overwrite this."""

    state_version: int
    last_trusted_time: ProcessingTime
    solution_accepted_time: ProcessingTime
    solution_reference_capture_time: CaptureTime
    solution_valid_interval: TimeInterval
    usable_interval: TimeInterval
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class RayUncertainty:
    """u, v, direction are orthonormal with u cross v = direction.

    Independent terms exclude installation/solution errors; shared references are
    resolved in registries. Initial covariance reports remain conditional on them.
    """

    model: str  # tangent_gaussian_v1
    tangent_u_enu_unit: Vec3
    tangent_v_enu_unit: Vec3
    independent_angular_covariance_rad2: Matrix2x2
    independent_origin_covariance_m2: Matrix3x3
    shared_installation_state_ref: InstallationStateRef
    shared_calibration_solution_ref: CalibrationSolutionRef


@dataclass(frozen=True, kw_only=True)
class RayEvent:
    envelope: EventEnvelope
    frame_id: FrameId
    local_track_id: LocalTrackId | None
    capture_time: CaptureTime
    world_frame_id: WorldFrameId
    calibration_solution_ref: CalibrationSolutionRef
    calibration_usage: CalibrationUsage
    calibration_evidence: CalibrationUseEvidence
    ray_origin_enu_m: Vec3M
    ray_direction_enu_unit: Vec3
    uncertainty: RayUncertainty
    detection_score: float
    detection_score_definition: ScoreDefinition
    track_quality: float | None
    track_quality_definition: ScoreDefinition | None
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class DiagnosticEvent:
    """Never supplies placeholder ray geometry."""

    envelope: EventEnvelope
    related_frame_ref: FrameRef | None
    capture_time: CaptureTime | None
    category: str
    reason_codes: tuple[str, ...]
    related_config_ref: ConfigDigest | None
    related_calibration_ref: CalibrationSolutionRef | None


StationEvent = RayEvent | DiagnosticEvent | CalibrationStateEvent


@dataclass(frozen=True, kw_only=True)
class ReceivedEvent:
    event: StationEvent
    arrival_time: ProcessingTime


@dataclass(frozen=True, kw_only=True)
class VoxelSearchSpec:
    world_frame_id: WorldFrameId
    min_enu_m: Vec3M
    max_enu_m: Vec3M
    step_enu_m: Vec3M
    forward_range_m: tuple[float, float] | None


@dataclass(frozen=True, kw_only=True)
class MeasurementRef:
    center_session_id: SessionId
    measurement_id: MeasurementId


@dataclass(frozen=True, kw_only=True)
class FusionMeasurement:
    center_session_id: SessionId
    measurement_id: MeasurementId
    clock_domain_id: str
    world_frame_id: WorldFrameId
    reference_time_ns: int
    support_exposure_span_ns: int
    status: FusionStatus
    position_enu_m: Vec3M | None
    conditional_position_covariance_enu_m2: Matrix3x3 | None
    supporting_ray_event_ids: tuple[EventId, ...]
    supporting_station_ids: tuple[StationId, ...]
    residuals_m: tuple[float, ...]
    geometry_condition: float | None
    minimum_intersection_angle_rad: float | None
    association_hypothesis_id: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class TrackEvent:
    """State order [E,N,U,vE,vN,vU]; covariance blocks m2, m2/s, m2/s2.

    Conditional covariance excludes shared installation/calibration error.
    """

    center_session_id: SessionId
    track_id: TrackId
    clock_domain_id: str
    world_frame_id: WorldFrameId
    event_time_ns: int
    lifecycle: TrackLifecycle
    position_enu_m: Vec3M | None
    velocity_enu_mps: Vec3 | None
    conditional_state_covariance: Matrix6x6 | None
    source_measurement_refs: tuple[MeasurementRef, ...]
    reason_codes: tuple[str, ...]
