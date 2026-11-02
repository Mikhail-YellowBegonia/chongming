"""Center pipeline records; online inputs never contain scenario truth.

All thresholds are supplied by experiment configuration. Frozen records describe
sealed evidence: late delivery cannot mutate a window or an already published
measurement. Installation and calibration shared errors remain conditional inputs,
not additional independent votes or an asserted absolute covariance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chongming.contracts.model import (
    CalibrationSolution,
    CalibrationSolutionRef,
    CalibrationUsage,
    CaptureTime,
    ConfigDigest,
    EventId,
    FusionMeasurement,
    InstallationStateRef,
    Matrix3x3,
    Matrix6x6,
    ProcessingTime,
    RayEvent,
    ReceivedEvent,
    ScoreDefinition,
    SessionId,
    StationConfig,
    StationId,
    TrackEvent,
    TrackId,
    Vec3,
    VoxelSearchSpec,
    WorldFrameId,
)


class RegistryWriteStatus(StrEnum):
    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class AdmissionDecision(StrEnum):
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class AdmissionReason(StrEnum):
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    EVENT_TYPE_MISMATCH = "event_type_mismatch"
    CONFIGURATION_UNKNOWN = "configuration_unknown"
    CONFIGURATION_DIGEST_MISMATCH = "configuration_digest_mismatch"
    CALIBRATION_SOLUTION_UNKNOWN = "calibration_solution_unknown"
    CALIBRATION_SOLUTION_SCOPE_MISMATCH = "calibration_solution_scope_mismatch"
    CALIBRATION_CONFIGURATION_MISMATCH = "calibration_configuration_mismatch"
    CALIBRATION_EVIDENCE_MISMATCH = "calibration_evidence_mismatch"
    CALIBRATION_STATE_CONFLICT = "calibration_state_conflict"
    CALIBRATION_EXPOSURE_OUTSIDE_VALIDITY = "calibration_exposure_outside_validity"
    CALIBRATION_USAGE_QUARANTINED = "calibration_usage_quarantined"
    CALIBRATION_USAGE_REJECTED = "calibration_usage_rejected"
    WORLD_FRAME_MISMATCH = "world_frame_mismatch"
    CLOCK_DOMAIN_MISMATCH = "clock_domain_mismatch"
    EXPOSURE_INTERVAL_INVALID = "exposure_interval_invalid"
    NONFINITE_RAY = "nonfinite_ray"
    NONUNIT_DIRECTION = "nonunit_direction"
    INVALID_TANGENT_BASIS = "invalid_tangent_basis"
    INVALID_UNCERTAINTY = "invalid_uncertainty"
    SHARED_ERROR_REFERENCE_MISMATCH = "shared_error_reference_mismatch"
    INVALID_SEQUENCE = "invalid_sequence"
    EVENT_ID_PAYLOAD_CONFLICT = "event_id_payload_conflict"
    EXACT_DUPLICATE_DELIVERY = "exact_duplicate_delivery"


class CalibrationAction(StrEnum):
    ACCEPT = "accept"
    QUARANTINE = "quarantine"
    REJECT = "reject"


class StationStreamHealth(StrEnum):
    AWAITING_FIRST_EVENT = "awaiting_first_event"
    ACTIVE = "active"
    IDLE = "idle"
    OFFLINE = "offline"
    RECOVERING = "recovering"
    ENDED = "ended"


class WatermarkStrategy(StrEnum):
    ACTIVE_STATION_MINIMUM = "active_station_minimum"
    EVENT_CLOCK_DEADLINE = "event_clock_deadline"


class EmptyActiveStationPolicy(StrEnum):
    HOLD = "hold"
    ADVANCE_WITH_MAPPED_EVENT_CLOCK = "advance_with_mapped_event_clock"


class WindowDisposition(StrEnum):
    BUFFERED = "buffered"
    PROGRESS_RECORDED = "progress_recorded"
    LATE_AFTER_SEAL = "late_after_seal"
    ALREADY_CONSUMED = "already_consumed"
    NOT_A_FUSION_OBSERVATION = "not_a_fusion_observation"


class WindowSealReason(StrEnum):
    WATERMARK_PASSED_END = "watermark_passed_end"
    EVENT_CLOCK_DEADLINE = "event_clock_deadline"
    STREAMS_ENDED = "streams_ended"


class CandidateMethod(StrEnum):
    COARSE_VOXEL = "coarse_voxel"
    DIRECT_GEOMETRY = "direct_geometry"


class CandidateOrigin(StrEnum):
    UNGUIDED_SEARCH = "unguided_search"
    TRACK_PREDICTION_GATE = "track_prediction_gate"


class CandidateBatchStatus(StrEnum):
    CANDIDATES_PROPOSED = "candidates_proposed"
    INSUFFICIENT_STATIONS = "insufficient_stations"
    NO_CANDIDATE_IN_VOLUME = "no_candidate_in_volume"


class AssociationConflictReason(StrEnum):
    RAY_ASSIGNED_TO_MULTIPLE_HYPOTHESES = "ray_assigned_to_multiple_hypotheses"
    MULTIPLE_SUPPORT_RAYS_FROM_ONE_STATION = "multiple_support_rays_from_one_station"
    COMPETING_TARGET_HYPOTHESES = "competing_target_hypotheses"
    INSUFFICIENT_INDEPENDENT_STATIONS = "insufficient_independent_stations"
    TEMPORAL_GATE_FAILED = "temporal_gate_failed"
    MOTION_ALIGNMENT_UNAVAILABLE = "motion_alignment_unavailable"
    PREDICTION_GATE_FAILED = "prediction_gate_failed"
    SPATIAL_GATE_FAILED = "spatial_gate_failed"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    SOLVER_FAILURE = "solver_failure"
    SOLVER_NOT_CONVERGED = "solver_not_converged"
    INCONSISTENT_EXACT_CONSTRAINTS = "inconsistent_exact_constraints"
    CANDIDATE_SEARCH_TRUNCATED = "candidate_search_truncated"
    ASSOCIATION_SEARCH_TRUNCATED = "association_search_truncated"


class AssociationBatchStatus(StrEnum):
    ASSOCIATIONS_SELECTED = "associations_selected"
    PARTIALLY_ASSOCIATED = "partially_associated"
    NO_VALID_ASSOCIATION = "no_valid_association"
    ASSOCIATION_AMBIGUOUS = "association_ambiguous"
    INSUFFICIENT_STATIONS = "insufficient_stations"
    NO_CANDIDATE_IN_VOLUME = "no_candidate_in_volume"


class MotionAlignmentModel(StrEnum):
    TRACK_PREDICTION = "track_prediction"
    CONSERVATIVE_SPEED_BOUND = "conservative_speed_bound"
    NARROW_WINDOW_WITHOUT_PRIOR = "narrow_window_without_prior"


class GeometryReason(StrEnum):
    INSUFFICIENT_STATIONS = "insufficient_stations"
    NEAR_PARALLEL_RAYS = "near_parallel_rays"
    POOR_CONDITION = "poor_condition"
    EXCESSIVE_RESIDUAL = "excessive_residual"
    REFERENCE_TIME_ALIGNMENT_UNAVAILABLE = "reference_time_alignment_unavailable"
    SOLVER_FAILURE = "solver_failure"
    NONFORWARD_SOLUTION = "nonforward_solution"
    INVALID_SUPPORT_GROUP = "invalid_support_group"
    UNSUPPORTED_UNCERTAINTY_MODEL = "unsupported_uncertainty_model"
    CALIBRATION_POLICY_EXCLUDED = "calibration_policy_excluded"
    INCONSISTENT_EXACT_CONSTRAINTS = "inconsistent_exact_constraints"
    SOLVER_NOT_CONVERGED = "solver_not_converged"


@dataclass(frozen=True, kw_only=True)
class StationConfigRef:
    station_id: StationId
    config_digest: ConfigDigest
    world_frame_id: WorldFrameId


@dataclass(frozen=True, kw_only=True)
class RegistryWriteResult:
    status: RegistryWriteStatus
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CalibrationUsageRule:
    """Scale independent precision; zero/non-ACCEPT excludes geometry evidence.

    No scale turns shared pose errors into independent noise. Exact independent
    constraints stay exact under a positive scale, including frozen/stale usage.
    """

    usage: CalibrationUsage
    action: CalibrationAction
    geometry_weight_scale: float


@dataclass(frozen=True, kw_only=True)
class AdmissionPolicy:
    accepted_schema_versions: tuple[str, ...]
    world_frame_id: WorldFrameId
    capture_clock_domain_id: str
    direction_unit_tolerance: float
    tangent_basis_orthogonality_tolerance: float
    calibration_usage_rules: tuple[CalibrationUsageRule, ...]


@dataclass(frozen=True, kw_only=True)
class EventClockMappingSpec:
    """Manifest-owned time mapping; a mapping is never inferred from ray arrival."""

    processing_clock_domain_id: str
    capture_clock_domain_id: str
    scale_numerator: int
    scale_denominator: int
    offset_ns: int
    mapping_uncertainty_ns: int


@dataclass(frozen=True, kw_only=True)
class EventClockReading:
    clock_domain_id: str
    event_time_ns: int
    mapping_uncertainty_ns: int


@dataclass(frozen=True, kw_only=True)
class EventWindowPolicy:
    """Windows are [origin + k*width, origin + (k+1)*width), then immutable.

    Exposure midpoint determines membership. Processing time only schedules health
    checks/deadlines through the explicit manifest clock mapping. A processed empty
    detection batch can advance a station through StationEventTimeProgress.

    Once every registered stream has ended, STREAMS_ENDED seals remaining windows
    through the window containing the latest completed exposure or buffered ray,
    regardless of no_active_station_policy. With neither, create no window. The
    transport drain deadline cannot extend this finite experiment-time horizon.
    """

    capture_clock_domain_id: str
    processing_clock_domain_id: str
    origin_ns: int
    width_ns: int
    allowed_lateness_ns: int
    awaiting_first_event_timeout_ns: int
    idle_after_ns: int
    offline_after_ns: int
    exclude_idle_stations: bool
    exclude_offline_stations: bool
    watermark_strategy: WatermarkStrategy
    no_active_station_policy: EmptyActiveStationPolicy


@dataclass(frozen=True, kw_only=True)
class CandidatePolicy:
    """A conservative k-sigma tube, not a probability of target occupancy.

    Zero sigma disables expansion. The per-ray uncertainty budget bounds retained
    cells and central grid-plane traversal. Exhaustion marks the search incomplete;
    downstream association cannot assert uniqueness.
    """

    search: VoxelSearchSpec
    primary_method: CandidateMethod
    minimum_independent_stations: int
    minimum_candidate_score: float
    maximum_candidates: int | None
    uncertainty_sigma: float
    maximum_uncertainty_voxels_per_ray: int


@dataclass(frozen=True, kw_only=True)
class AssociationPolicy:
    """Rank disjoint groups by station pairs, prior matches, then normalized error.

    The margin applies to the summed squared residual/prediction gate ratios.
    The hypothesis budget bounds examined ray combinations and expanded prior
    choices; the node budget applies independently to each conflict component.
    """

    minimum_independent_stations: int
    temporal_gate_ns: int
    spatial_gate_m: float
    ambiguity_score_margin: float
    prediction_spatial_gate_m: float
    maximum_hypotheses: int
    maximum_search_nodes: int


@dataclass(frozen=True, kw_only=True)
class GeometryPolicy:
    """At least two independent stations; numerical quality limits remain supplied.

    The angle limit applies to the strongest acute pair; the normal-matrix
    condition and maximum per-ray residual assess the complete support group.
    maximum_unaligned_span_ns bounds exposure midpoint differences, not arrival
    jitter or exposure duration. TRACK_PREDICTION keeps this narrow unguided path
    for new targets; an unknown speed never becomes a zero-velocity assumption.
    Huber scale is dimensionless in whitened 2D perpendicular residual space.
    Both losses iterate depth-dependent angular covariance to the supplied metric
    tolerance and iteration budget. The metric residual limit still applies to
    every supporting ray, including robustly downweighted observations.
    """

    minimum_independent_stations: int
    minimum_intersection_angle_rad: float
    maximum_condition_number: float
    maximum_residual_m: float
    robust_loss_definition: str
    robust_loss_scale_sigma: float
    maximum_solver_iterations: int
    convergence_tolerance_m: float
    motion_alignment_model: MotionAlignmentModel
    conservative_speed_bound_mps: float | None
    maximum_unaligned_span_ns: int


@dataclass(frozen=True, kw_only=True)
class TrackingPolicy:
    """Two exposure times initialize velocity; confirmation count is at least two.

    Acceleration covariance is m2/s4 for a constant acceleration per prediction
    interval, not a continuous-time spectral density. No noise floor is implied.
    Uninitialized seeds use a Euclidean position gate, not a guessed velocity
    prior. Assignment costs are squared gate ratios; the margin applies to their
    sum. The node budget applies independently to each conflict component.
    """

    confirmation_measurement_count: int
    confirmation_max_gap_ns: int
    coast_after_ns: int
    delete_after_ns: int
    association_gate_mahalanobis_squared: float
    initialization_spatial_gate_m: float
    association_ambiguity_margin: float
    maximum_assignment_search_nodes: int
    process_acceleration_covariance_enu_m2ps4: Matrix3x3


@dataclass(frozen=True, kw_only=True)
class FusionPolicy:
    admission: AdmissionPolicy
    windows: EventWindowPolicy
    candidates: CandidatePolicy
    association: AssociationPolicy
    geometry: GeometryPolicy
    tracking: TrackingPolicy


@dataclass(frozen=True, kw_only=True)
class AdmissionRecord:
    """Deduplicate the complete EventId and payload, never sequence number alone.

    A different payload under an existing ID is a conflict. Quarantine retains the
    original arrival and event for reconsideration after registry prerequisites
    arrive; it is not an accepted geometry observation.
    """

    event_id: EventId
    received: ReceivedEvent
    decision: AdmissionDecision
    reasons: tuple[AdmissionReason, ...]
    checked_at: ProcessingTime
    station_config_ref: StationConfigRef | None
    calibration_solution_ref: CalibrationSolutionRef | None


@dataclass(frozen=True, kw_only=True)
class AdmittedRay:
    event: RayEvent
    arrival_time: ProcessingTime
    config: StationConfig
    calibration_solution: CalibrationSolution


@dataclass(frozen=True, kw_only=True)
class AdmissionOutcome:
    """Only an ACCEPTED record may carry admitted_ray.

    Accepted diagnostics and calibration health transitions are auditable control
    events, not fabricated rays. A later health transition does not replace the
    RayEvent.calibration_usage or referenced solution at its exposure time.
    """

    record: AdmissionRecord
    admitted_ray: AdmittedRay | None


@dataclass(frozen=True, kw_only=True)
class StationEventTimeProgress:
    """Completed device processing, including a genuinely empty detection batch.

    A capture time is required for ordinary progress. Only stream_ended may carry
    None, for a stream that stopped without a completed frame; never invent an
    exposure to report startup failure or zero samples. Stream end retires this
    session from future watermark participation after its transport is accounted for.
    """

    station_id: StationId
    station_session_id: SessionId
    station_config_digest: ConfigDigest
    completed_capture_time: CaptureTime | None
    received_at: ProcessingTime
    stream_ended: bool


@dataclass(frozen=True, kw_only=True)
class StationProgress:
    """Retain maximum exposure when ENDED; None is valid for zero-frame streams.

    RECOVERING means fresh delivery is still behind the first open window. Such
    a stream rejoins the watermark only after supplying current usable progress.
    """

    station_id: StationId
    station_session_id: SessionId | None
    maximum_seen_exposure_midpoint_ns: int | None
    last_received_at: ProcessingTime | None
    health: StationStreamHealth
    participates_in_watermark: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class WatermarkSnapshot:
    capture_clock_domain_id: str
    event_time_ns: int | None
    evaluated_at: ProcessingTime
    station_progress: tuple[StationProgress, ...]


@dataclass(frozen=True, kw_only=True)
class WindowKey:
    center_session_id: SessionId
    capture_clock_domain_id: str
    start_ns: int
    end_ns: int


@dataclass(frozen=True, kw_only=True)
class WindowReceipt:
    event_id: EventId | None
    station_id: StationId
    disposition: WindowDisposition
    window: WindowKey | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class SealedObservationWindow:
    """Each ray belongs to at most one sealed window; no revision exists in v1.

    Empty windows are sealed too so consumed event-time progress can drive track
    coasting/deletion without any measurement. Their support_exposure_span_ns is 0.
    """

    key: WindowKey
    reference_time_ns: int
    support_exposure_span_ns: int
    rays: tuple[AdmittedRay, ...]
    watermark_at_seal: WatermarkSnapshot
    sealed_at: ProcessingTime
    seal_reason: WindowSealReason


@dataclass(frozen=True, kw_only=True)
class WindowUpdate:
    receipts: tuple[WindowReceipt, ...]
    sealed_windows: tuple[SealedObservationWindow, ...]
    watermark: WatermarkSnapshot


@dataclass(frozen=True, kw_only=True)
class IndependentStationSupport:
    """One independent station vote, even if it offers several alternative rays."""

    station_id: StationId
    alternative_ray_event_ids: tuple[EventId, ...]


@dataclass(frozen=True, kw_only=True)
class SpatialCandidate:
    candidate_id: str
    window: WindowKey
    method: CandidateMethod
    origin: CandidateOrigin
    prior_track_id: TrackId | None
    coarse_position_enu_m: Vec3 | None
    region_min_enu_m: Vec3 | None
    region_max_enu_m: Vec3 | None
    independent_station_support: tuple[IndependentStationSupport, ...]
    score: float
    score_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class CandidateBatch:
    """No candidate means only no supported hypothesis in the configured volume."""

    window: WindowKey
    method: CandidateMethod
    search: VoxelSearchSpec
    status: CandidateBatchStatus
    candidates: tuple[SpatialCandidate, ...]
    examined_ray_event_ids: tuple[EventId, ...]
    unguided_search_performed: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class TrackPrediction:
    """A gating prior, valid before valid_until_ns, never a current station vote.

    Read-only extrapolation may be requested beyond validity; such a prediction
    cannot guide candidates or alignment in a consumed window.
    """

    center_session_id: SessionId
    track_id: TrackId
    clock_domain_id: str
    world_frame_id: WorldFrameId
    reference_time_ns: int
    valid_until_ns: int
    position_enu_m: Vec3
    velocity_enu_mps: Vec3
    conditional_state_covariance_enu: Matrix6x6 | None


@dataclass(frozen=True, kw_only=True)
class AssociatedRayGroup:
    """At most one support ray per station for this target in this window.

    Exclusive association uses an EventId at most once across selected groups.
    Exhaustive post-intersection hypotheses may overlap until spatial suppression. Group IDs
    are scoped by window (which includes the center session); truth IDs are absent.
    Without a motion prior, reference time is the midpoint of the earliest/latest
    supporting exposure midpoints, not an assumed position at the window center.
    Velocity-aligned groups use the earliest supporting midpoint, avoiding an
    extrapolated measurement later than all current evidence.
    """

    hypothesis_id: str
    candidate_id: str
    window: WindowKey
    reference_time_ns: int
    support_exposure_span_ns: int
    supporting_ray_event_ids: tuple[EventId, ...]
    supporting_station_ids: tuple[StationId, ...]
    prior_track_id: TrackId | None
    motion_alignment_model: MotionAlignmentModel
    score: float
    score_definition: ScoreDefinition


@dataclass(frozen=True, kw_only=True)
class AssociationConflict:
    window: WindowKey
    competing_hypothesis_ids: tuple[str, ...]
    ray_event_ids: tuple[EventId, ...]
    reasons: tuple[AssociationConflictReason, ...]
    resolved: bool
    selected_hypothesis_id: str | None


@dataclass(frozen=True, kw_only=True)
class AssociationBatch:
    window: WindowKey
    selected_groups: tuple[AssociatedRayGroup, ...]
    conflicts: tuple[AssociationConflict, ...]
    unassigned_ray_event_ids: tuple[EventId, ...]
    status: AssociationBatchStatus


@dataclass(frozen=True, kw_only=True)
class RayGeometryWeight:
    """Independent information multiplier, never another station vote."""

    event_id: EventId
    weight_scale: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class GeometryResult:
    """Degenerate groups retain references and reasons without an invented position.

    Normalized residuals use independent covariance after the usage penalty.
    None denotes a violated zero-variance constraint, not a finite residual score.
    Robust weights inflate the local residual model used for conditional covariance;
    they do not establish calibrated robust-estimator or absolute-pose coverage.
    """

    window: WindowKey
    hypothesis_id: str
    measurement: FusionMeasurement
    reasons: tuple[GeometryReason, ...]
    motion_alignment_model: MotionAlignmentModel
    shared_installation_state_refs: tuple[InstallationStateRef, ...]
    shared_calibration_solution_refs: tuple[CalibrationSolutionRef, ...]
    ray_weights: tuple[RayGeometryWeight, ...]
    normalized_residuals: tuple[float | None, ...]
    robust_weights: tuple[float, ...]
    solver_iterations: int
    solver_converged: bool


@dataclass(frozen=True, kw_only=True)
class GeometryBatch:
    window: WindowKey
    results: tuple[GeometryResult, ...]


@dataclass(frozen=True, kw_only=True)
class SuppressedMeasurement:
    measurement: FusionMeasurement
    representative_measurement_id: str
    distance_m: float
    reason: str


@dataclass(frozen=True, kw_only=True)
class FusionBatchResult:
    """One ingestion/progress/tick response, including rejection and late evidence.

    Empty rays do not imply absence of targets. Window, association, and geometry
    diagnostics remain available alongside measurements and track lifecycle events.
    """

    center_session_id: SessionId
    produced_at: ProcessingTime
    admissions: tuple[AdmissionRecord, ...]
    window_update: WindowUpdate
    candidates: tuple[CandidateBatch, ...]
    associations: tuple[AssociationBatch, ...]
    geometry: tuple[GeometryBatch, ...]
    measurements: tuple[FusionMeasurement, ...]
    tracks: tuple[TrackEvent, ...]
    suppressed_measurements: tuple[SuppressedMeasurement, ...] = ()
    tracking_exclusions: tuple[SuppressedMeasurement, ...] = ()
