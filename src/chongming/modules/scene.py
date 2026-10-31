"""Deterministic scene-generation primitives.

The first implementation deliberately keeps rendering and online algorithms
separate.  The classes in this module operate only on immutable scene
contracts and the experiment clock; no wall clock or global random state is
consulted.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import time
from collections.abc import Iterable, Iterator
from dataclasses import replace
from time import perf_counter

import numpy as np

from chongming.contracts.model import (
    CalibrationHealth,
    CalibrationQuality,
    CalibrationSolution,
    CalibrationSolutionId,
    CalibrationSolutionRef,
    CalibrationState,
    CalibrationStateEvent,
    CalibrationUsage,
    CalibrationUseEvidence,
    CameraFrame,
    CameraRole,
    CaptureTime,
    DiagnosticEvent,
    EventEnvelope,
    EventId,
    EventType,
    FrameId,
    FrameRef,
    ImagePlane,
    LocalTrackId,
    Matrix3x3,
    PixelFormat,
    ProcessingTime,
    RayEvent,
    RayUncertainty,
    StationConfig,
    StationId,
    StationSessionBinding,
    TimeInterval,
    Vec3M,
)
from chongming.contracts.runtime import ModuleHealth
from chongming.contracts.scene import (
    SCENE_RANDOM_SEED,
    CameraFrameTruth,
    CameraJitterPerturbation,
    CameraStateTruth,
    EvaluationRecord,
    EvaluationReport,
    EvaluationSpec,
    ExperimentPerturbations,
    FlyingObjectTruth,
    FrameImagingInput,
    ImageGenerationRequest,
    ImagingBatch,
    ImagingTruthBatch,
    KinematicState,
    LandmarkVisibilityTruth,
    LogicalSceneSnapshot,
    MonitoringFrame,
    MonitoringSnapshot,
    MonitoringSpec,
    OcclusionMode,
    OnlineEvaluationData,
    OnlineTruthCorrespondence,
    ProjectedLandmarkTruth,
    ProjectedTargetTruth,
    ProjectionTruth,
    ReferenceVisibilityQuery,
    ScenarioPacketOutput,
    ScenarioSpec,
    ScenarioStep,
    ScenarioStepTiming,
    ScenarioTruth,
    ScenarioTruthOutput,
    ScenarioVideoOutput,
    SceneCameraSpec,
    SceneDefinition,
    SegmentOcclusionTruth,
    SightSegment,
    SimulationTime,
    TargetVisibilityTruth,
    TerrainSpec,
    TimelineSpec,
    TrajectorySpec,
    TriangleMeshGeometry,
    TruthRay,
    VegetationMotionModel,
    VirtualCalibrationInterval,
    VirtualObservationBatch,
    VirtualObservationRequest,
    VirtualObservationTruthBatch,
    VisibilityQuery,
    WorldObjectId,
    WorldObjectKind,
    WorldObjectSpec,
)
from chongming.modules.base import ModuleShell
from chongming.ports.device import EventSequencer
from chongming.ports.scene import (
    CameraImager,
    CameraProjector,
    ExperimentClock,
    SceneLogic,
    TrajectorySampler,
    VirtualObservationGenerator,
    VisibilityResolver,
)
from chongming.rendering.geometry import (
    contains_point,
    mesh_segment_may_intersect,
    resolve_geometry,
    surface_hits,
)
from chongming.rendering.godot import (
    close_default_renderer,
    default_renderer,
    projected_footprints,
    render_rgb8,
)
from chongming.rendering.occlusion_index import OcclusionIndex
from chongming.scenarios.behaviors import sample_behavior
from chongming.scenarios.uncertainty import gaussian_sample
from chongming.uncertainty import covariance_factor

_EPS = 1e-9
_TODO = "TODO(stage-3): business logic is intentionally absent from the architecture skeleton"


def _scheduled_exposures(
    cameras: tuple[SceneCameraSpec, ...],
    timeline: TimelineSpec,
) -> Iterator[tuple[int, list[tuple[int, SceneCameraSpec]]]]:
    """One pending exposure per camera, independent of experiment duration."""
    ordered = sorted(cameras, key=lambda c: (str(c.station_id), c.camera_role.value))
    pending: list[tuple[int, int]] = []
    for index, camera in enumerate(ordered):
        midpoint = (
            camera.exposure.first_exposure_start_ns + camera.exposure.exposure_duration_ns // 2
        )
        period = camera.exposure.frame_period_ns
        if midpoint < timeline.start_time_ns:
            midpoint += ((timeline.start_time_ns - midpoint + period - 1) // period) * period
        if midpoint <= timeline.end_time_ns:
            heapq.heappush(pending, (midpoint, index))
    while pending:
        midpoint = pending[0][0]
        group: list[tuple[int, SceneCameraSpec]] = []
        while pending and pending[0][0] == midpoint:
            _, index = heapq.heappop(pending)
            camera = ordered[index]
            group.append((midpoint, camera))
            following = midpoint + camera.exposure.frame_period_ns
            if following <= timeline.end_time_ns:
                heapq.heappush(pending, (following, index))
        yield midpoint, group


def _sub(a: Vec3M, b: Vec3M) -> Vec3M:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec3M, b: Vec3M) -> Vec3M:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _deterministic_wind(seed: int, object_id: str, time_ns: int, mean: Vec3M, std: Vec3M) -> Vec3M:
    """Smooth replayable half-second turbulence samples keyed by object and seed."""
    interval_ns = 500_000_000
    index, remainder = divmod(time_ns, interval_ns)
    blend = remainder / interval_ns
    blend = blend * blend * (3.0 - 2.0 * blend)

    def sample(tick: int) -> Vec3M:
        digest = hashlib.blake2b(
            f"{seed}:wind/{object_id}/{tick}".encode(), digest_size=24
        ).digest()
        values = []
        for component in range(3):
            first = (int.from_bytes(digest[component * 8 : component * 8 + 4], "big") + 0.5) / 2**32
            second = (
                int.from_bytes(digest[component * 8 + 4 : component * 8 + 8], "big") + 0.5
            ) / 2**32
            values.append(math.sqrt(-2.0 * math.log(first)) * math.cos(2.0 * math.pi * second))
        return tuple(values)  # type: ignore[return-value]

    a, b = sample(index), sample(index + 1)
    return tuple(mean[i] + std[i] * (a[i] * (1.0 - blend) + b[i] * blend) for i in range(3))  # type: ignore[return-value]


def _scale(a: Vec3M, value: float) -> Vec3M:
    return (a[0] * value, a[1] * value, a[2] * value)


def _dot(a: Vec3M, b: Vec3M) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _mat_vec(matrix: tuple[tuple[float, float, float], ...], vector: Vec3M) -> Vec3M:
    result: Vec3M = (
        matrix[0][0] * vector[0] + matrix[0][1] * vector[1] + matrix[0][2] * vector[2],
        matrix[1][0] * vector[0] + matrix[1][1] * vector[1] + matrix[1][2] * vector[2],
        matrix[2][0] * vector[0] + matrix[2][1] * vector[1] + matrix[2][2] * vector[2],
    )
    return result


def _transpose(
    matrix: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return (
        (matrix[0][0], matrix[1][0], matrix[2][0]),
        (matrix[0][1], matrix[1][1], matrix[2][1]),
        (matrix[0][2], matrix[1][2], matrix[2][2]),
    )


def _lerp(a: Vec3M, b: Vec3M, fraction: float) -> Vec3M:
    return _add(a, _scale(_sub(b, a), fraction))


def _rotation_from_small_angle(offset: Vec3M) -> Matrix3x3:
    """Rodrigues rotation of a camera-frame rotation vector; remains orthonormal."""
    angle = math.sqrt(_dot(offset, offset))
    if angle < 1e-15:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    x, y, z = _scale(offset, 1.0 / angle)
    c, s, v = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return (
        (c + x * x * v, x * y * v - z * s, x * z * v + y * s),
        (y * x * v + z * s, c + y * y * v, y * z * v - x * s),
        (z * x * v - y * s, z * y * v + x * s, c + z * z * v),
    )


def _mat_mul(left: Matrix3x3, right: Matrix3x3) -> Matrix3x3:
    return (
        (
            sum(left[0][k] * right[k][0] for k in range(3)),
            sum(left[0][k] * right[k][1] for k in range(3)),
            sum(left[0][k] * right[k][2] for k in range(3)),
        ),
        (
            sum(left[1][k] * right[k][0] for k in range(3)),
            sum(left[1][k] * right[k][1] for k in range(3)),
            sum(left[1][k] * right[k][2] for k in range(3)),
        ),
        (
            sum(left[2][k] * right[k][0] for k in range(3)),
            sum(left[2][k] * right[k][1] for k in range(3)),
            sum(left[2][k] * right[k][2] for k in range(3)),
        ),
    )


def _unit(vector: Vec3M) -> Vec3M:
    length = math.sqrt(_dot(vector, vector))
    if not math.isfinite(length) or length <= _EPS:
        raise ValueError("cannot normalize a zero or non-finite vector")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _cross(left: Vec3M, right: Vec3M) -> Vec3M:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _tangent_basis(direction: Vec3M) -> tuple[Vec3M, Vec3M]:
    reference: Vec3M = (0.0, 0.0, 1.0) if abs(direction[2]) < 0.9 else (0.0, 1.0, 0.0)
    tangent_u = _unit(_cross(reference, direction))
    tangent_v = _unit(_cross(direction, tangent_u))
    return tangent_u, tangent_v


def _estimated_rotation(actual: Matrix3x3, error_camera_rad: Vec3M) -> Matrix3x3:
    approximate = np.asarray(_mat_mul(actual, _rotation_from_small_angle(error_camera_rad)))
    left, _, right = np.linalg.svd(approximate)
    rotation = left @ right
    if np.linalg.det(rotation) < 0:
        left[:, -1] *= -1
        rotation = left @ right
    return tuple(tuple(float(value) for value in row) for row in rotation)  # type: ignore[return-value]


def _deterministic_unit_interval(seed: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _deterministic_poisson(seed: int, key: str, expected_count: float) -> int:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    generator = np.random.default_rng(int.from_bytes(digest[:8], "big"))
    return int(generator.poisson(expected_count))


class ExperimentClockModule(ModuleShell):
    business_implemented = True

    def __init__(self) -> None:
        self._timeline: TimelineSpec | None = None
        self._time: SimulationTime | None = None

    def initialize(self, timeline: TimelineSpec) -> None:
        if timeline.start_time_ns > timeline.end_time_ns:
            raise ValueError("timeline start must not be after end")
        if timeline.logic_step_ns <= 0:
            raise ValueError("logic_step_ns must be positive")
        if timeline.clock_domain.time_scale.value != "experiment_elapsed" and (
            timeline.clock_domain.epoch_absolute_ns is None
        ):
            raise ValueError("absolute clock domains require epoch_absolute_ns")
        if timeline.clock_domain.time_scale.value == "experiment_elapsed" and (
            timeline.clock_domain.epoch_absolute_ns is not None
        ):
            raise ValueError("elapsed clock domains cannot define an absolute epoch")
        self._timeline = timeline
        self._time = SimulationTime(
            clock_domain_id=timeline.clock_domain.clock_domain_id,
            time_ns=timeline.start_time_ns,
            step_no=0,
        )

    def read(self) -> SimulationTime:
        if self._time is None:
            raise RuntimeError("experiment clock is not initialized")
        return self._time

    def advance_to(self, time_ns: int) -> SimulationTime:
        if self._timeline is None or self._time is None:
            raise RuntimeError("experiment clock is not initialized")
        if time_ns < self._time.time_ns:
            raise ValueError("experiment clock cannot move backwards")
        if time_ns > self._timeline.end_time_ns:
            raise ValueError("experiment clock cannot pass timeline end")
        if time_ns == self._time.time_ns:
            return self._time
        self._time = SimulationTime(
            clock_domain_id=self._time.clock_domain_id,
            time_ns=time_ns,
            step_no=self._time.step_no + 1,
        )
        return self._time


class TrajectoryModule(ModuleShell):
    business_implemented = True

    def sample(self, trajectory: TrajectorySpec, time: SimulationTime) -> KinematicState:
        if not trajectory.knots:
            raise ValueError("trajectory must contain at least one knot")
        knots = trajectory.knots
        if any(knots[index].time_ns >= knots[index + 1].time_ns for index in range(len(knots) - 1)):
            raise ValueError("trajectory knot times must be strictly increasing")
        if time.time_ns <= knots[0].time_ns:
            return knots[0].state
        if time.time_ns >= knots[-1].time_ns:
            return knots[-1].state
        right_index = next(
            index for index, knot in enumerate(knots) if knot.time_ns >= time.time_ns
        )
        left = knots[right_index - 1]
        right = knots[right_index]
        if trajectory.interpolation.value == "hold":
            return right.state if time.time_ns == right.time_ns else left.state
        fraction = (time.time_ns - left.time_ns) / (right.time_ns - left.time_ns)
        if trajectory.interpolation.value == "linear":
            return KinematicState(
                position_enu_m=_lerp(
                    left.state.position_enu_m, right.state.position_enu_m, fraction
                ),
                velocity_enu_mps=_lerp(
                    left.state.velocity_enu_mps, right.state.velocity_enu_mps, fraction
                ),
                rotation_enu_from_body=(
                    (
                        left.state.rotation_enu_from_body[0][0]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[0][0]
                            - left.state.rotation_enu_from_body[0][0]
                        ),
                        left.state.rotation_enu_from_body[0][1]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[0][1]
                            - left.state.rotation_enu_from_body[0][1]
                        ),
                        left.state.rotation_enu_from_body[0][2]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[0][2]
                            - left.state.rotation_enu_from_body[0][2]
                        ),
                    ),
                    (
                        left.state.rotation_enu_from_body[1][0]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[1][0]
                            - left.state.rotation_enu_from_body[1][0]
                        ),
                        left.state.rotation_enu_from_body[1][1]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[1][1]
                            - left.state.rotation_enu_from_body[1][1]
                        ),
                        left.state.rotation_enu_from_body[1][2]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[1][2]
                            - left.state.rotation_enu_from_body[1][2]
                        ),
                    ),
                    (
                        left.state.rotation_enu_from_body[2][0]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[2][0]
                            - left.state.rotation_enu_from_body[2][0]
                        ),
                        left.state.rotation_enu_from_body[2][1]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[2][1]
                            - left.state.rotation_enu_from_body[2][1]
                        ),
                        left.state.rotation_enu_from_body[2][2]
                        + fraction
                        * (
                            right.state.rotation_enu_from_body[2][2]
                            - left.state.rotation_enu_from_body[2][2]
                        ),
                    ),
                ),
                angular_velocity_body_radps=_lerp(
                    left.state.angular_velocity_body_radps,
                    right.state.angular_velocity_body_radps,
                    fraction,
                ),
            )
        if trajectory.interpolation.value == "cubic_hermite":
            dt = (right.time_ns - left.time_ns) / 1_000_000_000.0
            t2 = fraction * fraction
            t3 = t2 * fraction
            h00 = 2 * t3 - 3 * t2 + 1
            h10 = t3 - 2 * t2 + fraction
            h01 = -2 * t3 + 3 * t2
            h11 = t3 - t2
            dh00 = 6 * t2 - 6 * fraction
            dh10 = 3 * t2 - 4 * fraction + 1
            dh01 = -6 * t2 + 6 * fraction
            dh11 = 3 * t2 - 2 * fraction
            position: Vec3M = (
                h00 * left.state.position_enu_m[0]
                + h10 * dt * left.state.velocity_enu_mps[0]
                + h01 * right.state.position_enu_m[0]
                + h11 * dt * right.state.velocity_enu_mps[0],
                h00 * left.state.position_enu_m[1]
                + h10 * dt * left.state.velocity_enu_mps[1]
                + h01 * right.state.position_enu_m[1]
                + h11 * dt * right.state.velocity_enu_mps[1],
                h00 * left.state.position_enu_m[2]
                + h10 * dt * left.state.velocity_enu_mps[2]
                + h01 * right.state.position_enu_m[2]
                + h11 * dt * right.state.velocity_enu_mps[2],
            )
            velocity: Vec3M = (
                (
                    dh00 * left.state.position_enu_m[0]
                    + dh10 * dt * left.state.velocity_enu_mps[0]
                    + dh01 * right.state.position_enu_m[0]
                    + dh11 * dt * right.state.velocity_enu_mps[0]
                )
                / dt,
                (
                    dh00 * left.state.position_enu_m[1]
                    + dh10 * dt * left.state.velocity_enu_mps[1]
                    + dh01 * right.state.position_enu_m[1]
                    + dh11 * dt * right.state.velocity_enu_mps[1]
                )
                / dt,
                (
                    dh00 * left.state.position_enu_m[2]
                    + dh10 * dt * left.state.velocity_enu_mps[2]
                    + dh01 * right.state.position_enu_m[2]
                    + dh11 * dt * right.state.velocity_enu_mps[2]
                )
                / dt,
            )
            return replace(
                left.state,
                position_enu_m=position,
                velocity_enu_mps=velocity,
            )
        raise ValueError(f"unsupported trajectory interpolation: {trajectory.interpolation}")


class CameraProjectionModule(ModuleShell):
    business_implemented = True

    def project(self, camera: CameraStateTruth, reference_point_enu_m: Vec3M) -> ProjectionTruth:
        displacement = _sub(reference_point_enu_m, camera.optical_state.position_enu_m)
        camera_from_enu = _transpose(camera.optical_state.rotation_enu_from_body)
        point = _mat_vec(camera_from_enu, displacement)
        in_front = point[2] > _EPS
        if not in_front:
            return ProjectionTruth(
                reference_point_enu_m=reference_point_enu_m,
                reference_point_camera_m=point,
                centroid_px=None,
                in_front_of_camera=False,
                center_in_field_of_view=False,
            )
        intrinsics = camera.actual_intrinsics
        normalized_x = point[0] / point[2]
        normalized_y = point[1] / point[2]
        distortion = camera.actual_distortion
        if distortion.model.value == "brown_conrady":
            k1, k2, k3 = distortion.radial_k1_k2_k3
            p1, p2 = distortion.tangential_p1_p2
            radius2 = normalized_x * normalized_x + normalized_y * normalized_y
            radial = 1.0 + k1 * radius2 + k2 * radius2 * radius2 + k3 * radius2 * radius2 * radius2
            x, y = normalized_x, normalized_y
            normalized_x = x * radial + 2 * p1 * x * y + p2 * (radius2 + 2 * x * x)
            normalized_y = y * radial + p1 * (radius2 + 2 * y * y) + 2 * p2 * x * y
        u = (
            intrinsics.focal_x_px * normalized_x
            + intrinsics.skew_px * normalized_y
            + intrinsics.principal_x_px
        )
        v = intrinsics.focal_y_px * normalized_y + intrinsics.principal_y_px
        in_view = 0.0 <= u < intrinsics.image_width_px and 0.0 <= v < intrinsics.image_height_px
        return ProjectionTruth(
            reference_point_enu_m=reference_point_enu_m,
            reference_point_camera_m=point,
            centroid_px=(u, v),
            in_front_of_camera=True,
            center_in_field_of_view=in_view,
        )


class VisibilityModule(ModuleShell):
    def __init__(self, occlusion_mode: OcclusionMode = OcclusionMode.KNOWN) -> None:
        self.occlusion_mode = occlusion_mode

    business_implemented = True

    def resolve(self, query: VisibilityQuery) -> TargetVisibilityTruth:
        occlusions = (
            self._occlusions(
                query.projection,
                query.camera.optical_state.position_enu_m,
                query.target.center_reference_point_enu_m,
                query.terrain,
                query.world_objects,
                query.vegetation,
            )
            if self.occlusion_mode is OcclusionMode.KNOWN
            else ()
        )
        return TargetVisibilityTruth(
            station_id=query.camera.station_id,
            camera_role=query.camera.camera_role,
            truth_object_id=query.target.truth_object_id,
            sample_time=query.sample_time,
            projection=query.projection,
            sight_segment=SightSegment(
                optical_center_enu_m=query.camera.optical_state.position_enu_m,
                reference_point_enu_m=query.target.center_reference_point_enu_m,
            ),
            occlusions=occlusions,
            capture_eligible=query.projection.center_in_field_of_view and not occlusions,
        )

    def resolve_reference(self, query: ReferenceVisibilityQuery) -> LandmarkVisibilityTruth:
        occlusions = (
            self._occlusions(
                query.projection,
                query.camera.optical_state.position_enu_m,
                query.landmark.position_enu_m,
                query.terrain,
                query.world_objects,
                query.vegetation,
            )
            if self.occlusion_mode is OcclusionMode.KNOWN
            else ()
        )
        return LandmarkVisibilityTruth(
            station_id=query.camera.station_id,
            camera_role=query.camera.camera_role,
            landmark_id=query.landmark.landmark_id,
            sample_time=query.sample_time,
            projection=query.projection,
            sight_segment=SightSegment(
                optical_center_enu_m=query.camera.optical_state.position_enu_m,
                reference_point_enu_m=query.landmark.position_enu_m,
            ),
            occlusions=occlusions,
            capture_eligible=query.projection.center_in_field_of_view and not occlusions,
        )

    def _occlusions(
        self,
        projection: ProjectionTruth,
        origin: Vec3M,
        target: Vec3M,
        terrain: object,
        world_objects: tuple[object, ...],
        vegetation: tuple[object, ...],
    ) -> tuple[SegmentOcclusionTruth, ...]:
        if not projection.in_front_of_camera:
            return ()
        blockers: list[tuple[float, SegmentOcclusionTruth]] = []
        # Immutable world objects are shared across snapshots. Aircraft are few
        # and change on every tick/query; test them directly so they cannot cause
        # the static city BVH to rebuild. Preserve original input order for ties.
        static_world = tuple(e for e in world_objects if getattr(e, "category", None) != "aircraft")
        world_index = getattr(self, "_world_index", None)
        if world_index is None or not world_index.matches(static_world):
            self._world_index = world_index = OcclusionIndex(static_world)
        vegetation_index = getattr(self, "_vegetation_index", None)
        if vegetation_index is None or not vegetation_index.matches(vegetation):
            self._vegetation_index = vegetation_index = OcclusionIndex(vegetation)
        candidate_ids = {id(e) for e in world_index.candidates(origin, target)}
        entities = (
            terrain,
            *(
                e
                for e in world_objects
                if id(e) in candidate_ids or getattr(e, "category", None) == "aircraft"
            ),
            *vegetation_index.candidates(origin, target),
        )
        for entity in entities:
            if not getattr(entity, "occludes_sight_lines", False):
                continue
            hit = VisibilityModule._segment_hit(entity, origin, target)
            if hit is not None and 0.0 <= hit[0] < 1.0 - _EPS:
                blockers.append((hit[0], hit[1]))
        blockers.sort(key=lambda item: item[0])
        return tuple(item[1] for item in blockers)

    @staticmethod
    def _segment_hit(
        entity: object, origin: Vec3M, target: Vec3M
    ) -> tuple[float, SegmentOcclusionTruth] | None:
        segment = _sub(target, origin)
        length2 = _dot(segment, segment)
        if length2 <= _EPS:
            return None
        pose = getattr(entity, "pose", getattr(entity, "base_pose", None))
        geometry = getattr(entity, "geometry", None)
        object_id = getattr(
            entity,
            "object_id",
            getattr(entity, "vegetation_id", getattr(entity, "terrain_id", None)),
        )
        if object_id is None:
            return None
        if isinstance(entity, TerrainSpec):
            candidates: list[tuple[float, SegmentOcclusionTruth]] = []
            if origin[2] < entity.base_elevation_m:
                candidates.append((0.0, SegmentOcclusionTruth(object_id, origin, 0.0)))
            elif abs(segment[2]) > _EPS:
                fraction = (entity.base_elevation_m - origin[2]) / segment[2]
                if 0.0 < fraction < 1.0:
                    candidates.append(
                        (
                            fraction,
                            SegmentOcclusionTruth(
                                object_id, _add(origin, _scale(segment, fraction)), fraction
                            ),
                        )
                    )
            if (
                entity.heightfield_geometry is not None
                and entity.heightfield_origin_enu_m is not None
            ):
                east, north = entity.heightfield_origin_enu_m
                terrain_pose = KinematicState(
                    (east, north, entity.base_elevation_m),
                    (0.0, 0.0, 0.0),
                    ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                    (0.0, 0.0, 0.0),
                )
                if mesh_segment_may_intersect(
                    origin, target, terrain_pose, entity.heightfield_geometry
                ):
                    depths, _ = surface_hits(
                        origin,
                        np.asarray([segment], dtype=np.float64),
                        terrain_pose,
                        entity.heightfield_geometry,
                    )
                    fraction = float(depths[0])
                    if 0.0 < fraction < 1.0 - _EPS:
                        candidates.append(
                            (
                                fraction,
                                SegmentOcclusionTruth(
                                    object_id, _add(origin, _scale(segment, fraction)), fraction
                                ),
                            )
                        )
            return min(candidates, key=lambda item: item[0]) if candidates else None
        if pose is None or geometry is None:
            return None
        geometry = resolve_geometry(geometry)
        if isinstance(geometry, TriangleMeshGeometry) and not mesh_segment_may_intersect(
            origin, target, pose, geometry
        ):
            return None
        if contains_point(origin, pose, geometry):
            return 0.0, SegmentOcclusionTruth(object_id, origin, 0.0)
        distances, _ = surface_hits(origin, np.asarray([segment], dtype=np.float64), pose, geometry)
        fraction = float(distances[0])
        if 0.0 < fraction < 1.0 - _EPS:
            point = _add(origin, _scale(segment, fraction))
            return fraction, SegmentOcclusionTruth(object_id, point, fraction)
        return None


class SceneLogicModule(ModuleShell):
    business_implemented = True

    def __init__(
        self,
        trajectory_sampler: TrajectorySampler,
        projector: CameraProjector,
        visibility: VisibilityResolver,
    ) -> None:
        self.trajectory_sampler = trajectory_sampler
        self.projector = projector
        self.visibility = visibility

    def initialize(
        self,
        scene: SceneDefinition,
        perturbations: ExperimentPerturbations,
        random_seed: int,
    ) -> None:
        if not scene.cameras:
            raise ValueError("scene must define at least one camera")
        if random_seed != SCENE_RANDOM_SEED:
            raise ValueError(f"virtual scenes require the fixed random_seed {SCENE_RANDOM_SEED}")
        camera_stations = {camera.station_id for camera in scene.cameras}
        device_stations = {device.station_id for device in scene.edge_devices}
        if camera_stations != device_stations:
            raise ValueError("camera and edge-device station sets must match")
        for station_id in camera_stations:
            roles = {
                camera.camera_role for camera in scene.cameras if camera.station_id == station_id
            }
            if roles != {CameraRole.MAIN, CameraRole.AUXILIARY}:
                raise ValueError(f"station {station_id} must define main and auxiliary cameras")
        self._scene = scene
        self._perturbations = perturbations
        self._random_seed = random_seed
        if isinstance(self.visibility, VisibilityModule):
            self.visibility.occlusion_mode = scene.occlusion_mode

    def sample(self, time: SimulationTime) -> LogicalSceneSnapshot:
        scene = getattr(self, "_scene", None)
        if scene is None:
            raise RuntimeError("scene logic is not initialized")
        perturbations: ExperimentPerturbations = self._perturbations
        camera_perturbations = {
            (item.station_id, item.camera_role): item for item in perturbations.camera_pose
        }
        jitter_perturbations = {
            (item.station_id, item.camera_role): item for item in perturbations.camera_jitter
        }
        cameras: list[CameraStateTruth] = []
        for camera_spec in scene.cameras:
            state = self.trajectory_sampler.sample(camera_spec.optical_pose_trajectory, time)
            perturbation = camera_perturbations.get(
                (camera_spec.station_id, camera_spec.camera_role)
            )
            if perturbation is not None:
                state = replace(
                    state,
                    position_enu_m=_add(
                        state.position_enu_m, perturbation.translation_offset_enu_m
                    ),
                    rotation_enu_from_body=_mat_mul(
                        state.rotation_enu_from_body,
                        _rotation_from_small_angle(perturbation.rotation_offset_camera_rad),
                    ),
                    angular_velocity_body_radps=_add(
                        state.angular_velocity_body_radps,
                        perturbation.drift_rotation_camera_radps,
                    ),
                )
            jitter = jitter_perturbations.get((camera_spec.station_id, camera_spec.camera_role))
            if jitter is not None:
                # Physical rotation, never an image-only shift: the same pose drives
                # projection, visibility, ordinary rays, protected truth and Godot.
                # Pixel amplitudes describe equivalent small angular motion at the
                # nominal focal length. No frame counter or wall-clock history.
                intrinsics = camera_spec.actual_intrinsics
                amplitudes = (
                    jitter.angular_amplitude_rad[0]
                    + math.atan(jitter.pixel_amplitude_px[1] / intrinsics.focal_y_px),
                    jitter.angular_amplitude_rad[1]
                    + math.atan(jitter.pixel_amplitude_px[0] / intrinsics.focal_x_px),
                    jitter.angular_amplitude_rad[2],
                )
                angles, rates = [], []
                for axis_index, amplitude in enumerate(amplitudes):
                    phase = math.tau * _deterministic_unit_interval(
                        self._random_seed,
                        f"{jitter.random_stream}/{camera_spec.station_id}/{camera_spec.camera_role.value}/{axis_index}",
                    )
                    omega = math.tau * jitter.frequency_hz * (1.0 + axis_index * 0.173)
                    angle = omega * time.time_ns / 1e9 + phase
                    angles.append(amplitude * math.sin(angle))
                    rates.append(amplitude * omega * math.cos(angle))
                rotation_vector: Vec3M = tuple(angles)  # type: ignore[assignment]
                jitter_rotation = _rotation_from_small_angle(rotation_vector)
                # Right Jacobian maps the rotation-vector derivative to body omega.
                theta = math.sqrt(_dot(rotation_vector, rotation_vector))
                rate: Vec3M = tuple(rates)  # type: ignore[assignment]
                cross_rate = _cross(rotation_vector, rate)
                a = (1.0 - math.cos(theta)) / theta**2 if theta > 1e-5 else 0.5
                b = (theta - math.sin(theta)) / theta**3 if theta > 1e-5 else 1.0 / 6.0
                body_rate = _add(
                    _sub(rate, _scale(cross_rate, a)),
                    _scale(_cross(rotation_vector, cross_rate), b),
                )
                state = replace(
                    state,
                    rotation_enu_from_body=_mat_mul(state.rotation_enu_from_body, jitter_rotation),
                    angular_velocity_body_radps=_add(
                        _mat_vec(tuple(zip(*jitter_rotation)), state.angular_velocity_body_radps),
                        body_rate,
                    ),
                )
            cameras.append(
                CameraStateTruth(
                    station_id=camera_spec.station_id,
                    camera_role=camera_spec.camera_role,
                    metadata=camera_spec.metadata,
                    sample_time=time,
                    optical_state=state,
                    actual_intrinsics=camera_spec.actual_intrinsics,
                    actual_distortion=camera_spec.actual_distortion,
                )
            )
        flying_objects: list[FlyingObjectTruth] = []
        for object_spec in scene.flying_objects:
            state = self.trajectory_sampler.sample(object_spec.trajectory, time)
            if object_spec.behavior is not None:
                first = object_spec.trajectory.knots[0]
                state = sample_behavior(
                    object_spec.behavior, first.state, time.time_ns - first.time_ns
                )
            center = _add(
                state.position_enu_m,
                _mat_vec(state.rotation_enu_from_body, object_spec.center_reference_point_body_m),
            )
            flying_objects.append(
                FlyingObjectTruth(
                    truth_object_id=object_spec.truth_object_id,
                    display_name=object_spec.display_name,
                    model_name=object_spec.model_name,
                    sample_time=time,
                    state=state,
                    center_reference_point_enu_m=center,
                    geometry=object_spec.geometry,
                    appearance=object_spec.appearance,
                )
            )
        vegetation_perturbations = {str(item.object_id): item for item in perturbations.vegetation}
        sampled_vegetation = []
        wind = perturbations.wind
        for item in scene.vegetation:
            sway = vegetation_perturbations.get(str(item.vegetation_id))
            if (
                sway is None
                or wind is None
                or item.motion_model is not VegetationMotionModel.WIND_DRIVEN
                or math.hypot(*wind.mean_velocity_enu_mps[:2])
                + math.hypot(*wind.turbulent_velocity_std_enu_mps[:2])
                <= _EPS
            ):
                sampled_vegetation.append(item)
                continue
            digest = hashlib.blake2b(
                f"{self._random_seed}:vegetation/{item.vegetation_id}".encode(), digest_size=8
            ).digest()
            phase = int.from_bytes(digest, "big") / (2**64) * 2 * math.pi
            seconds = time.time_ns / 1_000_000_000
            angle = 2 * math.pi * sway.oscillation_frequency_hz * seconds + phase
            amplitude = math.sqrt(sum(value * value for value in sway.displacement_amplitude_enu_m))
            wind_velocity = _deterministic_wind(
                self._random_seed,
                str(item.vegetation_id),
                time.time_ns,
                wind.mean_velocity_enu_mps,
                wind.turbulent_velocity_std_enu_mps,
            )
            wind_east, wind_north = wind_velocity[:2]
            wind_speed = math.hypot(wind_east, wind_north)
            if wind_speed <= _EPS:
                sampled_vegetation.append(item)
                continue
            # Mean wind sets the bend direction; two deterministic harmonics
            # create a restrained gust-like motion without wall-clock/RNG use.
            scale = min(1.5, wind_speed / 4.0)
            displacement = (
                amplitude * scale * (math.sin(angle) + 0.18 * math.sin(2.37 * angle + phase * 0.61))
            )
            lean = amplitude * min(wind_speed / 12.0, 0.3)
            bend_angle = max(-0.22, min(0.22, (displacement + lean) / 8.0))
            horizontal_x = wind_east / wind_speed
            horizontal_y = wind_north / wind_speed
            axis = (-horizontal_y, horizontal_x, 0.0)
            cosine, sine = math.cos(bend_angle), math.sin(bend_angle)
            one_minus_cosine = 1.0 - cosine
            ax, ay, az = axis
            rotation: Matrix3x3 = (
                (
                    cosine + ax * ax * one_minus_cosine,
                    ax * ay * one_minus_cosine - az * sine,
                    ax * az * one_minus_cosine + ay * sine,
                ),
                (
                    ay * ax * one_minus_cosine + az * sine,
                    cosine + ay * ay * one_minus_cosine,
                    ay * az * one_minus_cosine - ax * sine,
                ),
                (
                    az * ax * one_minus_cosine - ay * sine,
                    az * ay * one_minus_cosine + ax * sine,
                    cosine + az * az * one_minus_cosine,
                ),
            )
            old_pose = item.base_pose
            sampled_vegetation.append(
                replace(
                    item,
                    base_pose=replace(
                        old_pose,
                        rotation_enu_from_body=rotation,
                    ),
                )
            )
        target_visibility: list[TargetVisibilityTruth] = []
        landmark_visibility: list[LandmarkVisibilityTruth] = []
        # Use the same moving geometry for depth rendering and center-ray truth.
        blockers = tuple(
            WorldObjectSpec(
                WorldObjectId(f"aircraft/{item.truth_object_id}"),
                item.display_name,
                WorldObjectKind.OBSTACLE,
                "aircraft",
                item.state,
                item.geometry,
                item.appearance,
                True,
            )
            for item in flying_objects
        )
        for camera in cameras:
            for index, target in enumerate(flying_objects):
                projection = self.projector.project(camera, target.center_reference_point_enu_m)
                target_visibility.append(
                    self.visibility.resolve(
                        VisibilityQuery(
                            sample_time=time,
                            camera=camera,
                            target=target,
                            projection=projection,
                            terrain=scene.terrain,
                            world_objects=scene.world_objects
                            + blockers[:index]
                            + blockers[index + 1 :],
                            vegetation=tuple(sampled_vegetation),
                        )
                    )
                )
            for landmark in scene.reference_landmarks:
                projection = self.projector.project(camera, landmark.position_enu_m)
                landmark_visibility.append(
                    self.visibility.resolve_reference(
                        ReferenceVisibilityQuery(
                            sample_time=time,
                            camera=camera,
                            landmark=landmark,
                            projection=projection,
                            terrain=scene.terrain,
                            world_objects=scene.world_objects + blockers,
                            vegetation=tuple(sampled_vegetation),
                        )
                    )
                )
        return LogicalSceneSnapshot(
            sample_time=time,
            world_frame_id=scene.world.world_frame_id,
            cameras=tuple(cameras),
            flying_objects=tuple(flying_objects),
            terrain=scene.terrain,
            world_objects=scene.world_objects,
            vegetation=tuple(sampled_vegetation),
            reference_landmarks=scene.reference_landmarks,
            target_visibility=tuple(target_visibility),
            landmark_visibility=tuple(landmark_visibility),
            illumination=scene.illumination,
            occlusion_mode=scene.occlusion_mode,
            clouds=perturbations.clouds,
            render_effects=scene.render_effects,
            wind=perturbations.wind,
        )


def _frame_truth(frame: FrameImagingInput, scene: LogicalSceneSnapshot) -> CameraFrameTruth:
    """Build privileged frame truth without invoking the pixel renderer."""
    camera = next(
        (
            item
            for item in scene.cameras
            if item.station_id == frame.station_id and item.camera_role is frame.camera_role
        ),
        None,
    )
    if camera is None:
        raise ValueError("frame has no matching actual camera state")
    footprints = projected_footprints(scene, camera)
    target_truth: list[ProjectedTargetTruth] = []
    for target_visibility in scene.target_visibility:
        if (
            target_visibility.station_id != frame.station_id
            or target_visibility.camera_role is not frame.camera_role
        ):
            continue
        target = next(
            item
            for item in scene.flying_objects
            if item.truth_object_id == target_visibility.truth_object_id
        )
        target_truth.append(
            ProjectedTargetTruth(
                truth_object_id=target.truth_object_id,
                projection=target_visibility.projection,
                visibility=target_visibility,
                projected_width_px=footprints.get(str(target.truth_object_id), (None, None))[0],
                projected_height_px=footprints.get(str(target.truth_object_id), (None, None))[1],
            )
        )
    landmark_truth: list[ProjectedLandmarkTruth] = []
    for landmark_visibility in scene.landmark_visibility:
        if (
            landmark_visibility.station_id != frame.station_id
            or landmark_visibility.camera_role is not frame.camera_role
        ):
            continue
        landmark = next(
            item
            for item in scene.reference_landmarks
            if item.landmark_id == landmark_visibility.landmark_id
        )
        landmark_truth.append(
            ProjectedLandmarkTruth(
                landmark_id=landmark.landmark_id,
                position_enu_m=landmark.position_enu_m,
                projection=landmark_visibility.projection,
                visibility=landmark_visibility,
            )
        )
    return CameraFrameTruth(
        station_id=frame.station_id,
        station_session_id=frame.station_session_id,
        camera_role=frame.camera_role,
        frame_id=frame.frame_id,
        capture_time=frame.capture_time,
        actual_camera=camera,
        targets=tuple(target_truth),
        landmarks=tuple(landmark_truth),
        sampling_period_ns=frame.sampling_period_ns,
    )


class CameraImagingModule(ModuleShell):
    business_implemented = True

    def start_module(self, module_key: str, now: ProcessingTime) -> ModuleHealth:
        default_renderer()
        return super().start_module(module_key, now)

    def stop_module(self, module_key: str, now: ProcessingTime) -> ModuleHealth:
        close_default_renderer()
        return super().stop_module(module_key, now)

    def __init__(self) -> None:
        self.last_gpu_render_ms = 0.0
        self.last_imager_overhead_ms = 0.0
        self.last_gpu_submissions = 0

    def render(self, request: ImageGenerationRequest) -> tuple[ImagingBatch, ImagingTruthBatch]:
        self.last_gpu_render_ms = 0.0
        self.last_imager_overhead_ms = 0.0
        self.last_gpu_submissions = 0
        render_started = time.perf_counter()
        artifacts = request.imaging_spec.sensor_artifacts
        if artifacts.output_pixel_format is not PixelFormat.RGB8:
            raise ValueError("the Godot scene imager requires rgb8 output")
        frames: list[CameraFrame] = []
        truths: list[CameraFrameTruth] = []
        for frame in request.frames:
            if not frame.exposure_samples:
                raise ValueError("each frame requires at least one logical scene sample")
            midpoint = (
                frame.capture_time.exposure_start_ns + frame.capture_time.exposure_end_ns
            ) // 2
            samples = {sample.sample_time.time_ns: sample for sample in frame.exposure_samples}
            if midpoint not in samples:
                raise ValueError("frame requires an exact exposure-midpoint scene sample")
            scene = samples[midpoint]
            camera = next(
                (
                    item
                    for item in scene.cameras
                    if item.station_id == frame.station_id and item.camera_role is frame.camera_role
                ),
                None,
            )
            if camera is None:
                raise ValueError("frame has no matching actual camera state")
            width = camera.actual_intrinsics.image_width_px
            height = camera.actual_intrinsics.image_height_px
            if width <= 0 or height <= 0:
                raise ValueError("camera dimensions must be positive")
            pixels, gpu_ms = render_rgb8(scene, camera)
            self.last_gpu_submissions += 1
            self.last_gpu_render_ms += gpu_ms
            frames.append(
                CameraFrame(
                    station_id=frame.station_id,
                    station_session_id=frame.station_session_id,
                    camera_role=frame.camera_role,
                    frame_id=frame.frame_id,
                    capture_time=frame.capture_time,
                    image=ImagePlane(
                        width_px=width,
                        height_px=height,
                        pixel_format=PixelFormat.RGB8,
                        row_stride_bytes=width * 3,
                        pixels=pixels,
                    ),
                    camera_config_ref=frame.online_camera_config_digest,
                )
            )
            truths.append(_frame_truth(frame, scene))
        # Wall time outside Godot render execution: serialization, IPC,
        # scheduling/waiting, readback, sensor effects and truth assembly.
        # This is not CPU execution time or CPU utilization.
        self.last_imager_overhead_ms = max(
            0.0, (time.perf_counter() - render_started) * 1000.0 - self.last_gpu_render_ms
        )
        return ImagingBatch(frames=tuple(frames)), ImagingTruthBatch(frame_truth=tuple(truths))

    def describe(self, request: ImageGenerationRequest) -> ImagingTruthBatch:
        """Produce frame truth for a logic sample without any GPU invocation."""
        truths: list[CameraFrameTruth] = []
        for frame in request.frames:
            if not frame.exposure_samples:
                raise ValueError("each frame requires at least one logical scene sample")
            midpoint = (
                frame.capture_time.exposure_start_ns + frame.capture_time.exposure_end_ns
            ) // 2
            samples = {sample.sample_time.time_ns: sample for sample in frame.exposure_samples}
            scene = samples.get(midpoint)
            if scene is None:
                raise ValueError("frame requires an exact exposure-midpoint scene sample")
            truths.append(_frame_truth(frame, scene))
        return ImagingTruthBatch(frame_truth=tuple(truths))


class VirtualObservationModule(ModuleShell):
    """Each sequencer owns one station's fresh virtual event session."""

    business_implemented = True

    def __init__(self, sequencers: tuple[EventSequencer, ...]) -> None:
        self.sequencers = sequencers
        self._interval_keys: dict[StationId, VirtualCalibrationInterval | None] = {}
        self._states: dict[StationId, CalibrationState] = {}
        self._solutions: dict[StationId, CalibrationSolution] = {}
        self._state_versions: dict[StationId, int] = {}

    @staticmethod
    def _midpoint(capture: CaptureTime) -> int:
        return (capture.exposure_start_ns + capture.exposure_end_ns) // 2

    @staticmethod
    def _event_id(envelope: EventEnvelope) -> EventId:
        return (envelope.station_id, envelope.station_session_id, envelope.sequence_no)

    @staticmethod
    def _score(request: VirtualObservationRequest, *, false_alarm: bool = False) -> float:
        definition = request.generation_spec.detection_score_definition
        low = 0.0 if definition.minimum is None else definition.minimum
        high = 1.0 if definition.maximum is None else definition.maximum
        return low + (high - low) * (0.25 if false_alarm else 0.9)

    @staticmethod
    def _active_interval(
        station_id: StationId, time_ns: int, intervals: tuple[VirtualCalibrationInterval, ...]
    ) -> VirtualCalibrationInterval | None:
        matches = tuple(
            item
            for item in intervals
            if item.station_id == station_id and item.start_time_ns <= time_ns < item.end_time_ns
        )
        if len(matches) > 1:
            raise ValueError(f"overlapping virtual calibration intervals: {station_id}")
        return matches[0] if matches else None

    @staticmethod
    def _validate(request: VirtualObservationRequest) -> None:
        spec = request.generation_spec
        covariance_factor(
            spec.independent_angular_covariance_rad2, 2, "independent_angular_covariance_rad2"
        )
        covariance_factor(
            spec.independent_origin_covariance_m2, 3, "independent_origin_covariance_m2"
        )
        if not 0.0 <= spec.detection_probability <= 1.0:
            raise ValueError("detection_probability must be between 0 and 1")
        if spec.false_alarms_per_camera_hour < 0.0:
            raise ValueError("false_alarms_per_camera_hour cannot be negative")
        for interval in spec.calibration_intervals:
            covariance_factor(
                interval.correction_covariance_rad2, 3, "calibration correction_covariance_rad2"
            )
            if interval.end_time_ns <= interval.start_time_ns:
                raise ValueError("virtual calibration interval must have positive duration")

    @staticmethod
    def _jitter_bound_squared(
        frame: CameraFrameTruth, jitter: tuple[CameraJitterPerturbation, ...]
    ) -> float:
        """Bound rotation between any two samples, without reading later poses."""
        item = next(
            (
                item
                for item in jitter
                if item.station_id == frame.station_id and item.camera_role is CameraRole.MAIN
            ),
            None,
        )
        if item is None:
            return 0.0
        intrinsics = frame.actual_camera.actual_intrinsics
        amplitudes = (
            item.angular_amplitude_rad[0]
            + math.atan(item.pixel_amplitude_px[1] / intrinsics.focal_y_px),
            item.angular_amplitude_rad[1]
            + math.atan(item.pixel_amplitude_px[0] / intrinsics.focal_x_px),
            item.angular_amplitude_rad[2],
        )
        return 4 * sum(amplitude * amplitude for amplitude in amplitudes)

    def _transition_calibration(
        self,
        frame: CameraFrameTruth,
        config: StationConfig,
        session: StationSessionBinding,
        sequencer: EventSequencer,
        interval: VirtualCalibrationInterval | None,
        jitter_bound_squared: float,
    ) -> tuple[CalibrationState, CalibrationSolution | None, CalibrationStateEvent | None]:
        station_id = frame.station_id
        if self._interval_keys.get(station_id) == interval and station_id in self._states:
            return self._states[station_id], None, None

        version = self._state_versions.get(station_id, 0) + 1
        self._state_versions[station_id] = version
        self._interval_keys[station_id] = interval
        time_ns = self._midpoint(frame.capture_time)
        now = ProcessingTime(clock_domain_id=frame.capture_time.clock_domain_id, time_ns=time_ns)
        new_solution: CalibrationSolution | None = None
        solution = self._solutions.get(station_id)
        reasons = interval.reason_codes if interval is not None else ("virtual_no_calibration",)
        if jitter_bound_squared:
            reasons = tuple(reason for reason in reasons if reason != "virtual_clean_baseline") + (
                "virtual_untracked_camera_jitter",
                "virtual_calibration_snapshot_held",
                "virtual_conservative_angular_envelope",
            )

        if interval is not None and interval.health is CalibrationHealth.VALID:
            # Keep the snapshot fixed; jitter and configured bias share one envelope.
            covariance = interval.correction_covariance_rad2
            if jitter_bound_squared:
                bound = (
                    math.sqrt(jitter_bound_squared)
                    + math.sqrt(
                        sum(value * value for value in interval.correction_error_camera_rad)
                    )
                ) ** 2
                covariance = (
                    (covariance[0][0] + bound, covariance[0][1], covariance[0][2]),
                    (covariance[1][0], covariance[1][1] + bound, covariance[1][2]),
                    (covariance[2][0], covariance[2][1], covariance[2][2] + bound),
                )
            solution_ref = CalibrationSolutionRef(
                station_id=station_id,
                station_session_id=session.virtual_session_id,
                solution_id=CalibrationSolutionId(f"virtual-calibration-{version}"),
            )
            solution = CalibrationSolution(
                solution_ref=solution_ref,
                station_config_digest=config.config_digest,
                world_frame_id=config.world_frame_id,
                reference_capture_time=frame.capture_time,
                accepted_time=now,
                valid_interval=TimeInterval(
                    clock_domain_id=frame.capture_time.clock_domain_id,
                    start_ns=interval.start_time_ns,
                    end_ns=interval.end_time_ns,
                ),
                main_camera_position_enu_m=config.main_camera_position_enu_m,
                rotation_enu_from_main=_estimated_rotation(
                    frame.actual_camera.optical_state.rotation_enu_from_body,
                    interval.correction_error_camera_rad,
                ),
                correction_covariance_enu_rad2=covariance,
                quality=CalibrationQuality(
                    source="virtual_observation",
                    matched_reference_count=0,
                    reprojection_rmse_px=None,
                    angular_residual_rad=math.sqrt(
                        sum(value * value for value in interval.correction_error_camera_rad)
                    ),
                    reason_codes=reasons,
                ),
            )
            self._solutions[station_id] = solution
            new_solution = solution

        has_usable_solution = solution is not None and interval is not None
        requested_health = interval.health if interval is not None else CalibrationHealth.INVALID
        if (
            requested_health in {CalibrationHealth.FROZEN, CalibrationHealth.STALE}
            and solution is None
        ):
            health = CalibrationHealth.INVALID
            reasons = ("virtual_no_trusted_calibration",)
        else:
            health = requested_health
            if jitter_bound_squared and health is CalibrationHealth.VALID:
                health = CalibrationHealth.FROZEN
        usable_interval = (
            TimeInterval(
                clock_domain_id=frame.capture_time.clock_domain_id,
                start_ns=interval.start_time_ns,
                end_ns=interval.end_time_ns,
            )
            if has_usable_solution and interval is not None
            else None
        )
        state = CalibrationState(
            station_id=station_id,
            station_session_id=session.virtual_session_id,
            station_config_digest=config.config_digest,
            state_version=version,
            health=health,
            solution_ref=solution.solution_ref
            if has_usable_solution and solution is not None
            else None,
            observed_at=now,
            last_trusted_time=solution.accepted_time if solution is not None else None,
            usable_interval=usable_interval,
            reason_codes=reasons,
        )
        self._states[station_id] = state
        envelope = sequencer.next_envelope(EventType.CALIBRATION_STATE, config.config_digest)
        event = CalibrationStateEvent(envelope=envelope, state=state, solution=new_solution)
        return state, new_solution, event

    @staticmethod
    def _usage(state: CalibrationState) -> CalibrationUsage | None:
        return {
            CalibrationHealth.VALID: CalibrationUsage.VALID,
            CalibrationHealth.FROZEN: CalibrationUsage.FROZEN,
            CalibrationHealth.STALE: CalibrationUsage.STALE,
        }.get(state.health)

    def _ray(
        self,
        request: VirtualObservationRequest,
        frame: CameraFrameTruth,
        config: StationConfig,
        sequencer: EventSequencer,
        state: CalibrationState,
        solution: CalibrationSolution,
        local_direction: Vec3M,
        local_track_id: LocalTrackId,
        *,
        false_alarm: bool = False,
    ) -> RayEvent:
        direction = _unit(_mat_vec(solution.rotation_enu_from_main, _unit(local_direction)))
        tangent_u, tangent_v = _tangent_basis(direction)
        key = (
            f"{request.scenario_id}/{frame.station_id}/{frame.camera_role.value}/"
            f"{frame.capture_time.exposure_start_ns}/{frame.capture_time.exposure_end_ns}/"
            f"{local_track_id}"
        )
        angular = gaussian_sample(
            request.generation_spec.independent_angular_covariance_rad2,
            seed=request.random_seed,
            stream=f"ray-angle/{key}",
            label="independent_angular_covariance_rad2",
        )
        origin_error = gaussian_sample(
            request.generation_spec.independent_origin_covariance_m2,
            seed=request.random_seed,
            stream=f"ray-origin/{key}",
            label="independent_origin_covariance_m2",
        )
        # Rotate the direction and tangent basis together, preserving covariance axes.
        if any(angular):
            tangent = _add(_scale(tangent_u, angular[0]), _scale(tangent_v, angular[1]))
            rotation = _rotation_from_small_angle(_cross(direction, tangent))
            direction = _unit(_mat_vec(rotation, direction))
            tangent_u = _unit(_mat_vec(rotation, tangent_u))
            tangent_v = _unit(_cross(direction, tangent_u))
        origin = solution.main_camera_position_enu_m
        if any(origin_error):
            origin = _add(origin, (origin_error[0], origin_error[1], origin_error[2]))
        usage = self._usage(state)
        if usage is None or state.usable_interval is None or state.last_trusted_time is None:
            raise ValueError("ray generation requires a usable virtual calibration state")
        envelope = sequencer.next_envelope(EventType.RAY, config.config_digest)
        return RayEvent(
            envelope=envelope,
            frame_id=frame.frame_id,
            local_track_id=local_track_id,
            capture_time=frame.capture_time,
            world_frame_id=config.world_frame_id,
            calibration_solution_ref=solution.solution_ref,
            calibration_usage=usage,
            calibration_evidence=CalibrationUseEvidence(
                state_version=state.state_version,
                last_trusted_time=state.last_trusted_time,
                solution_accepted_time=solution.accepted_time,
                solution_reference_capture_time=solution.reference_capture_time,
                solution_valid_interval=solution.valid_interval,
                usable_interval=state.usable_interval,
                reason_codes=state.reason_codes,
            ),
            ray_origin_enu_m=origin,
            ray_direction_enu_unit=direction,
            uncertainty=RayUncertainty(
                model="tangent_gaussian_v1",
                tangent_u_enu_unit=tangent_u,
                tangent_v_enu_unit=tangent_v,
                independent_angular_covariance_rad2=(
                    request.generation_spec.independent_angular_covariance_rad2
                ),
                independent_origin_covariance_m2=(
                    request.generation_spec.independent_origin_covariance_m2
                ),
                shared_installation_state_ref=config.installation_uncertainty.state_ref,
                shared_calibration_solution_ref=solution.solution_ref,
            ),
            detection_score=self._score(request, false_alarm=false_alarm),
            detection_score_definition=request.generation_spec.detection_score_definition,
            track_quality=None,
            track_quality_definition=None,
            quality_flags=state.reason_codes,
        )

    @staticmethod
    def _track_id(session: StationSessionBinding, truth_object_id: object) -> LocalTrackId:
        digest = hashlib.sha256(f"{session.station_id}:{truth_object_id}".encode()).hexdigest()[:16]
        return LocalTrackId(f"virtual-{digest}")

    def _diagnostic(
        self,
        frame: CameraFrameTruth,
        config: StationConfig,
        sequencer: EventSequencer,
        state: CalibrationState,
    ) -> DiagnosticEvent:
        envelope = sequencer.next_envelope(EventType.DIAGNOSTIC, config.config_digest)
        return DiagnosticEvent(
            envelope=envelope,
            related_frame_ref=FrameRef(
                station_id=frame.station_id,
                station_session_id=frame.station_session_id,
                camera_role=frame.camera_role,
                frame_id=frame.frame_id,
            ),
            capture_time=frame.capture_time,
            category="virtual_calibration_unavailable",
            reason_codes=state.reason_codes,
            related_config_ref=config.config_digest,
            related_calibration_ref=state.solution_ref,
        )

    @staticmethod
    def _false_alarm_directions(
        request: VirtualObservationRequest, frame: CameraFrameTruth
    ) -> tuple[Vec3M, ...]:
        if frame.sampling_period_ns is not None and frame.sampling_period_ns <= 0:
            raise ValueError("camera sampling period must be positive")
        frame_rate = (
            1_000_000_000 / frame.sampling_period_ns
            if frame.sampling_period_ns is not None
            else frame.actual_camera.metadata.nominal_frame_rate_hz
        )
        if frame_rate <= 0.0:
            raise ValueError("nominal camera frame rate must be positive")
        expected = request.generation_spec.false_alarms_per_camera_hour / (3600.0 * frame_rate)
        count = _deterministic_poisson(
            request.random_seed, f"false-alarm-count/{frame.station_id}/{frame.frame_id}", expected
        )
        intrinsics = frame.actual_camera.actual_intrinsics
        directions: list[Vec3M] = []
        for index in range(count):
            u = _deterministic_unit_interval(
                request.random_seed,
                f"false-alarm-u/{frame.station_id}/{frame.frame_id}/{index}",
            ) * (intrinsics.image_width_px - 1)
            v = _deterministic_unit_interval(
                request.random_seed,
                f"false-alarm-v/{frame.station_id}/{frame.frame_id}/{index}",
            ) * (intrinsics.image_height_px - 1)
            normalized_y = (v - intrinsics.principal_y_px) / intrinsics.focal_y_px
            normalized_x = (
                u - intrinsics.principal_x_px - intrinsics.skew_px * normalized_y
            ) / intrinsics.focal_x_px
            directions.append((normalized_x, normalized_y, 1.0))
        return tuple(directions)

    def generate(
        self, request: VirtualObservationRequest
    ) -> tuple[VirtualObservationBatch, VirtualObservationTruthBatch]:
        self._validate(request)
        if len(request.station_sessions) != len(self.sequencers):
            raise ValueError("station session bindings must match virtual sequencers")
        configs = {item.station_id: item for item in request.online_station_configs}
        sessions = {item.station_id: item for item in request.station_sessions}
        sequencers = {
            item.station_id: sequencer
            for item, sequencer in zip(request.station_sessions, self.sequencers, strict=True)
        }
        states: list[CalibrationState] = []
        solutions: list[CalibrationSolution] = []
        state_events: list[CalibrationStateEvent] = []
        rays: list[RayEvent] = []
        diagnostics: list[DiagnosticEvent] = []
        correspondences: list[OnlineTruthCorrespondence] = []
        main_frames = tuple(
            frame for frame in request.frames if frame.camera_role is CameraRole.MAIN
        )
        for frame in main_frames:
            config = configs.get(frame.station_id)
            session = sessions.get(frame.station_id)
            sequencer = sequencers.get(frame.station_id)
            if config is None or session is None or sequencer is None:
                raise ValueError(f"missing virtual station binding: {frame.station_id}")
            if frame.station_session_id != session.image_session_id:
                raise ValueError("virtual frame image session does not match station binding")
            visible_targets = tuple(
                item for item in frame.targets if item.visibility.capture_eligible
            )
            selected_targets = tuple(
                item
                for item in visible_targets
                if _deterministic_unit_interval(
                    request.random_seed,
                    f"detect/{frame.station_id}/{frame.frame_id}/{item.truth_object_id}",
                )
                < request.generation_spec.detection_probability
            )
            false_alarm_directions = self._false_alarm_directions(request, frame)
            interval = self._active_interval(
                frame.station_id,
                self._midpoint(frame.capture_time),
                request.generation_spec.calibration_intervals,
            )
            if (
                interval is None
                and frame.station_id not in self._states
                and not selected_targets
                and not false_alarm_directions
            ):
                continue
            state, new_solution, state_event = self._transition_calibration(
                frame,
                config,
                session,
                sequencer,
                interval,
                self._jitter_bound_squared(frame, request.camera_jitter),
            )
            states.append(state)
            if new_solution is not None:
                solutions.append(new_solution)
            if state_event is not None:
                state_events.append(state_event)
            solution = self._solutions.get(frame.station_id)
            if (selected_targets or false_alarm_directions) and (
                solution is None or self._usage(state) is None
            ):
                diagnostics.append(self._diagnostic(frame, config, sequencer, state))
                continue
            for target in selected_targets:
                assert solution is not None
                track_id = self._track_id(session, target.truth_object_id)
                ray = self._ray(
                    request,
                    frame,
                    config,
                    sequencer,
                    state,
                    solution,
                    target.projection.reference_point_camera_m,
                    track_id,
                )
                rays.append(ray)
                correspondences.append(
                    OnlineTruthCorrespondence(
                        station_id=frame.station_id,
                        station_session_id=frame.station_session_id,
                        camera_role=frame.camera_role,
                        frame_id=frame.frame_id,
                        local_track_id=track_id,
                        ray_event_id=self._event_id(ray.envelope),
                        truth_object_id=target.truth_object_id,
                    )
                )
            for index, direction in enumerate(false_alarm_directions):
                assert solution is not None
                track_id = self._track_id(session, f"false/{frame.frame_id}/{index}")
                ray = self._ray(
                    request,
                    frame,
                    config,
                    sequencer,
                    state,
                    solution,
                    direction,
                    track_id,
                    false_alarm=True,
                )
                rays.append(ray)
                correspondences.append(
                    OnlineTruthCorrespondence(
                        station_id=frame.station_id,
                        station_session_id=frame.station_session_id,
                        camera_role=frame.camera_role,
                        frame_id=frame.frame_id,
                        local_track_id=track_id,
                        ray_event_id=self._event_id(ray.envelope),
                        truth_object_id=None,
                    )
                )
        return (
            VirtualObservationBatch(
                calibration_states=tuple(states),
                calibration_solutions=tuple(solutions),
                calibration_events=tuple(state_events),
                rays=tuple(rays),
                diagnostics=tuple(diagnostics),
            ),
            VirtualObservationTruthBatch(truth_correspondences=tuple(correspondences)),
        )


