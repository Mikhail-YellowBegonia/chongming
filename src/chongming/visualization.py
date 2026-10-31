"""Read-only adapters for Godot visualization; no matching or online truth inputs."""

from __future__ import annotations

import math

import cv2
import numpy as np

from chongming.contracts.development import CenterAlgorithmResult
from chongming.contracts.model import (
    CalibrationSnapshot,
    CameraRole,
    DiagnosticEvent,
    FusionMeasurement,
    FusionStatus,
    Matrix3x3,
    Matrix6x6,
    RayEvent,
    Rotation3x3,
    SessionId,
    StationConfig,
    StationEvent,
    StationId,
    TrackEvent,
    Vec3,
)
from chongming.contracts.scene import (
    BoxGeometry,
    CameraStateTruth,
    FlyingObjectTruth,
    LogicalSceneSnapshot,
    SimulationTime,
    SphereGeometry,
    TriangleMeshGeometry,
)
from chongming.contracts.visualization import (
    AlgorithmOutputUpdate,
    CenterFusionEvent,
    DiagnosticSummary,
    EdgeStationEvent,
    MeasurementVisualization,
    RayVisualization,
    StationVisualizationState,
    TargetStationVisibility,
    TrackVisualization,
    TruthComparisonSnapshot,
    TruthTargetState,
    VoxelSpaceVisualization,
)
from chongming.ports.scene import SceneLogic
from chongming.rendering.geometry import resolve_geometry
from chongming.uncertainty import covariance_factor


def ray_visualization(ray: RayEvent) -> RayVisualization:
    """Use the major independent angular std, excluding shared pose errors."""
    if not 0.0 <= ray.detection_score <= 1.0:
        raise ValueError("visualization requires detection_score in [0, 1]")
    factor = covariance_factor(
        ray.uncertainty.independent_angular_covariance_rad2, 2, "ray angular covariance"
    )
    envelope = ray.envelope
    return RayVisualization(
        event_id=(envelope.station_id, envelope.station_session_id, envelope.sequence_no),
        capture_time_ns=(ray.capture_time.exposure_start_ns + ray.capture_time.exposure_end_ns)
        // 2,
        origin_enu_m=ray.ray_origin_enu_m,
        direction_enu_unit=ray.ray_direction_enu_unit,
        calibration_usage=ray.calibration_usage,
        detection_score=ray.detection_score,
        angular_std_rad=float(np.linalg.norm(factor, ord=2)),
    )


def diagnostic_summary(event: DiagnosticEvent) -> DiagnosticSummary:
    if event.capture_time is None:
        raise ValueError("cannot visualize a diagnostic without capture_time")
    return DiagnosticSummary(
        capture_time_ns=(event.capture_time.exposure_start_ns + event.capture_time.exposure_end_ns)
        // 2,
        category=event.category,
        reason_codes=event.reason_codes,
    )


def edge_station_event(
    *, station_id: StationId, station_session_id: SessionId, events: tuple[StationEvent, ...]
) -> EdgeStationEvent:
    """Adapt one station/session, including empty output; health has its own adapter."""
    for event in events:
        envelope = event.envelope
        if (envelope.station_id, envelope.station_session_id) != (station_id, station_session_id):
            raise ValueError("edge visualization events must belong to one station/session")
    return EdgeStationEvent(
        station_id=station_id,
        station_session_id=station_session_id,
        rays=tuple(ray_visualization(event) for event in events if isinstance(event, RayEvent)),
        diagnostics=tuple(
            diagnostic_summary(event) for event in events if isinstance(event, DiagnosticEvent)
        ),
    )


def _standard_deviations(covariance: Matrix3x3 | Matrix6x6 | None, offset: int = 0) -> Vec3 | None:
    if covariance is None:
        return None
    covariance_factor(covariance, len(covariance), "visualization covariance")
    return (
        math.sqrt(covariance[offset][offset]),
        math.sqrt(covariance[offset + 1][offset + 1]),
        math.sqrt(covariance[offset + 2][offset + 2]),
    )


