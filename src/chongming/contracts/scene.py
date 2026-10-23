"""Scenario generation, privileged truth, evaluation and monitoring contracts.

Only this generation/evaluation side may see ``TruthObjectId`` or perturbation
parameters. Online algorithms receive frames, configurations and ordinary event
contracts from ``model``; none receive ``ScenarioTruth`` or ``SceneDefinition``.
The immutable objects below describe data, and perform no simulation or rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType

from chongming.contracts.fusion import FusionBatchResult
from chongming.contracts.model import (
    CalibrationHealth,
    CalibrationSolution,
    CalibrationState,
    CalibrationStateEvent,
    CameraFrame,
    CameraRole,
    CaptureTime,
    ConfigDigest,
    DetectionBatch,
    DiagnosticEvent,
    EventId,
    FrameId,
    FrameRef,
    FusionMeasurement,
    GeodeticAnchor,
    ImagePlane,
    LocalTrackId,
    Matrix2x2,
    Matrix3x3,
    PixelFormat,
    ProcessingStatus,
    ProcessingTime,
    RayEvent,
    Rotation3x3,
    ScoreDefinition,
    SessionId,
    StationConfig,
    StationEvent,
    StationId,
    StationSessionBinding,
    TrackEvent,
    Vec2Px,
    Vec3,
    Vec3M,
    VoxelSearchSpec,
    WorldFrameId,
)

# Every virtual-scene RNG stream derives from this fixed experiment seed.
SCENE_RANDOM_SEED = 42

TruthObjectId = NewType("TruthObjectId", str)
WorldObjectId = NewType("WorldObjectId", str)
LandmarkId = NewType("LandmarkId", str)
CameraId = NewType("CameraId", str)
BehaviorId = NewType("BehaviorId", str)
VegetationId = NewType("VegetationId", str)


class TimeScale(StrEnum):
    EXPERIMENT_ELAPSED = "experiment_elapsed"
    TAI = "tai"
    UTC = "utc"


@dataclass(frozen=True)
class ClockDomainSpec:
    """Map this experiment's nanoseconds to a declared continuous time scale.

    ``epoch_absolute_ns`` is absent only for an elapsed-time experiment. UTC
    experiments declare the leap-second table/version in ``epoch_definition``.
    """

    clock_domain_id: str
    time_scale: TimeScale
    epoch_definition: str
    epoch_absolute_ns: int | None


@dataclass(frozen=True)
class SimulationTime:
    clock_domain_id: str
    time_ns: int
    step_no: int


@dataclass(frozen=True)
class TimelineSpec:
    clock_domain: ClockDomainSpec
    start_time_ns: int
    end_time_ns: int
    logic_step_ns: int


@dataclass(frozen=True)
class ENUWorldSpec:
    world_frame_id: WorldFrameId
    geodetic_anchor: GeodeticAnchor


@dataclass(frozen=True)
class KinematicState:
    position_enu_m: Vec3M
    velocity_enu_mps: Vec3
    rotation_enu_from_body: Rotation3x3
    angular_velocity_body_radps: Vec3


@dataclass(frozen=True)
class TrajectoryKnot:
    time_ns: int
    state: KinematicState


class TrajectoryInterpolation(StrEnum):
    HOLD = "hold"
    LINEAR = "linear"
    CUBIC_HERMITE = "cubic_hermite"


@dataclass(frozen=True)
class TrajectorySpec:
    """All knot times use the enclosing scenario clock; no wall-clock sampling."""

    interpolation: TrajectoryInterpolation
    knots: tuple[TrajectoryKnot, ...]


@dataclass(frozen=True)
class FlightBehaviorSpec:
    """Persistable reference to deterministic behavior code.

    A callable is deliberately not stored in a scenario. The runtime registry
    resolves behavior_id and implementation_version, then samples using only
    the experiment time, these parameters and the named deterministic stream.
    """

    behavior_id: BehaviorId
    implementation_version: str
    parameters: tuple[tuple[str, str | int | float | bool], ...]
    random_stream: str


@dataclass(frozen=True)
class SphereGeometry:
    radius_m: float


@dataclass(frozen=True)
class BoxGeometry:
    half_extents_body_m: Vec3M


@dataclass(frozen=True)
class CylinderGeometry:
    """Vertical finite cylinder around local +Z, used for cheap scene proxies."""

    radius_m: float
    height_m: float


@dataclass(frozen=True)
class TriangleMeshGeometry:
    vertices_body_m: tuple[Vec3M, ...]
    triangle_vertex_indices: tuple[tuple[int, int, int], ...]


GeometrySpec = SphereGeometry | BoxGeometry | CylinderGeometry | TriangleMeshGeometry


class TreePart(StrEnum):
    TRUNK = "trunk"
    CANOPY = "canopy"


@dataclass(frozen=True)
class TreeVisualSpec:
    """Semantic dimensions; Godot builds and owns all visual geometry."""

    part: TreePart
    height_m: float
    radius_m: float


@dataclass(frozen=True)
class ShrubVisualSpec:
    """Semantic shrub envelope; Godot generates branches and foliage."""

    height_m: float
    radius_m: float


@dataclass(frozen=True)
class GrassVisualSpec:
    """Semantic grass patch; blade instances are render-only geometry."""

    width_m: float
    depth_m: float
    blade_height_m: float
    density_per_m2: float


VegetationVisualSpec = TreeVisualSpec | ShrubVisualSpec | GrassVisualSpec


@dataclass(frozen=True)
class SurfaceAppearance:
    linear_rgb: tuple[float, float, float]
    diffuse_reflectance: float


class TerrainModel(StrEnum):
    FLAT = "flat"
    HEIGHTFIELD = "heightfield"


@dataclass(frozen=True)
class TerrainSpec:
    terrain_id: WorldObjectId
    model: TerrainModel
    base_elevation_m: float
    maximum_relief_m: float
    heightfield_asset_uri: str | None
    heightfield_asset_digest: str | None
    appearance: SurfaceAppearance
    occludes_sight_lines: bool
    heightfield_geometry: TriangleMeshGeometry | None = None
    heightfield_origin_enu_m: tuple[float, float] | None = None


class WorldObjectKind(StrEnum):
    TERRAIN = "terrain"
    OBSTACLE = "obstacle"
    STATIC_STRUCTURE = "static_structure"
    FIXED_REFERENCE = "fixed_reference"


@dataclass(frozen=True)
class WorldObjectSpec:
    object_id: WorldObjectId
    display_name: str
    kind: WorldObjectKind
    category: str
    pose: KinematicState
    geometry: GeometrySpec
    appearance: SurfaceAppearance
    occludes_sight_lines: bool


@dataclass(frozen=True)
class OSMImportConfig:
    """Explicit offline projection, selection and height policy (metres)."""

    anchor: GeodeticAnchor
    bounds_wgs84: tuple[float, float, float, float]  # west, south, east, north
    default_height_m: float
    level_height_m: float
    maximum_height_m: float
    base_elevation_m: float
    # OSM forest polygons do not enumerate individual trees.  These defaults
    # provide a visible, deterministic stand while keeping the proxy bounded.
    forest_spacing_m: float = 24.0
    maximum_forest_trees: int = 512


@dataclass(frozen=True)
class OSMBuilding:
    osm_way_id: int
    footprint_enu_m: tuple[tuple[float, float], ...]  # open, CCW, canonical start
    height_m: float
    height_source: str
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OSMRoad:
    osm_way_id: int
    centerline_enu_m: tuple[tuple[float, float], ...]
    width_m: float
    road_class: str
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OSMPointFeature:
    osm_node_id: int
    position_enu_m: tuple[float, float]
    category: str
    height_m: float
    radius_m: float
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OSMArea:
    element_id: str
    rings_enu_m: tuple[tuple[tuple[float, float], ...], ...]
    category: str
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OSMElevation:
    origin_enu_m: tuple[float, float]
    spacing_m: float
    heights_m: tuple[tuple[float, ...], ...]
    source: str
    source_sha256: str
    data_date: str
    vertical_datum: str
    reference_height_m: float
    license: str
    attribution: str


@dataclass(frozen=True)
class OSMDiagnostic:
    element_id: str
    reason: str


@dataclass(frozen=True)
class OSMImport:
    format: str
    importer_version: str
    source_url: str
    data_date: str
    source_sha256: str
    attribution: str
    license: str
    config: OSMImportConfig
    buildings: tuple[OSMBuilding, ...]
    diagnostics: tuple[OSMDiagnostic, ...]
    roads: tuple[OSMRoad, ...] = ()
    points: tuple[OSMPointFeature, ...] = ()
    areas: tuple[OSMArea, ...] = ()
    elevation: OSMElevation | None = None


@dataclass(frozen=True)
class OSMLandscape:
    asset_uri: str
    asset_sha256: str
    imported: OSMImport


class VegetationMotionModel(StrEnum):
    STATIC = "static"
    PERIODIC = "periodic"
    WIND_DRIVEN = "wind_driven"


@dataclass(frozen=True)
class VegetationSpec:
    """Dynamic vegetation is separate from static structures and terrain."""

    vegetation_id: VegetationId
    display_name: str
    base_pose: KinematicState
    geometry: GeometrySpec
    appearance: SurfaceAppearance
    motion_model: VegetationMotionModel
    occludes_sight_lines: bool
    visual: VegetationVisualSpec | None = None


@dataclass(frozen=True)
class FlyingObjectSpec:
    truth_object_id: TruthObjectId
    display_name: str
    model_name: str
    trajectory: TrajectorySpec
    behavior: FlightBehaviorSpec | None
    geometry: GeometrySpec
    appearance: SurfaceAppearance
    center_reference_point_body_m: Vec3M


@dataclass(frozen=True)
class ReferenceLandmarkSpec:
    landmark_id: LandmarkId
    world_object_id: WorldObjectId
    position_enu_m: Vec3M
    appearance: SurfaceAppearance


@dataclass(frozen=True)
class EdgeDeviceMetadata:
    station_id: StationId
    display_name: str
    hardware_model: str
    main_camera_id: CameraId
    auxiliary_camera_id: CameraId


@dataclass(frozen=True)
class CameraMetadata:
    camera_id: CameraId
    display_name: str
    profile_id: str
    sensor_format: str | None
    lens_focal_length_mm: float | None
    nominal_diagonal_fov_deg: float
    nominal_frame_rate_hz: float


@dataclass(frozen=True)
class SceneCameraIntrinsics:
    """Actual generating optics, independent of estimated StationConfig optics."""

    image_width_px: int
    image_height_px: int
    focal_x_px: float
    focal_y_px: float
    principal_x_px: float
    principal_y_px: float
    skew_px: float


class SceneDistortionModel(StrEnum):
    NONE = "none"
    BROWN_CONRADY = "brown_conrady"


@dataclass(frozen=True)
class SceneCameraDistortion:
    model: SceneDistortionModel
    radial_k1_k2_k3: tuple[float, float, float]
    tangential_p1_p2: tuple[float, float]


class ShutterModel(StrEnum):
    GLOBAL = "global"
    ROLLING = "rolling"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CameraExposureSpec:
    first_exposure_start_ns: int
    frame_period_ns: int
    exposure_duration_ns: int
    shutter_model: ShutterModel
    rolling_readout_ns: int | None


@dataclass(frozen=True)
class SceneCameraSpec:
    """Camera coordinates: +x right, +y down, +z forward; ENU is right handed.

    The trajectory's body frame is this optical frame. Both main and auxiliary
    actual optical-center positions are specified, including their translation.
    No camera state is reconstructed from estimated installation parameters.
    """

    station_id: StationId
    camera_role: CameraRole
    metadata: CameraMetadata
    actual_intrinsics: SceneCameraIntrinsics
    actual_distortion: SceneCameraDistortion
    optical_pose_trajectory: TrajectorySpec
    exposure: CameraExposureSpec


@dataclass(frozen=True)
class IlluminationSpec:
    sun_direction_enu_unit: Vec3
    illuminance_lux: float
    sky_linear_rgb: tuple[float, float, float]
    atmospheric_visibility_m: float | None = None
    sun_angular_diameter_deg: float = 0.0
    glare_strength: float = 0.0


@dataclass(frozen=True)
class RenderEffectsSpec:
    """Low-cost renderer controls; all effects are optional except vegetation LOD."""

    terrain_detail_strength: float = 0.0
    ssao_intensity: float = 0.0
    fxaa_enabled: bool = False
    god_rays_strength: float = 0.0
    vegetation_lod_near_m: float = 140.0
    vegetation_lod_far_m: float = 750.0


@dataclass(frozen=True)
class SensorArtifactSpec:
    """Explicit image-quality controls; values may be disabled in the baseline."""

    output_pixel_format: PixelFormat
    sensor_noise_std_intensity: float
    intensity_min: float
    intensity_max: float
    auto_exposure_enabled: bool
    dynamic_range_stops: float | None
    saturation_level: float | None
    glare_enabled: bool
    bloom_strength: float | None


@dataclass(frozen=True)
class FogPerturbation:
    visibility_range_m: float
    extinction_per_m: float


@dataclass(frozen=True)
class RainPerturbation:
    rate_mm_per_hour: float
    visible_streak_probability: float


@dataclass(frozen=True)
class CameraJitterPerturbation:
    """Deterministic high-frequency jitter, separate from pose drift."""

    station_id: StationId
    camera_role: CameraRole
    angular_amplitude_rad: Vec3
    pixel_amplitude_px: Vec2Px
    frequency_hz: float
    random_stream: str


@dataclass(frozen=True)
class SceneDefinition:
    world: ENUWorldSpec
    terrain: TerrainSpec
    edge_devices: tuple[EdgeDeviceMetadata, ...]
    cameras: tuple[SceneCameraSpec, ...]
    world_objects: tuple[WorldObjectSpec, ...]
    flying_objects: tuple[FlyingObjectSpec, ...]
    reference_landmarks: tuple[ReferenceLandmarkSpec, ...]
    illumination: IlluminationSpec
    vegetation: tuple[VegetationSpec, ...] = ()
    render_effects: RenderEffectsSpec = field(default_factory=RenderEffectsSpec)
    osm_landscape: OSMLandscape | None = None


@dataclass(frozen=True)
class CameraPosePerturbation:
    """Perturb actual generating poses; never modify the online estimate in lockstep."""

    station_id: StationId
    camera_role: CameraRole
    translation_offset_enu_m: Vec3M
    rotation_offset_camera_rad: Vec3
    drift_rotation_camera_radps: Vec3


@dataclass(frozen=True)
class WindPerturbation:
    mean_velocity_enu_mps: Vec3
    turbulent_velocity_std_enu_mps: Vec3


@dataclass(frozen=True)
class CloudPerturbation:
    layer_altitude_m: float
    optical_density: float
    drift_velocity_enu_mps: Vec3


@dataclass(frozen=True)
class VegetationPerturbation:
    object_id: WorldObjectId
    displacement_amplitude_enu_m: Vec3M
    oscillation_frequency_hz: float


@dataclass(frozen=True)
class CommunicationPerturbation:
    station_id: StationId
    latency_mean_ns: int
    latency_std_ns: int
    loss_probability: float
    duplicate_probability: float


@dataclass(frozen=True)
class ExperimentPerturbations:
    """Explicit future experiment settings, owned solely by generation/evaluation."""

    camera_pose: tuple[CameraPosePerturbation, ...]
    camera_jitter: tuple[CameraJitterPerturbation, ...]
    wind: WindPerturbation | None
    clouds: tuple[CloudPerturbation, ...]
    fog: FogPerturbation | None
    rain: RainPerturbation | None
    vegetation: tuple[VegetationPerturbation, ...]
    communication: tuple[CommunicationPerturbation, ...]


@dataclass(frozen=True)
class ImagingSpec:
    color_space: str
    exposure_samples: int
    sensor_artifacts: SensorArtifactSpec


@dataclass(frozen=True)
class VirtualCalibrationInterval:
    station_id: StationId
    start_time_ns: int
    end_time_ns: int
    health: CalibrationHealth
    correction_error_camera_rad: Vec3
    correction_covariance_rad2: Matrix3x3
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class VirtualObservationSpec:
    """Generator settings do not travel with ordinary online frames or events.

    Fresh per-run sessions are supplied in VirtualObservationRequest, not stored
    in this experiment configuration. The generator owns station-scoped local
    IDs and receives an independent virtual-session event sequencer per station.
    Truth IDs and cross-station correspondences are emitted on a separate truth
    output. Missing trusted calibration yields diagnostics, never placeholder rays.
    """

    detection_probability: float
    false_alarms_per_camera_hour: float
    independent_angular_covariance_rad2: Matrix2x2
    independent_origin_covariance_m2: Matrix3x3
    detection_score_definition: ScoreDefinition
    calibration_intervals: tuple[VirtualCalibrationInterval, ...]


@dataclass(frozen=True)
class ScenarioSpec:
    schema_version: str
    scenario_id: str
    config_digest: ConfigDigest
    random_seed: int
    world_frame_id: WorldFrameId
    voxel_search: VoxelSearchSpec
    station_configs: tuple[StationConfig, ...]
    scene_definition: SceneDefinition
    timeline: TimelineSpec
    imaging: ImagingSpec
    virtual_observations: VirtualObservationSpec
    perturbations: ExperimentPerturbations


@dataclass(frozen=True)
class CameraStateTruth:
    station_id: StationId
    camera_role: CameraRole
    metadata: CameraMetadata
    sample_time: SimulationTime
    optical_state: KinematicState
    actual_intrinsics: SceneCameraIntrinsics
    actual_distortion: SceneCameraDistortion


@dataclass(frozen=True)
class FlyingObjectTruth:
    truth_object_id: TruthObjectId
    display_name: str
    model_name: str
    sample_time: SimulationTime
    state: KinematicState
    center_reference_point_enu_m: Vec3M
    geometry: GeometrySpec
    appearance: SurfaceAppearance


@dataclass(frozen=True)
class ProjectionTruth:
    reference_point_enu_m: Vec3M
    reference_point_camera_m: Vec3M
    centroid_px: Vec2Px | None
    in_front_of_camera: bool
    center_in_field_of_view: bool


@dataclass(frozen=True)
class SightSegment:
    """The closed finite segment from optical center to the reference point.

    Obstacles beyond the target or behind the camera cannot block this segment;
    a ray/line intersection alone is insufficient for capture eligibility.
    """

    optical_center_enu_m: Vec3M
    reference_point_enu_m: Vec3M


@dataclass(frozen=True)
class SegmentOcclusionTruth:
    blocker_object_id: WorldObjectId
    intersection_enu_m: Vec3M
    segment_fraction: float


@dataclass(frozen=True)
class VisibilityQuery:
    sample_time: SimulationTime
    camera: CameraStateTruth
    target: FlyingObjectTruth
    projection: ProjectionTruth
    terrain: TerrainSpec
    world_objects: tuple[WorldObjectSpec, ...]
    vegetation: tuple[VegetationSpec, ...]


@dataclass(frozen=True)
class ReferenceVisibilityQuery:
    sample_time: SimulationTime
    camera: CameraStateTruth
    landmark: ReferenceLandmarkSpec
    projection: ProjectionTruth
    terrain: TerrainSpec
    world_objects: tuple[WorldObjectSpec, ...]
    vegetation: tuple[VegetationSpec, ...]


@dataclass(frozen=True)
class TargetVisibilityTruth:
    station_id: StationId
    camera_role: CameraRole
    truth_object_id: TruthObjectId
    sample_time: SimulationTime
    projection: ProjectionTruth
    sight_segment: SightSegment
    occlusions: tuple[SegmentOcclusionTruth, ...]
    capture_eligible: bool
    # capture_eligible iff center is in view and the finite segment is unobstructed.


@dataclass(frozen=True)
class LandmarkVisibilityTruth:
    station_id: StationId
    camera_role: CameraRole
    landmark_id: LandmarkId
    sample_time: SimulationTime
    projection: ProjectionTruth
    sight_segment: SightSegment
    occlusions: tuple[SegmentOcclusionTruth, ...]
    capture_eligible: bool


@dataclass(frozen=True)
class LogicalSceneSnapshot:
    sample_time: SimulationTime
    world_frame_id: WorldFrameId
    cameras: tuple[CameraStateTruth, ...]
    flying_objects: tuple[FlyingObjectTruth, ...]
    terrain: TerrainSpec
    world_objects: tuple[WorldObjectSpec, ...]
    vegetation: tuple[VegetationSpec, ...]
    reference_landmarks: tuple[ReferenceLandmarkSpec, ...]
    target_visibility: tuple[TargetVisibilityTruth, ...]
    landmark_visibility: tuple[LandmarkVisibilityTruth, ...]
    illumination: IlluminationSpec
    clouds: tuple[CloudPerturbation, ...] = ()
    render_effects: RenderEffectsSpec = field(default_factory=RenderEffectsSpec)


@dataclass(frozen=True)
class FrameImagingInput:
    """Logic-owned exposure samples, projections and visibility for one frame.

    The imager uses these authoritative samples; it does not decide visibility,
    advance time, or sample trajectories using wall-clock/render refresh times.
    ``exposure_samples`` include the exposure midpoint; integration is stage 3.
    """

    station_id: StationId
    station_session_id: SessionId
    camera_role: CameraRole
    frame_id: FrameId
    capture_time: CaptureTime
    online_camera_config_digest: ConfigDigest
    exposure_samples: tuple[LogicalSceneSnapshot, ...]
    sampling_period_ns: int | None = None


@dataclass(frozen=True)
class ImageGenerationRequest:
    scenario_id: str
    random_seed: int
    imaging_spec: ImagingSpec
    frames: tuple[FrameImagingInput, ...]


@dataclass(frozen=True)
class ProjectedTargetTruth:
    truth_object_id: TruthObjectId
    projection: ProjectionTruth
    visibility: TargetVisibilityTruth
    projected_width_px: float | None
    projected_height_px: float | None


@dataclass(frozen=True)
class ProjectedLandmarkTruth:
    landmark_id: LandmarkId
    position_enu_m: Vec3M
    projection: ProjectionTruth
    visibility: LandmarkVisibilityTruth


@dataclass(frozen=True)
class CameraFrameTruth:
    station_id: StationId
    station_session_id: SessionId
    camera_role: CameraRole
    frame_id: FrameId
    capture_time: CaptureTime
    actual_camera: CameraStateTruth
    targets: tuple[ProjectedTargetTruth, ...]
    landmarks: tuple[ProjectedLandmarkTruth, ...]
    sampling_period_ns: int | None = None


@dataclass(frozen=True)
class ImagingBatch:
    """Ordinary camera frames; privileged frame truth is returned separately."""

    frames: tuple[CameraFrame, ...]


@dataclass(frozen=True)
class ImagingTruthBatch:
    """Evaluation-only truth produced alongside an imaging batch."""

    frame_truth: tuple[CameraFrameTruth, ...]


@dataclass(frozen=True)
class StationFrameSet:
    """Main/auxiliary schedules are independent; an unsampled role is None."""

    station_id: StationId
    station_session_id: SessionId
    main_frame: CameraFrame | None
    auxiliary_frame: CameraFrame | None


@dataclass(frozen=True)
class OnlineScenarioStep:
    """Only this output goes to online registration, device and fusion ports.

    Both video and virtual-ray branches are generated during every complete run.
    Selection only chooses which ordinary event stream the center consumes.
    Camera frames use image_session_id; virtual calibration/ray/diagnostic events
    use virtual_session_id. The two fresh sessions never share event sequences.
    """

    station_frames: tuple[StationFrameSet, ...]
    virtual_calibration_states: tuple[CalibrationState, ...]
    virtual_calibration_solutions: tuple[CalibrationSolution, ...]
    virtual_calibration_events: tuple[CalibrationStateEvent, ...]
    virtual_ray_events: tuple[RayEvent, ...]
    virtual_diagnostics: tuple[DiagnosticEvent, ...]


@dataclass(frozen=True)
class ScenarioVideoOutput:
    """The ordinary video出口; it contains no privileged truth metadata."""

    scenario_id: str
    sample_time: SimulationTime
    frames: tuple[CameraFrame, ...]
    rendered: bool = True
    render_skip_reason: str | None = None
    skipped_frame_ids: tuple[FrameId, ...] = ()


@dataclass(frozen=True)
class ScenarioPacketOutput:
    """The ordinary virtual packet出口; truth IDs and correspondences are absent."""

    scenario_id: str
    sample_time: SimulationTime
    calibration_states: tuple[CalibrationState, ...]
    calibration_solutions: tuple[CalibrationSolution, ...]
    calibration_events: tuple[CalibrationStateEvent, ...]
    rays: tuple[RayEvent, ...]
    diagnostics: tuple[DiagnosticEvent, ...]


@dataclass(frozen=True)
class ScenarioTruthOutput:
    """Privileged truth出口 for evaluation, monitoring and evidence only."""

    scenario_id: str
    sample_time: SimulationTime
    logical_scene: LogicalSceneSnapshot
    camera_frames: tuple[CameraFrameTruth, ...]
    online_correspondences: tuple[OnlineTruthCorrespondence, ...]
    experiment_perturbations: ExperimentPerturbations
    rays: tuple[TruthRay, ...] = ()
    timing: ScenarioStepTiming | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TruthRay:
    station_id: StationId
    truth_object_id: TruthObjectId
    origin_enu_m: Vec3M
    direction_enu_unit: Vec3


@dataclass(frozen=True)
class ScenarioStepTiming:
    """Measured processing wall durations, excluding consumer I/O and pacing."""

    logic_ms: float
    frame_truth_ms: float
    render_ms: float
    virtual_ms: float
    total_ms: float
    gpu_ms: float
    gpu_submissions: int


@dataclass(frozen=True)
class OnlineTruthCorrespondence:
    """Evaluation-only mapping; a false alarm has truth_object_id=None.

    ``station_session_id`` scopes the original image frame. A virtual ray's
    ``ray_event_id`` independently carries its virtual session. Local track IDs
    belong to that ray event's session when present, otherwise the image session.
    This mapping must never accompany the ordinary online event stream.
    """

    station_id: StationId
    station_session_id: SessionId
    camera_role: CameraRole
    frame_id: FrameId
    local_track_id: LocalTrackId | None
    ray_event_id: EventId | None
    truth_object_id: TruthObjectId | None


@dataclass(frozen=True)
class VirtualObservationRequest:
    """Per-run station sessions bind shared images to independent virtual events.

    Frame truth uses image_session_id. Virtual rays/calibration/diagnostics and
    their local IDs use virtual_session_id and its dedicated event sequencer.
    """

    scenario_id: str
    random_seed: int
    generation_spec: VirtualObservationSpec
    online_station_configs: tuple[StationConfig, ...]
    frames: tuple[CameraFrameTruth, ...]
    station_sessions: tuple[StationSessionBinding, ...]
    camera_jitter: tuple[CameraJitterPerturbation, ...] = ()


@dataclass(frozen=True)
class VirtualObservationBatch:
    """Ordinary virtual events; truth correspondences are returned separately."""

    calibration_states: tuple[CalibrationState, ...]
    calibration_solutions: tuple[CalibrationSolution, ...]
    calibration_events: tuple[CalibrationStateEvent, ...]
    rays: tuple[RayEvent, ...]
    diagnostics: tuple[DiagnosticEvent, ...]


@dataclass(frozen=True)
class VirtualObservationTruthBatch:
    """Evaluation-only mapping for a virtual observation batch."""

    truth_correspondences: tuple[OnlineTruthCorrespondence, ...]


@dataclass(frozen=True)
class ScenarioTruth:
    """Layered privileged truth, confined to generation, evaluation and monitoring."""

    scenario_id: str
    time_ns: int
    clock_domain_id: str
    logical_scene: LogicalSceneSnapshot
    camera_frames: tuple[CameraFrameTruth, ...]
    online_correspondences: tuple[OnlineTruthCorrespondence, ...]
    experiment_perturbations: ExperimentPerturbations


@dataclass(frozen=True)
class ScenarioStep:
    video: ScenarioVideoOutput
    packets: ScenarioPacketOutput
    truth: ScenarioTruthOutput


@dataclass(frozen=True)
class OnlineEvaluationData:
    detections: tuple[DetectionBatch, ...]
    calibrations: tuple[CalibrationState, ...]
    calibration_solutions: tuple[CalibrationSolution, ...]
    calibration_events: tuple[CalibrationStateEvent, ...]
    events: tuple[StationEvent, ...]
    measurements: tuple[FusionMeasurement, ...]
    tracks: tuple[TrackEvent, ...]
    fusion_batches: tuple[FusionBatchResult, ...]
    processing_timings: tuple[StageProcessingRecord, ...]


@dataclass(frozen=True)
class StageProcessingRecord:
    """Measured stage boundaries, with scoped input references and clock domains."""

    stage: EvaluationStage
    input_frame_refs: tuple[FrameRef, ...]
    input_event_ids: tuple[EventId, ...]
    started_at: ProcessingTime
    completed_at: ProcessingTime
    status: ProcessingStatus


class EvaluationStage(StrEnum):
    DETECTION_2D = "detection_2d"
    TRACKING_2D = "tracking_2d"
    CALIBRATION = "calibration"
    RAY_GENERATION = "ray_generation"
    ASSOCIATION = "association"
    GEOMETRY_3D = "geometry_3d"
    TRACKING_3D = "tracking_3d"
    COMMUNICATION = "communication"
    SYSTEM = "system"


class MetricName(StrEnum):
    PRECISION = "precision"
    RECALL = "recall"
    F1 = "f1"
    FALSE_ALARMS_PER_CAMERA_HOUR = "false_alarms_per_camera_hour"
    LONGEST_MISS_RUN_FRAMES = "longest_miss_run_frames"
    CONFIRMATION_DELAY = "confirmation_delay"
    CENTROID_ERROR = "centroid_error"
    ANGULAR_ERROR = "angular_error"
    ORIGIN_ERROR = "origin_error"
    TRACK_CONTINUITY = "track_continuity"
    ID_SWITCHES = "id_switches"
    TRACK_LOSS_RATE = "track_loss_rate"
    FPS = "fps"
    LATENCY_P50 = "latency_p50"
    LATENCY_P95 = "latency_p95"
    REFERENCE_RECOGNITION_SUCCESS_RATE = "reference_recognition_success_rate"
    CALIBRATION_UPDATE_FREQUENCY = "calibration_update_frequency"
    FROZEN_DURATION = "frozen_duration"
    STALE_DURATION = "stale_duration"
    REPROJECTION_RESIDUAL = "reprojection_residual"
    UNCERTAINTY_COVERAGE = "uncertainty_coverage"
    HORIZONTAL_RMSE = "horizontal_rmse"
    VERTICAL_RMSE = "vertical_rmse"
    DEPTH_RMSE = "depth_rmse"
    MAE = "mae"
    ERROR_P95 = "error_p95"
    ASSOCIATION_ACCURACY = "association_accuracy"
    GEOMETRY_CONDITION = "geometry_condition"
    EVENT_FIELD_COMPLETENESS = "event_field_completeness"
    SEQUENCE_ERRORS = "sequence_errors"
    ERROR_EVENT_COUNT = "error_event_count"
    BYTES_PER_EVENT = "bytes_per_event"
    EVENT_RATE = "event_rate"
    CPU_USAGE = "cpu_usage"
    MEMORY_USAGE = "memory_usage"
    GPU_USAGE = "gpu_usage"
    POWER = "power"
    TEMPERATURE = "temperature"
    STABLE_RUN_DURATION = "stable_run_duration"
    HARDWARE_COST = "hardware_cost"


class MetricUnit(StrEnum):
    RATIO = "ratio"
    COUNT = "count"
    PX = "px"
    RAD = "rad"
    M = "m"
    NS = "ns"
    HZ = "hz"
    BYTES = "bytes"
    WATTS = "watts"
    CELSIUS = "celsius"
    CURRENCY = "currency"
    DIMENSIONLESS = "dimensionless"


@dataclass(frozen=True)
class EvaluationStratum:
    target_pixel_size_range_px: tuple[float, float] | None
    target_distance_range_m: tuple[float, float] | None
    target_speed_range_mps: tuple[float, float] | None
    background_class: str
    illuminance_range_lux: tuple[float, float] | None
    occlusion_class: str
    communication_condition: str
    calibration_health: CalibrationHealth | None


@dataclass(frozen=True)
class MetricValue:
    stage: EvaluationStage
    name: MetricName
    unit: MetricUnit
    value: float | None
    sample_count: int
    undefined_reason: str | None
    # Undefined/unsupported metrics remain None; never publish a fabricated zero.


@dataclass(frozen=True)
class EvaluationSpec:
    metrics: tuple[tuple[EvaluationStage, MetricName], ...]
    strata: tuple[EvaluationStratum, ...]
    association_distance_gate_m: float
    observation_angle_gate_rad: float
    report_conditional_uncertainty: bool
    # Shared installation/calibration scans report absolute errors separately.


@dataclass(frozen=True)
class EvaluationRecord:
    scenario_id: str
    clock_domain_id: str
    interval_start_ns: int
    interval_end_ns: int
    stratum: EvaluationStratum
    metrics: tuple[MetricValue, ...]
    source_frame_refs: tuple[FrameRef, ...]
    source_ray_event_ids: tuple[EventId, ...]


@dataclass(frozen=True)
class EvaluationReport:
    scenario_id: str
    random_seed: int
    records: tuple[EvaluationRecord, ...]
    simulation_only: bool


class MonitoringLayer(StrEnum):
    WORLD_OBJECTS = "world_objects"
    CAMERA_POSES_AND_FIELDS_OF_VIEW = "camera_poses_and_fields_of_view"
    FLYING_OBJECT_POSES_AND_VELOCITIES = "flying_object_poses_and_velocities"
    TRUTH_SIGHT_SEGMENTS = "truth_sight_segments"
    DEVICE_WORLD_RAYS = "device_world_rays"
    CENTER_CANDIDATES_AND_TRACKS = "center_candidates_and_tracks"
    CAMERA_VIDEO_PREVIEWS = "camera_video_previews"
    METADATA_AND_HEALTH = "metadata_and_health"


@dataclass(frozen=True)
class MonitoringSpec:
    refresh_hz: float
    layers: tuple[MonitoringLayer, ...]
    window_width_px: int
    window_height_px: int


@dataclass(frozen=True)
class MonitoringSnapshot:
    """A read-only snapshot: monitor refresh never consumes/advances experiment time."""

    sample_time: SimulationTime
    truth: ScenarioTruth
    camera_frames: tuple[CameraFrame, ...]
    online: OnlineEvaluationData
    evaluation_records: tuple[EvaluationRecord, ...]


@dataclass(frozen=True)
class MonitoringFrame:
    sample_time: SimulationTime
    image: ImagePlane