class ScenarioModule(ModuleShell):
    business_implemented = True

    def __init__(
        self,
        clock: ExperimentClock,
        logic: SceneLogic,
        imager: CameraImager,
        virtual_generator: VirtualObservationGenerator,
        station_sessions: tuple[StationSessionBinding, ...],
        render_every: int = 1,
        video_enabled: bool = True,
        virtual_enabled: bool = True,
    ) -> None:
        if type(render_every) is not int or render_every <= 0:
            raise ValueError("render_every must be positive")
        self.clock = clock
        self.logic = logic
        self.imager = imager
        self.virtual_generator = virtual_generator
        self.station_sessions = station_sessions
        self.render_every = render_every
        self.video_enabled = video_enabled
        self.virtual_enabled = virtual_enabled

    def generate(self, spec: ScenarioSpec) -> Iterable[ScenarioStep]:
        from chongming.scenarios.serialization import (
            validate_scenario_spec,
            validate_supported_features,
        )

        validate_scenario_spec(spec)
        validate_supported_features(spec)
        if {s.station_id for s in self.station_sessions} != {
            c.station_id for c in spec.station_configs
        } or len(self.station_sessions) != len(spec.station_configs):
            raise ValueError("station sessions must match station configs exactly")
        self.clock.initialize(spec.timeline)
        self.logic.initialize(
            spec.scene_definition,
            spec.perturbations,
            spec.random_seed,
        )
        station_configs = {config.station_id: config for config in spec.station_configs}
        camera_specs = spec.scene_definition.cameras
        camera_samples: dict[tuple[StationId, CameraRole], int] = {}
        for _, group in _scheduled_exposures(camera_specs, spec.timeline):
            began = perf_counter()
            midpoint = group[0][0]
            time = self.clock.advance_to(midpoint)
            logical_scene = self.logic.sample(time)
            frame_inputs: list[FrameImagingInput] = []
            for index, (_, camera) in enumerate(group):
                session = next(
                    item for item in self.station_sessions if item.station_id == camera.station_id
                )
                config = station_configs.get(camera.station_id)
                if config is None:
                    raise ValueError(f"missing online station config: {camera.station_id}")
                capture = CaptureTime(
                    clock_domain_id=time.clock_domain_id,
                    exposure_start_ns=midpoint - camera.exposure.exposure_duration_ns // 2,
                    exposure_end_ns=midpoint
                    - camera.exposure.exposure_duration_ns // 2
                    + camera.exposure.exposure_duration_ns,
                    clock_uncertainty_ns=0,
                    rolling_shutter_model=(
                        "none"
                        if camera.exposure.shutter_model.value == "global"
                        else camera.exposure.shutter_model.value
                    ),
                )
                frame_inputs.append(
                    FrameImagingInput(
                        station_id=camera.station_id,
                        station_session_id=session.image_session_id,
                        camera_role=camera.camera_role,
                        frame_id=FrameId(
                            f"{spec.scenario_id}:{camera.station_id}:{camera.camera_role.value}:{midpoint}:{index}"
                        ),
                        capture_time=capture,
                        online_camera_config_digest=config.config_digest,
                        exposure_samples=(logical_scene,),
                        sampling_period_ns=camera.exposure.frame_period_ns,
                    )
                )
            image_request = ImageGenerationRequest(
                scenario_id=spec.scenario_id,
                random_seed=spec.random_seed,
                imaging_spec=spec.imaging,
                frames=tuple(frame_inputs),
            )
            logic_finished = perf_counter()
            imaging_truth = self.imager.describe(image_request)
            truth_finished = perf_counter()
            selected: list[FrameImagingInput] = []
            skipped: list[FrameId] = []
            for frame in frame_inputs:
                key = (frame.station_id, frame.camera_role)
                count = camera_samples.get(key, 0)
                camera_samples[key] = count + 1
                if self.video_enabled and count % self.render_every == 0:
                    selected.append(frame)
                else:
                    skipped.append(frame.frame_id)
            rendered = bool(selected)
            if rendered:
                imaging, rendered_truth = self.imager.render(
                    replace(image_request, frames=tuple(selected))
                )
                # Rendering and description must agree; retaining this check prevents
                # a future renderer from silently changing event truth.
                expected_truth = tuple(
                    f for f in imaging_truth.frame_truth if f.frame_id not in skipped
                )
                if rendered_truth.frame_truth != expected_truth:
                    raise RuntimeError("renderer truth diverged from logic description")
            else:
                imaging = ImagingBatch(frames=())
            render_finished = perf_counter()
            if self.virtual_enabled:
                virtual, virtual_truth = self.virtual_generator.generate(
                    VirtualObservationRequest(
                        scenario_id=spec.scenario_id,
                        random_seed=spec.random_seed,
                        generation_spec=spec.virtual_observations,
                        online_station_configs=spec.station_configs,
                        frames=imaging_truth.frame_truth,
                        station_sessions=self.station_sessions,
                        camera_jitter=spec.perturbations.camera_jitter,
                    )
                )
            else:
                virtual = VirtualObservationBatch(
                    calibration_states=(),
                    calibration_solutions=(),
                    calibration_events=(),
                    rays=(),
                    diagnostics=(),
                )
                virtual_truth = VirtualObservationTruthBatch(truth_correspondences=())
            virtual_finished = perf_counter()
            truth_rays: list[TruthRay] = []
            for frame_truth in imaging_truth.frame_truth:
                if frame_truth.camera_role is not CameraRole.MAIN:
                    continue
                for target in frame_truth.targets:
                    if target.visibility.capture_eligible:
                        segment = target.visibility.sight_segment
                        direction = _sub(
                            segment.reference_point_enu_m, segment.optical_center_enu_m
                        )
                        length = math.sqrt(sum(v * v for v in direction))
                        if length > _EPS:
                            truth_rays.append(
                                TruthRay(
                                    frame_truth.station_id,
                                    target.truth_object_id,
                                    segment.optical_center_enu_m,
                                    _scale(direction, 1 / length),
                                )
                            )
            yield ScenarioStep(
                video=ScenarioVideoOutput(
                    scenario_id=spec.scenario_id,
                    sample_time=time,
                    frames=imaging.frames,
                    rendered=rendered,
                    render_skip_reason=(
                        "video_disabled"
                        if not self.video_enabled
                        else "discrete_render_clock"
                        if skipped
                        else None
                    ),
                    skipped_frame_ids=tuple(skipped),
                ),
                packets=ScenarioPacketOutput(
                    scenario_id=spec.scenario_id,
                    sample_time=time,
                    calibration_states=virtual.calibration_states,
                    calibration_solutions=virtual.calibration_solutions,
                    calibration_events=virtual.calibration_events,
                    rays=virtual.rays,
                    diagnostics=virtual.diagnostics,
                ),
                truth=ScenarioTruthOutput(
                    scenario_id=spec.scenario_id,
                    sample_time=time,
                    logical_scene=logical_scene,
                    camera_frames=imaging_truth.frame_truth,
                    online_correspondences=virtual_truth.truth_correspondences,
                    experiment_perturbations=spec.perturbations,
                    rays=tuple(truth_rays),
                    timing=ScenarioStepTiming(
                        logic_ms=(logic_finished - began) * 1000,
                        frame_truth_ms=(truth_finished - logic_finished) * 1000,
                        render_ms=(render_finished - truth_finished) * 1000 if rendered else 0.0,
                        virtual_ms=(virtual_finished - render_finished) * 1000,
                        total_ms=(perf_counter() - began) * 1000,
                        gpu_ms=float(getattr(self.imager, "last_gpu_render_ms", 0.0))
                        if rendered
                        else 0.0,
                        gpu_submissions=int(getattr(self.imager, "last_gpu_submissions", 0))
                        if rendered
                        else 0,
                    ),
                ),
            )


class EvaluationModule(ModuleShell):
    def __init__(self, spec: EvaluationSpec) -> None:
        self.spec = spec

    def observe(
        self, truth: ScenarioTruth, online: OnlineEvaluationData
    ) -> tuple[EvaluationRecord, ...]:
        raise NotImplementedError(_TODO)

    def finalize(self, scenario_id: str, random_seed: int) -> EvaluationReport:
        raise NotImplementedError(_TODO)


class MonitoringModule(ModuleShell):
    def __init__(self, spec: MonitoringSpec) -> None:
        self.spec = spec

    def render(self, snapshot: MonitoringSnapshot) -> MonitoringFrame:
        raise NotImplementedError(_TODO)