def measurement_visualization(measurement: FusionMeasurement) -> MeasurementVisualization:
    located = measurement.status is FusionStatus.LOCATED
    return MeasurementVisualization(
        measurement_id=measurement.measurement_id,
        reference_time_ns=measurement.reference_time_ns,
        status=measurement.status,
        position_enu_m=measurement.position_enu_m if located else None,
        position_std_enu_m=_standard_deviations(measurement.conditional_position_covariance_enu_m2)
        if located
        else None,
        supporting_ray_ids=measurement.supporting_ray_event_ids,
        supporting_station_ids=measurement.supporting_station_ids,
        minimum_intersection_angle_deg=math.degrees(measurement.minimum_intersection_angle_rad)
        if measurement.minimum_intersection_angle_rad is not None
        else None,
        geometry_condition=measurement.geometry_condition,
    )


def track_visualization(track: TrackEvent) -> TrackVisualization:
    return TrackVisualization(
        track_id=track.track_id,
        event_time_ns=track.event_time_ns,
        lifecycle=track.lifecycle,
        position_enu_m=track.position_enu_m,
        velocity_enu_mps=track.velocity_enu_mps,
        position_std_enu_m=_standard_deviations(track.conditional_state_covariance)
        if track.position_enu_m is not None
        else None,
        velocity_std_enu_mps=_standard_deviations(track.conditional_state_covariance, 3)
        if track.velocity_enu_mps is not None
        else None,
        recent_position_enu_m=track.position_enu_m,
        recent_time_ns=track.event_time_ns if track.position_enu_m is not None else None,
    )


def center_fusion_event(
    result: CenterAlgorithmResult, *, voxel_space: VoxelSpaceVisualization | None = None
) -> CenterFusionEvent:
    """Convert outputs plus the separately exported last-window raw voxel snapshot."""
    return CenterFusionEvent(
        measurements=tuple(measurement_visualization(item) for item in result.measurements),
        tracks=tuple(track_visualization(item) for item in result.tracks),
        window_count=sum(len(batch.window_update.sealed_windows) for batch in result.batches),
        candidate_count=sum(
            len(candidates.candidates)
            for batch in result.batches
            for candidates in batch.candidates
        ),
        association_count=sum(
            len(associations.selected_groups)
            for batch in result.batches
            for associations in batch.associations
        ),
        voxel_space=voxel_space,
    )


def _orientation_error(configured: Rotation3x3, orientation: Rotation3x3 | None) -> Vec3 | None:
    if orientation is None:
        return None
    relative = np.asarray(configured).T @ np.asarray(orientation)
    # atan2 retains tiny drift angles that acos/trace-based conversions lose.
    skew = (
        np.array(
            [
                relative[2, 1] - relative[1, 2],
                relative[0, 2] - relative[2, 0],
                relative[1, 0] - relative[0, 1],
            ]
        )
        / 2
    )
    sine = float(np.linalg.norm(skew))
    cosine = float((np.trace(relative) - 1) / 2)
    if cosine > 0:
        vector = skew * (math.atan2(sine, cosine) / sine) if sine else skew
    else:
        vector = cv2.Rodrigues(relative)[0].reshape(3)
    return float(vector[0]), float(vector[1]), float(vector[2])


def station_visualization_state(
    config: StationConfig,
    *,
    actual_camera: CameraStateTruth | None,
    calibration: CalibrationSnapshot | None,
) -> StationVisualizationState:
    """Compare poses in configured camera axes; caller selects calibration as of exposure."""
    if actual_camera is not None and (
        actual_camera.station_id != config.station_id
        or actual_camera.camera_role is not CameraRole.MAIN
    ):
        raise ValueError("station visualization requires this station's main camera truth")
    if calibration is not None and calibration.state.station_id != config.station_id:
        raise ValueError("station visualization calibration belongs to another station")
    configured = config.baseline_orientation_enu_from_main
    actual = (
        actual_camera.optical_state.rotation_enu_from_body if actual_camera is not None else None
    )
    solution = calibration.solution if calibration is not None else None
    calibrated = solution.rotation_enu_from_main if solution is not None else None
    return StationVisualizationState(
        station_id=config.station_id,
        configured_orientation_enu_from_main=configured,
        actual_orientation_enu_from_main=actual,
        calibrated_orientation_enu_from_main=calibrated,
        calibration_usage=calibration.usage if calibration is not None else None,
        truth_orientation_error_rad=_orientation_error(configured, actual),
        calibration_orientation_error_rad=_orientation_error(configured, calibrated),
    )


