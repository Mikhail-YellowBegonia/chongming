"""Exhaustive support groups and bounded-diameter post-intersection suppression."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from itertools import combinations, product

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.modules.fusion_geometry import _STATION_SCORE, _exposure_span, _reference_time


def candidate_groups(
    batch: f.CandidateBatch, minimum_stations: int = 2
) -> dict[tuple[m.EventId, ...], list[str]]:
    """One ray per station; duplicate voxel evidence never repeats a group."""
    groups: dict[tuple[m.EventId, ...], list[str]] = {}
    for candidate in batch.candidates:
        support = sorted(candidate.independent_station_support, key=lambda s: s.station_id)
        for count in range(minimum_stations, len(support) + 1):
            for stations in combinations(support, count):
                for ids in product(*(sorted(set(s.alternative_ray_event_ids)) for s in stations)):
                    groups.setdefault(ids, []).append(candidate.candidate_id)
    return groups


@dataclass(frozen=True, kw_only=True)
class ExhaustiveAssociationModule(ModuleShell):
    """Publish hypotheses for independent refinement, not exclusive assignments."""

    business_implemented = True
    minimum_stations: int = 2

    def associate(
        self,
        window: f.SealedObservationWindow,
        candidates: f.CandidateBatch,
        predictions: tuple[f.TrackPrediction, ...],
    ) -> f.AssociationBatch:
        if candidates.window != window.key:
            raise ValueError("candidate and observation windows differ")
        rays = {
            (
                r.event.envelope.station_id,
                r.event.envelope.station_session_id,
                r.event.envelope.sequence_no,
            ): r
            for r in window.rays
        }
        groups = []
        for ids, candidate_ids in candidate_groups(candidates, self.minimum_stations).items():
            chosen = tuple(rays[event_id] for event_id in ids)
            groups.append(
                f.AssociatedRayGroup(
                    hypothesis_id="exhaustive/" + hashlib.sha256(repr(ids).encode()).hexdigest(),
                    candidate_id=candidate_ids[0],
                    window=window.key,
                    reference_time_ns=_reference_time(chosen),
                    support_exposure_span_ns=_exposure_span(chosen),
                    supporting_ray_event_ids=ids,
                    supporting_station_ids=tuple(ray.event.envelope.station_id for ray in chosen),
                    prior_track_id=None,
                    motion_alignment_model=f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR,
                    score=float(len(chosen)),
                    score_definition=_STATION_SCORE,
                )
            )
        return f.AssociationBatch(
            window=window.key,
            selected_groups=tuple(groups),
            conflicts=(),
            unassigned_ray_event_ids=tuple(
                sorted(set(rays) - {i for g in groups for i in g.supporting_ray_event_ids})
            ),
            status=f.AssociationBatchStatus.ASSOCIATIONS_SELECTED
            if groups
            else f.AssociationBatchStatus.NO_VALID_ASSOCIATION,
        )


def measurement_quality(item: m.FusionMeasurement) -> tuple[float, float, float, str]:
    return (
        -len(set(item.supporting_station_ids)),
        max(item.residuals_m, default=math.inf),
        item.geometry_condition if item.geometry_condition is not None else math.inf,
        str(item.measurement_id),
    )


def exclusive_tracking_inputs(
    measurements: tuple[m.FusionMeasurement, ...],
) -> tuple[tuple[m.FusionMeasurement, ...], tuple[f.SuppressedMeasurement, ...]]:
    """Preserve fusion outputs while preventing repeated evidence in track updates."""
    kept = []
    exclusions = []
    owners: dict[m.EventId, m.FusionMeasurement] = {}
    for item in sorted(measurements, key=measurement_quality):
        if item.status is not m.FusionStatus.LOCATED:
            kept.append(item)
            continue
        conflict = next(
            (owners[event] for event in item.supporting_ray_event_ids if event in owners), None
        )
        if conflict is not None:
            assert item.position_enu_m is not None and conflict.position_enu_m is not None
            exclusions.append(
                f.SuppressedMeasurement(
                    measurement=item,
                    representative_measurement_id=conflict.measurement_id,
                    distance_m=math.dist(item.position_enu_m, conflict.position_enu_m),
                    reason="shared_ray_conflict",
                )
            )
        else:
            kept.append(item)
            owners.update((event, item) for event in item.supporting_ray_event_ids)
    return tuple(kept), tuple(exclusions)


def suppress_duplicates(
    measurements: tuple[m.FusionMeasurement, ...],
    distance_m: float,
) -> tuple[tuple[m.FusionMeasurement, ...], tuple[f.SuppressedMeasurement, ...]]:
    """Quality-ordered complete-link groups; retain an original representative."""
    if not math.isfinite(distance_m) or distance_m <= 0:
        raise ValueError("duplicate distance must be finite and positive")

    def point(item: m.FusionMeasurement) -> m.Vec3:
        assert item.position_enu_m is not None
        return item.position_enu_m

    clusters: list[list[m.FusionMeasurement]] = []
    kept = [item for item in measurements if item.status is not m.FusionStatus.LOCATED]
    suppressed = []
    for item in sorted(
        (i for i in measurements if i.status is m.FusionStatus.LOCATED), key=measurement_quality
    ):
        assert item.position_enu_m is not None
        matching = []
        for index, cluster in enumerate(clusters):
            representative = cluster[0]
            if (
                item.center_session_id,
                item.clock_domain_id,
                item.world_frame_id,
                item.reference_time_ns,
            ) != (
                representative.center_session_id,
                representative.clock_domain_id,
                representative.world_frame_id,
                representative.reference_time_ns,
            ):
                continue
            if all(math.dist(point(item), point(member)) <= distance_m for member in cluster):
                matching.append(index)
        if not matching:
            clusters.append([item])
            kept.append(item)
            continue
        index = min(
            matching,
            key=lambda i: (math.dist(point(item), point(clusters[i][0])), i),
        )
        representative = clusters[index][0]
        suppressed.append(
            f.SuppressedMeasurement(
                measurement=item,
                representative_measurement_id=representative.measurement_id,
                distance_m=math.dist(point(item), point(representative)),
                reason="duplicate_location",
            )
        )
        clusters[index].append(item)
    return tuple(sorted(kept, key=lambda i: (i.reference_time_ns, str(i.measurement_id)))), tuple(
        suppressed
    )
