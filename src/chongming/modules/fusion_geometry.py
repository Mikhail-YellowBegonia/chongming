"""Uncertainty-expanded voxel candidates and continuous ray localization."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import combinations, pairwise, product

import numpy as np
import numpy.typing as npt

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.modules.fusion_estimation import RayFit, fit_rays
from chongming.modules.fusion_motion import (
    alignment_reference,
    matrix3,
    midpoint,
    midpoint_span,
    origins_at,
    validate_policy,
    validate_predictions,
)
from chongming.ports import fusion as ports
from chongming.uncertainty import covariance_factor
from chongming.voxel_diagnostics import VoxelDiagnostics

Array = npt.NDArray[np.float64]
Voxel = tuple[int, int, int]


class _VoxelLimitReached(Exception):
    """Internal budget signal; incomplete searches must not assert uniqueness."""


_STATION_SCORE = m.ScoreDefinition(
    definition_id="independent_station_count_v1",
    kind=m.ScoreKind.RANKING,
    minimum=0,
    maximum=None,
    cross_station_comparison_group=None,
)


def _event_id(ray: f.AdmittedRay) -> m.EventId:
    envelope = ray.event.envelope
    return envelope.station_id, envelope.station_session_id, envelope.sequence_no


def _reference_time(rays: tuple[f.AdmittedRay, ...]) -> int:
    times = [midpoint(ray) for ray in rays]
    return (min(times) + max(times)) // 2


def _exposure_span(rays: tuple[f.AdmittedRay, ...]) -> int:
    return max(ray.event.capture_time.exposure_end_ns for ray in rays) - min(
        ray.event.capture_time.exposure_start_ns for ray in rays
    )


def _ray_voxels(
    ray: f.AdmittedRay,
    search: m.VoxelSearchSpec,
    *,
    aligned_origin: Array | None = None,
    maximum_cells: int | None = None,
) -> set[Voxel]:
    """Traverse the clipped forward segment, including cells touched on faces/edges.

    Grid-plane crossings partition the segment; interval interiors and crossing
    points give a sparse supercover without allocating a full volume or ray samples.
    """
    origin = (
        np.asarray(ray.event.ray_origin_enu_m, dtype=np.float64)
        if aligned_origin is None
        else aligned_origin
    )
    direction = np.asarray(ray.event.ray_direction_enu_unit, dtype=np.float64)
    direction /= np.linalg.norm(direction)
    lower, upper, step = (
        np.asarray(value) for value in (search.min_enu_m, search.max_enu_m, search.step_enu_m)
    )
    near, far = (0.0, math.inf) if search.forward_range_m is None else search.forward_range_m
    for axis in range(3):
        if direction[axis] == 0:
            if not lower[axis] <= origin[axis] <= upper[axis]:
                return set()
        else:
            entry, exit = sorted(
                (
                    (lower[axis] - origin[axis]) / direction[axis],
                    (upper[axis] - origin[axis]) / direction[axis],
                )
            )
            near, far = max(near, float(entry)), min(far, float(exit))
    if near > far:
        return set()
    counts = tuple(math.ceil(float((upper[axis] - lower[axis]) / step[axis])) for axis in range(3))
    crossings = {near, far}
    examined_planes = 0
    for axis in range(3):
        if direction[axis] == 0:
            continue
        ends = sorted(
            (
                float(origin[axis] + near * direction[axis]),
                float(origin[axis] + far * direction[axis]),
            )
        )
        first = max(1, math.ceil((ends[0] - float(lower[axis])) / float(step[axis])))
        last = min(counts[axis] - 1, math.floor((ends[1] - float(lower[axis])) / float(step[axis])))
        for plane in range(first, last + 1):
            examined_planes += 1
            if maximum_cells is not None and examined_planes > maximum_cells:
                raise _VoxelLimitReached
            distance = float((lower[axis] + plane * step[axis] - origin[axis]) / direction[axis])
            if near <= distance <= far:
                crossings.add(distance)
                if maximum_cells is not None and len(crossings) > maximum_cells:
                    raise _VoxelLimitReached
    ordered = sorted(crossings)
    samples = [
        *ordered,
        *((left + (right - left) / 2) for left, right in pairwise(ordered)),
    ]
    cells: set[Voxel] = set()
    for distance in samples:
        grid = (origin + distance * direction - lower) / step
        choices = []
        for axis in range(3):
            value = float(grid[axis])
            nearest = round(value)
            tolerance = 32 * np.finfo(np.float64).eps * max(1, abs(value))
            indices = (
                (nearest - 1, nearest)
                if abs(value - nearest) <= tolerance
                else (math.floor(value),)
            )
            choices.append(tuple(index for index in indices if 0 <= index < counts[axis]))
        cells.update((x, y, z) for x, y, z in product(*choices))
        if maximum_cells is not None and len(cells) > maximum_cells:
            raise _VoxelLimitReached
    return cells


def _uncertain_voxels(
    ray: f.AdmittedRay,
    search: m.VoxelSearchSpec,
    policy: f.CandidatePolicy,
    weight_scale: float,
    *,
    aligned_origin: Array | None,
) -> tuple[set[Voxel], bool]:
    """Conservative k-sigma tube using a range bound over the configured box.

    This is a candidate envelope, not a probability or independent station vote.
    The grid is extended on the same lattice before dilation so central rays
    outside the search box can still support uncertain intersections inside it.
    """
    uncertainty = ray.event.uncertainty
    angular = covariance_factor(
        uncertainty.independent_angular_covariance_rad2, 2, "ray angular covariance"
    )
    origin_root = covariance_factor(
        uncertainty.independent_origin_covariance_m2, 3, "ray origin covariance"
    )
    origin = np.asarray(ray.event.ray_origin_enu_m) if aligned_origin is None else aligned_origin
    lower, upper, step = (
        np.asarray(value) for value in (search.min_enu_m, search.max_enu_m, search.step_enu_m)
    )
    distance = float(np.linalg.norm(np.maximum(np.abs(lower - origin), np.abs(upper - origin))))
    radius = policy.uncertainty_sigma * math.sqrt(
        (
            float(np.linalg.norm(origin_root, ord=2)) ** 2
            + distance**2 * float(np.linalg.norm(angular, ord=2)) ** 2
        )
        / weight_scale
    )
    if radius == 0:
        return _ray_voxels(ray, search, aligned_origin=aligned_origin), False
    counts = tuple(math.ceil(float((upper[axis] - lower[axis]) / step[axis])) for axis in range(3))
    cells: set[Voxel] = set()
    # A very broad tube covers the entire configured volume. Avoid traversing an
    # arbitrarily long padded central ray in that case.
    central: set[Voxel] | None = None
    padding: tuple[int, ...] = (0, 0, 0)
    if radius < float(np.linalg.norm(upper - lower)):
        padding = tuple(math.ceil(radius / float(value)) for value in step)
        expanded_lower = lower - np.asarray(padding, dtype=np.float64) * step
        expanded_upper = (
            lower
            + (np.asarray(counts, dtype=np.float64) + np.asarray(padding, dtype=np.float64)) * step
        )
        expanded = replace(
            search,
            min_enu_m=(
                float(expanded_lower[0]),
                float(expanded_lower[1]),
                float(expanded_lower[2]),
            ),
            max_enu_m=(
                float(expanded_upper[0]),
                float(expanded_upper[1]),
                float(expanded_upper[2]),
            ),
        )
        try:
            central = _ray_voxels(
                ray,
                expanded,
                aligned_origin=aligned_origin,
                maximum_cells=policy.maximum_uncertainty_voxels_per_ray,
            )
        except _VoxelLimitReached:
            return set(), True
    ranges = (
        ((range(int(counts[0])), range(int(counts[1])), range(int(counts[2]))),)
        if central is None
        else (
            tuple(
                range(
                    max(0, int(cell[axis] - 2 * padding[axis])),
                    min(int(counts[axis]), cell[axis] + 1),
                )
                for axis in range(3)
            )
            for cell in sorted(central)
        )
    )
    for choices in ranges:
        # Unlike itertools.product, nested ranges do not materialize axis pools
        # before the budget can stop an extremely fine or broad expansion.
        for x in choices[0]:
            for y in choices[1]:
                for z in choices[2]:
                    cell = (x, y, z)
                    if (
                        cell not in cells
                        and len(cells) >= policy.maximum_uncertainty_voxels_per_ray
                    ):
                        return cells, True
                    cells.add(cell)
    return cells, False


def _line_fit(
    rays: tuple[f.AdmittedRay, ...],
    *,
    reference_ns: int | None = None,
    velocity: m.Vec3 | None = None,
) -> tuple[Array, Array, Array, int, Array]:
    """Solve all perpendicular residuals together, centered to limit cancellation."""
    if velocity is None:
        origins = np.asarray([ray.event.ray_origin_enu_m for ray in rays], dtype=np.float64)
    else:
        assert reference_ns is not None
        origins = origins_at(rays, reference_ns, velocity)
    directions = np.asarray([ray.event.ray_direction_enu_unit for ray in rays], dtype=np.float64)
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    projectors = np.eye(3) - directions[:, :, None] * directions[:, None, :]
    anchor = origins.mean(axis=0)
    design = projectors.reshape(-1, 3)
    offsets = np.einsum("nij,nj->ni", projectors, origins - anchor).reshape(-1)
    delta, _, rank, singular = np.linalg.lstsq(design, offsets, rcond=None)
    position = anchor + delta
    residuals = np.linalg.norm(np.einsum("nij,nj->ni", projectors, position - origins), axis=1)
    depths = np.einsum("ni,ni->n", directions, position - origins)
    if not all(np.isfinite(value).all() for value in (position, residuals, depths, singular)):
        raise np.linalg.LinAlgError("nonfinite continuous ray solution")
    return position, residuals, depths, int(rank), singular


@dataclass(frozen=True, kw_only=True)
class VoxelCandidateModule(ModuleShell):
    business_implemented = True
    policy: f.CandidatePolicy
    motion_policy: f.GeometryPolicy
    weights: ports.GeometryWeighting
    diagnostics: VoxelDiagnostics | None = None

    def __post_init__(self) -> None:
        validate_policy(self.motion_policy)
        search = self.policy.search
        lower, upper, step = (
            np.asarray(value) for value in (search.min_enu_m, search.max_enu_m, search.step_enu_m)
        )
        if (
            not np.isfinite([lower, upper, step]).all()
            or np.any(upper <= lower)
            or np.any(step <= 0)
            or not np.isfinite((upper - lower) / step).all()
            or self.policy.minimum_independent_stations < 2
            or not math.isfinite(self.policy.minimum_candidate_score)
            or self.policy.minimum_candidate_score < 0
            or (self.policy.maximum_candidates is not None and self.policy.maximum_candidates <= 0)
            or not math.isfinite(self.policy.uncertainty_sigma)
            or self.policy.uncertainty_sigma < 0
            or self.policy.maximum_uncertainty_voxels_per_ray <= 0
            or (
                search.forward_range_m is not None
                and (
                    not all(math.isfinite(value) for value in search.forward_range_m)
                    or not 0 <= search.forward_range_m[0] < search.forward_range_m[1]
                )
            )
        ):
            raise ValueError("invalid voxel candidate policy")

    def propose(
        self, window: f.SealedObservationWindow, predictions: tuple[f.TrackPrediction, ...]
    ) -> f.CandidateBatch:
        search = self.policy.search
        examined = tuple(sorted({_event_id(ray) for ray in window.rays}))
        if any(ray.event.world_frame_id != search.world_frame_id for ray in window.rays):
            raise ValueError("candidate search and ray world frames differ")
        validate_predictions(window, search.world_frame_id, predictions)
        usable = replace(
            window,
            rays=tuple(ray for ray in window.rays if self.weights.weight(ray).weight_scale > 0),
        )
        candidates, expansion_limited = self._propose_for(usable, None)
        if self.motion_policy.motion_alignment_model is f.MotionAlignmentModel.TRACK_PREDICTION:
            for prediction in sorted(predictions, key=lambda item: item.track_id):
                if (
                    prediction.conditional_state_covariance_enu is not None
                    and prediction.valid_until_ns > window.key.start_ns
                    and window.key.start_ns <= prediction.reference_time_ns < window.key.end_ns
                    and window.rays
                    and midpoint_span(window.rays)
                ):
                    proposed, limited_expansion = self._propose_for(usable, prediction)
                    candidates.extend(proposed)
                    expansion_limited |= limited_expansion
        limited = (
            self.policy.maximum_candidates is not None
            and len(candidates) > self.policy.maximum_candidates
        )
        if self.policy.maximum_candidates is not None:
            candidates = candidates[: self.policy.maximum_candidates]
        insufficient = (
            len({ray.event.envelope.station_id for ray in usable.rays})
            < self.policy.minimum_independent_stations
        )
        reasons = []
        if limited:
            reasons.append("candidate_limit_reached")
        if expansion_limited:
            reasons.append("uncertainty_expansion_limit_reached")
        if len(usable.rays) != len(window.rays):
            reasons.append("calibration_policy_excluded_rays")
        if not candidates:
            reasons.append(
                "insufficient_independent_stations"
                if insufficient
                else "no_supported_forward_ray_voxel"
            )
        return f.CandidateBatch(
            window=window.key,
            method=f.CandidateMethod.COARSE_VOXEL,
            search=search,
            status=f.CandidateBatchStatus.CANDIDATES_PROPOSED
            if candidates
            else f.CandidateBatchStatus.INSUFFICIENT_STATIONS
            if insufficient
            else f.CandidateBatchStatus.NO_CANDIDATE_IN_VOLUME,
            candidates=tuple(candidates),
            examined_ray_event_ids=examined,
            unguided_search_performed=True,
            reason_codes=tuple(reasons),
        )

    def _propose_for(
        self, window: f.SealedObservationWindow, prediction: f.TrackPrediction | None
    ) -> tuple[list[f.SpatialCandidate], bool]:
        search = self.policy.search
        if prediction is not None:
            # Sweep the configured box across possible support reference times;
            # the final fit is still gated against the original search volume.
            margin = np.abs(prediction.velocity_enu_mps) * (
                (window.key.end_ns - window.key.start_ns) / 1e9
            )
            swept_lower = np.asarray(search.min_enu_m) - margin
            swept_upper = np.asarray(search.max_enu_m) + margin
            search = replace(
                search,
                min_enu_m=(float(swept_lower[0]), float(swept_lower[1]), float(swept_lower[2])),
                max_enu_m=(float(swept_upper[0]), float(swept_upper[1]), float(swept_upper[2])),
            )
        aligned = (
            None
            if prediction is None
            else origins_at(window.rays, window.reference_time_ns, prediction.velocity_enu_mps)
        )
        cells: dict[Voxel, dict[m.StationId, set[m.EventId]]] = {}
        expansion_limited = False
        for index, ray in enumerate(window.rays):
            ray_voxels, limited = _uncertain_voxels(
                ray,
                search,
                self.policy,
                self.weights.weight(ray).weight_scale,
                aligned_origin=None if aligned is None else aligned[index],
            )
            expansion_limited |= limited
            for voxel in ray_voxels:
                cells.setdefault(voxel, {}).setdefault(ray.event.envelope.station_id, set()).add(
                    _event_id(ray)
                )
        if prediction is None and self.diagnostics is not None:
            self.diagnostics.capture(window.key, search, cells)
        # Identical evidence in neighboring cells is one coarse hypothesis, not
        # several independent targets or additional station votes.
        regions: dict[tuple[tuple[m.StationId, tuple[m.EventId, ...]], ...], list[Voxel]] = {}
        for voxel, stations in cells.items():
            if (
                len(stations) >= self.policy.minimum_independent_stations
                and len(stations) >= self.policy.minimum_candidate_score
            ):
                signature = tuple(
                    (station, tuple(sorted(ids))) for station, ids in sorted(stations.items())
                )
                regions.setdefault(signature, []).append(voxel)
        candidates = []
        for signature, voxels in sorted(regions.items(), key=lambda item: (-len(item[0]), item[0])):
            lower = tuple(
                float(
                    search.min_enu_m[axis]
                    + min(cell[axis] for cell in voxels) * search.step_enu_m[axis]
                )
                for axis in range(3)
            )
            upper = tuple(
                float(
                    min(
                        search.max_enu_m[axis],
                        search.min_enu_m[axis]
                        + (max(cell[axis] for cell in voxels) + 1) * search.step_enu_m[axis],
                    )
                )
                for axis in range(3)
            )
            first = min(voxels)
            candidates.append(
                f.SpatialCandidate(
                    candidate_id=f"voxel/{first[0]}/{first[1]}/{first[2]}"
                    + ("" if prediction is None else f"/track/{prediction.track_id}"),
                    window=window.key,
                    method=f.CandidateMethod.COARSE_VOXEL,
                    origin=f.CandidateOrigin.UNGUIDED_SEARCH
                    if prediction is None
                    else f.CandidateOrigin.TRACK_PREDICTION_GATE,
                    prior_track_id=None if prediction is None else prediction.track_id,
                    coarse_position_enu_m=(
                        (lower[0] + upper[0]) / 2,
                        (lower[1] + upper[1]) / 2,
                        (lower[2] + upper[2]) / 2,
                    ),
                    region_min_enu_m=(lower[0], lower[1], lower[2]),
                    region_max_enu_m=(upper[0], upper[1], upper[2]),
                    independent_station_support=tuple(
                        f.IndependentStationSupport(
                            station_id=station, alternative_ray_event_ids=ids
                        )
                        for station, ids in signature
                    ),
                    score=float(len(signature)),
                    score_definition=_STATION_SCORE,
                )
            )
        return candidates, expansion_limited


@dataclass(frozen=True, kw_only=True)
class CandidateGenerationModule(ModuleShell):
    business_implemented = True
    policy: f.CandidatePolicy
    voxel: ports.SpatialCandidateStrategy
    direct: ports.SpatialCandidateStrategy

    def generate(
        self, window: f.SealedObservationWindow, predictions: tuple[f.TrackPrediction, ...]
    ) -> f.CandidateBatch:
        if self.policy.primary_method is f.CandidateMethod.COARSE_VOXEL:
            return self.voxel.propose(window, predictions)
        if self.policy.primary_method is f.CandidateMethod.DIRECT_GEOMETRY:
            return self.direct.propose(window, predictions)
        raise ValueError("unsupported candidate method")


@dataclass(frozen=True, kw_only=True)
class GeometryRefinementModule(ModuleShell):
    """Conditional weighted/robust localization with explicit exact constraints."""

    business_implemented = True
    center_session_id: m.SessionId
    world_frame_id: m.WorldFrameId
    policy: f.GeometryPolicy
    weights: ports.GeometryWeighting

    def __post_init__(self) -> None:
        validate_policy(self.policy)
        if (
            self.policy.minimum_independent_stations < 2
            or not 0 <= self.policy.minimum_intersection_angle_rad <= math.pi / 2
            or not math.isfinite(self.policy.maximum_condition_number)
            or self.policy.maximum_condition_number < 1
            or not math.isfinite(self.policy.maximum_residual_m)
            or self.policy.maximum_residual_m < 0
            or not math.isfinite(self.policy.robust_loss_scale_sigma)
            or self.policy.robust_loss_scale_sigma <= 0
            or self.policy.maximum_solver_iterations <= 0
            or not math.isfinite(self.policy.convergence_tolerance_m)
            or self.policy.convergence_tolerance_m <= 0
        ):
            raise ValueError("invalid geometry policy")

    def refine(
        self,
        window: f.SealedObservationWindow,
        association: f.AssociationBatch,
        predictions: tuple[f.TrackPrediction, ...],
    ) -> f.GeometryBatch:
        if self.policy.robust_loss_definition not in ("least_squares", "huber"):
            raise NotImplementedError("unsupported geometry loss")
        if association.window != window.key:
            raise ValueError("association and observation windows differ")
        if window.key.center_session_id != self.center_session_id or any(
            ray.event.world_frame_id != self.world_frame_id for ray in window.rays
        ):
            raise ValueError("geometry session or world frame differs from the window")
        validate_predictions(window, self.world_frame_id, predictions)
        rays = {_event_id(ray): ray for ray in window.rays}
        if any(
            event_id not in rays
            for group in association.selected_groups
            for event_id in group.supporting_ray_event_ids
        ):
            raise ValueError("association references a ray outside the sealed window")
        return f.GeometryBatch(
            window=window.key,
            results=tuple(
                self._refine_group(window, group, rays, predictions)
                for group in association.selected_groups
            ),
        )

    def _refine_group(
        self,
        window: f.SealedObservationWindow,
        group: f.AssociatedRayGroup,
        available: dict[m.EventId, f.AdmittedRay],
        predictions: tuple[f.TrackPrediction, ...],
    ) -> f.GeometryResult:
        chosen = tuple(available[event_id] for event_id in group.supporting_ray_event_ids)
        stations = tuple(ray.event.envelope.station_id for ray in chosen)
        reasons: list[f.GeometryReason] = []
        status = m.FusionStatus.REJECTED_INPUT
        position: Array | None = None
        residuals: tuple[float, ...] = ()
        condition: float | None = None
        minimum_angle: float | None = None
        motion: m.MotionAlignmentEvidence | None = None
        covariance: m.Matrix3x3 | None = None
        ray_weights = tuple(self.weights.weight(ray) for ray in chosen)
        fit: RayFit | None = None
        prior = (
            next((item for item in predictions if item.track_id == group.prior_track_id), None)
            if group.motion_alignment_model is f.MotionAlignmentModel.TRACK_PREDICTION
            else None
        )
        unaligned = (
            group.motion_alignment_model is f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR
        )
        alignment_valid = (
            (
                unaligned
                and group.reference_time_ns == _reference_time(chosen)
                and midpoint_span(chosen) <= self.policy.maximum_unaligned_span_ns
            )
            if chosen
            else False
        )
        if not unaligned:
            alignment_valid = (
                self.policy.motion_alignment_model is f.MotionAlignmentModel.TRACK_PREDICTION
                and prior is not None
                and prior.reference_time_ns == group.reference_time_ns
                and prior.valid_until_ns > group.reference_time_ns
                and prior.conditional_state_covariance_enu is not None
                and bool(chosen)
                and group.reference_time_ns == alignment_reference(chosen)
            )
        if (
            group.window != window.key
            or len(set(stations)) != len(stations)
            or stations != group.supporting_station_ids
        ):
            reasons.append(f.GeometryReason.INVALID_SUPPORT_GROUP)
        elif len(stations) < self.policy.minimum_independent_stations:
            reasons.append(f.GeometryReason.INSUFFICIENT_STATIONS)
            status = m.FusionStatus.INSUFFICIENT_STATIONS
        elif any(item.weight_scale <= 0 for item in ray_weights):
            reasons.append(f.GeometryReason.CALIBRATION_POLICY_EXCLUDED)
        elif any(ray.event.uncertainty.model != "tangent_gaussian_v1" for ray in chosen):
            reasons.append(f.GeometryReason.UNSUPPORTED_UNCERTAINTY_MODEL)
        elif not alignment_valid:
            reasons.append(f.GeometryReason.REFERENCE_TIME_ALIGNMENT_UNAVAILABLE)
        else:
            directions = [np.asarray(ray.event.ray_direction_enu_unit) for ray in chosen]
            angles = [
                math.atan2(
                    float(np.linalg.norm(np.cross(left, right))), abs(float(np.dot(left, right)))
                )
                for left, right in combinations(directions, 2)
            ]
            minimum_angle = min(angles)
            try:
                fitted, errors, depths, rank, singular = _line_fit(
                    chosen,
                    reference_ns=group.reference_time_ns,
                    velocity=None if prior is None else prior.velocity_enu_mps,
                )
                if rank == 3:
                    fit = fit_rays(
                        chosen,
                        self.policy,
                        tuple(item.weight_scale for item in ray_weights),
                        fitted,
                        reference_ns=group.reference_time_ns,
                        velocity=None if prior is None else prior.velocity_enu_mps,
                    )
                    fitted = fit.position
                    origins = (
                        np.asarray([ray.event.ray_origin_enu_m for ray in chosen])
                        if prior is None
                        else origins_at(chosen, group.reference_time_ns, prior.velocity_enu_mps)
                    )
                    vector = fitted - origins
                    unit = np.asarray(directions)
                    unit = unit / np.linalg.norm(unit, axis=1)[:, None]
                    depths = np.einsum("ni,ni->n", unit, vector)
                    errors = np.linalg.norm(vector - depths[:, None] * unit, axis=1)
            except np.linalg.LinAlgError:
                reasons.append(f.GeometryReason.SOLVER_FAILURE)
            else:
                residuals = tuple(float(value) for value in errors)
                if rank == 3:
                    condition = max(
                        float((singular[0] / singular[-1]) ** 2),
                        1.0 if fit is None else fit.condition,
                    )
                    if not math.isfinite(condition):
                        condition = None
                if rank < 3 or max(angles) < self.policy.minimum_intersection_angle_rad:
                    reasons.append(f.GeometryReason.NEAR_PARALLEL_RAYS)
                if condition is None or condition > self.policy.maximum_condition_number:
                    reasons.append(f.GeometryReason.POOR_CONDITION)
                if float(errors.max()) > self.policy.maximum_residual_m:
                    reasons.append(f.GeometryReason.EXCESSIVE_RESIDUAL)
                if np.any(depths < 0):
                    reasons.append(f.GeometryReason.NONFORWARD_SOLUTION)
                if fit is not None:
                    if not fit.exact_consistent:
                        reasons.append(f.GeometryReason.INCONSISTENT_EXACT_CONSTRAINTS)
                    if not fit.converged:
                        reasons.append(f.GeometryReason.SOLVER_NOT_CONVERGED)
                if reasons:
                    status = m.FusionStatus.GEOMETRY_DEGENERATE
                else:
                    assert fit is not None
                    status, position = m.FusionStatus.LOCATED, fitted
                    covariance = matrix3(fit.covariance)
                    if prior is not None:
                        assert prior.conditional_state_covariance_enu is not None
                        projection = fit.velocity_projection
                        velocity_root = covariance_factor(
                            prior.conditional_state_covariance_enu, 6, "prediction covariance"
                        )[3:, :]
                        projected = projection @ velocity_root
                        covariance = matrix3(fit.covariance + projected @ projected.T)
                        motion = m.MotionAlignmentEvidence(
                            prior_track_id=prior.track_id,
                            velocity_enu_mps=prior.velocity_enu_mps,
                            velocity_projection_s=matrix3(projection),
                            independent_position_covariance_enu_m2=matrix3(fit.covariance),
                        )
        measurement = m.FusionMeasurement(
            center_session_id=self.center_session_id,
            measurement_id=m.MeasurementId(
                f"measurement/{window.key.start_ns}/{group.hypothesis_id}"
            ),
            clock_domain_id=window.key.capture_clock_domain_id,
            world_frame_id=self.world_frame_id,
            reference_time_ns=group.reference_time_ns,
            support_exposure_span_ns=group.support_exposure_span_ns,
            status=status,
            position_enu_m=None
            if position is None
            else (float(position[0]), float(position[1]), float(position[2])),
            conditional_position_covariance_enu_m2=covariance,
            motion_alignment=motion,
            supporting_ray_event_ids=group.supporting_ray_event_ids,
            supporting_station_ids=stations,
            residuals_m=residuals,
            geometry_condition=condition,
            minimum_intersection_angle_rad=minimum_angle,
            association_hypothesis_id=group.hypothesis_id,
            reason_codes=tuple(reason.value for reason in reasons),
        )
        return f.GeometryResult(
            window=window.key,
            hypothesis_id=group.hypothesis_id,
            measurement=measurement,
            reasons=tuple(reasons),
            motion_alignment_model=group.motion_alignment_model,
            ray_weights=ray_weights,
            normalized_residuals=() if fit is None else fit.normalized_residuals,
            robust_weights=() if fit is None else fit.robust_weights,
            solver_iterations=0 if fit is None else fit.iterations,
            solver_converged=fit is not None and fit.converged,
            shared_installation_state_refs=tuple(
                dict.fromkeys(ray.event.uncertainty.shared_installation_state_ref for ray in chosen)
            ),
            shared_calibration_solution_refs=tuple(
                dict.fromkeys(
                    ray.event.uncertainty.shared_calibration_solution_ref for ray in chosen
                )
            ),
        )