def _target_geometry(target: FlyingObjectTruth) -> tuple[str, float]:
    """Bounding sphere about the reported center reference, not the body origin."""
    geometry = resolve_geometry(target.geometry)
    displacement = np.asarray(target.center_reference_point_enu_m) - np.asarray(
        target.state.position_enu_m
    )
    center = np.asarray(target.state.rotation_enu_from_body).T @ displacement
    if isinstance(geometry, SphereGeometry):
        return "sphere", geometry.radius_m + float(np.linalg.norm(center))
    if isinstance(geometry, BoxGeometry):
        return "box", math.sqrt(
            sum((abs(center[i]) + geometry.half_extents_body_m[i]) ** 2 for i in range(3))
        )
    if isinstance(geometry, TriangleMeshGeometry):
        radius = max(
            float(np.linalg.norm(np.asarray(vertex) - center))
            for vertex in geometry.vertices_body_m
        )
        return "aircraft", radius
    raise ValueError(f"unsupported visualization target geometry: {type(geometry).__name__}")


def truth_comparison_snapshot(scene: LogicalSceneSnapshot) -> TruthComparisonSnapshot:
    targets = []
    for target in scene.flying_objects:
        geometry_type, radius = _target_geometry(target)
        rotation = np.asarray(target.state.rotation_enu_from_body)
        offset = rotation.T @ (
            np.asarray(target.center_reference_point_enu_m)
            - np.asarray(target.state.position_enu_m)
        )
        velocity = np.asarray(target.state.velocity_enu_mps) + rotation @ np.cross(
            target.state.angular_velocity_body_radps, offset
        )
        targets.append(
            TruthTargetState(
                target_id=str(target.truth_object_id),
                time_ns=scene.sample_time.time_ns,
                position_enu_m=target.center_reference_point_enu_m,
                velocity_enu_mps=(float(velocity[0]), float(velocity[1]), float(velocity[2])),
                geometry_type=geometry_type,
                bounding_radius_m=radius,
                visibility_by_station=tuple(
                    TargetStationVisibility(
                        station_id=visibility.station_id,
                        in_field_of_view=visibility.projection.center_in_field_of_view,
                        capture_eligible=visibility.capture_eligible,
                    )
                    for visibility in scene.target_visibility
                    if visibility.truth_object_id == target.truth_object_id
                    and visibility.camera_role is CameraRole.MAIN
                ),
            )
        )
    return TruthComparisonSnapshot(targets=tuple(targets))


def truth_times(update: AlgorithmOutputUpdate) -> tuple[int, ...]:
    """Array positions bind empty snapshots to these sorted, unique experiment times."""
    times = {update.scenario_time_ns}
    if update.edge_events is not None:
        times.update(ray.capture_time_ns for station in update.edge_events for ray in station.rays)
    if update.center_events is not None:
        times.update(
            measurement.reference_time_ns for measurement in update.center_events.measurements
        )
    return tuple(sorted(times))


def truth_snapshots(
    update: AlgorithmOutputUpdate, *, scene_logic: SceneLogic, clock_domain_id: str
) -> tuple[TruthComparisonSnapshot, ...]:
    """Sample read-only scene logic, never the clock, renderer or correct correspondences."""
    return tuple(
        truth_comparison_snapshot(
            scene_logic.sample(
                SimulationTime(
                    clock_domain_id=clock_domain_id, time_ns=time_ns, step_no=update.step_no
                )
            )
        )
        for time_ns in truth_times(update)
    )
