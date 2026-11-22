"""Offline operational attribution; truth labels never enter online algorithms."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations, product
from typing import Any

import numpy as np

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.metrics import annotate, projection_pixel_gates
from chongming_scorer.storage import RunOutputPackage, TestScenePackage


def miss_owner(
    *,
    emitted_stations: int,
    admitted_stations: int,
    raw_can_hit: bool,
    geometry_can_hit: bool,
    correct_groups_examined: bool,
    minimum_stations: int = 2,
) -> str:
    if emitted_stations < minimum_stations:
        return "device:insufficient_correct_stations"
    if admitted_stations < minimum_stations:
        return "center:ingress_rejection"
    if not raw_can_hit:
        return "device:triangulation_localization_limit"
    if geometry_can_hit:
        return "center:spatial_suppression_or_assignment"
    if correct_groups_examined:
        return "center:geometry_quality_or_estimation"
    return "center:candidate_coverage"


def false_output_owner(kind: str, labels: Sequence[Mapping[str, Any]]) -> str:
    if kind == "duplicate":
        return "center:duplicate_location"
    if any(label["kind"] == "false_alarm" for label in labels):
        return "device:false_positive_propagated"
    targets = {label["target_id"] for label in labels if label.get("target_id") is not None}
    if len(targets) > 1:
        return "center:cross_target_combination"
    return "device:localization_error_propagated"


def unweighted_line_fit(
    rays: Sequence[tuple[tuple[float, float, float], tuple[float, float, float]]],
) -> tuple[tuple[float, float, float], int]:
    """Centered unweighted 3D ray intersection least-squares solver."""
    if len(rays) < 2:
        raise ValueError("line fit requires at least two rays")
    origins = np.asarray([origin for origin, _ in rays], dtype=np.float64)
    directions = np.asarray([direction for _, direction in rays], dtype=np.float64)
    norms = np.linalg.norm(directions, axis=1)
    if np.any(norms <= 0) or not np.all(np.isfinite(norms)):
        raise np.linalg.LinAlgError("ray directions must be finite and non-zero")
    directions = directions / norms[:, None]
    projectors = np.eye(3) - directions[:, :, None] * directions[:, None, :]
    anchor = origins.mean(axis=0)
    design = projectors.reshape(-1, 3)
    offsets = np.einsum("nij,nj->ni", projectors, origins - anchor).reshape(-1)
    delta, _, rank, singular = np.linalg.lstsq(design, offsets, rcond=None)
    position = anchor + delta
    if not np.all(np.isfinite(position)) or not np.all(np.isfinite(singular)):
        raise np.linalg.LinAlgError("nonfinite continuous ray solution")
    return (float(position[0]), float(position[1]), float(position[2])), int(rank)


def _label_edge_observations(
    scene: TestScenePackage,
    batches: Sequence[online_pb2.DetectionBatch],
    *,
    gate_px: float,
    size_k_px: float | None,
    warmup_s: float,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, int], dict[str, Any]],
]:
    projections_by_frame: dict[str, list[truth_pb2.StationTargetProjection]] = defaultdict(list)
    projections_by_time: dict[tuple[str, int], list[truth_pb2.StationTargetProjection]] = (
        defaultdict(list)
    )
    for truth_frame in scene.iter_truth_frames():
        for proj in truth_frame.projections:
            if proj.HasField("frame_id") and proj.frame_id:
                projections_by_frame[proj.frame_id].append(proj)
            projections_by_time[(proj.station_id, truth_frame.experiment_time_ns)].append(proj)

    origins: dict[str, int] = {}
    for batch in batches:
        station = batch.main_frame_ref.station_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2
        if station not in origins or ns < origins[station]:
            origins[station] = ns

    warmup_ns = round(warmup_s * 1e9)
    by_track: dict[tuple[str, str], dict[str, Any]] = {}
    by_index: dict[tuple[str, int], dict[str, Any]] = {}
    for batch in batches:
        station = batch.main_frame_ref.station_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2
        if ns - origins[station] < warmup_ns:
            continue
        frame_id = batch.main_frame_ref.frame_id
        projs = projections_by_frame.get(frame_id) or projections_by_time.get((station, ns), [])
        targets = {
            proj.target_id: (proj.projected_centroid_px.x, proj.projected_centroid_px.y)
            for proj in projs
            if proj.occlusion_status != truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV
        }
        gates = projection_pixel_gates(projs, gate_px, size_k_px)
        predictions = [(obs.centroid_px.x, obs.centroid_px.y) for obs in batch.observations]
        annotated = annotate(
            {
                "eligible_targets_px": targets,
                "target_gates_px": gates,
                "predictions_px": predictions,
            },
            gate_px,
        )
        target_entries = annotated["targets"]
        for idx, (obs, pred) in enumerate(
            zip(batch.observations, annotated["predictions"], strict=True)
        ):
            target_index = pred["target"]
            target_id = target_entries[target_index]["id"] if target_index >= 0 else None
            if target_id is None and pred["kind"] == "duplicate":
                choices = [t for t in target_entries if math.dist(pred["xy"], t["xy"]) <= t["gate"]]
                if choices:
                    target_id = min(
                        choices, key=lambda t: (math.dist(pred["xy"], t["xy"]), t["id"])
                    )["id"]
            label = {
                "target_id": target_id,
                "kind": pred["kind"],
                "pixel_position": pred["xy"],
            }
            by_index[(frame_id, idx)] = label
            if obs.HasField("local_track_id") and obs.local_track_id:
                by_track[(frame_id, obs.local_track_id)] = label
    return by_track, by_index


def attribute_messages(
    scene: TestScenePackage,
    edge_messages: Iterable[online_pb2.BusMessage],
    center_messages: Iterable[online_pb2.BusMessage],
    center_frame_records: Sequence[Mapping[str, Any]],
    *,
    gate_px: float = 10.0,
    size_k_px: float | None = 0.5,
    precise_k_m: float = 0.7,
    minimum_stations: int = 2,
    warmup_s: float = 15.0,
) -> dict[str, Any]:
    """Attribute center misses and false outputs across in-memory edge and center streams."""
    batches: list[online_pb2.DetectionBatch] = []
    ray_events: list[online_pb2.RayEvent] = []
    for msg in edge_messages:
        kind = msg.WhichOneof("payload")
        if kind == "detection_batch":
            batches.append(msg.detection_batch)
        elif kind == "ray_event":
            ray_events.append(msg.ray_event)

    by_track, by_index = _label_edge_observations(
        scene,
        batches,
        gate_px=gate_px,
        size_k_px=size_k_px,
        warmup_s=warmup_s,
    )

    labels_by_event: dict[tuple[str, str, int], dict[str, Any]] = {}
    emitted: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    emitted_rays: dict[int, dict[str, dict[str, list[online_pb2.RayEvent]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    frame_ray_counter: dict[str, int] = defaultdict(int)
    for ray in ray_events:
        idx = frame_ray_counter[ray.frame_id]
        frame_ray_counter[ray.frame_id] += 1
        label: dict[str, Any] | None = None
        if ray.HasField("local_track_id") and ray.local_track_id:
            label = by_track.get((ray.frame_id, ray.local_track_id))
        if label is None:
            label = by_index.get((ray.frame_id, idx))
        if label is None:
            continue
        event_id = (
            ray.envelope.station_id,
            ray.envelope.station_session_id,
            int(ray.envelope.sequence_no),
        )
        labels_by_event[event_id] = label
        if label["target_id"] is not None:
            ns = (ray.capture_time.exposure_start_ns + ray.capture_time.exposure_end_ns) // 2
            emitted[ns][label["target_id"]].add(ray.envelope.station_id)
            emitted_rays[ns][label["target_id"]][ray.envelope.station_id].append(ray)

    measurements_by_time: dict[int, list[online_pb2.FusionMeasurement]] = defaultdict(list)
    seen_ids: set[str] = set()
    for msg in center_messages:
        if msg.WhichOneof("payload") != "fusion_measurement":
            continue
        meas = msg.fusion_measurement
        if meas.measurement_id in seen_ids:
            continue
        seen_ids.add(meas.measurement_id)
        measurements_by_time[meas.reference_time_ns].append(meas)

    misses: Counter[str] = Counter()
    false_outputs: Counter[str] = Counter()
    contributors: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()

    for frame in center_frame_records:
        ns = int(frame["reference_time_ns"])
        all_meas = measurements_by_time.get(ns, [])
        located = [m for m in all_meas if m.status == common_pb2.FUSION_STATUS_LOCATED]
        for meas in all_meas:
            if meas.status != common_pb2.FUSION_STATUS_LOCATED:
                for reason in meas.reason_codes:
                    rejection_reasons[reason] += 1

        meas_by_support: dict[str, list[online_pb2.FusionMeasurement]] = defaultdict(list)
        for meas in all_meas:
            support_labels = [
                labels_by_event[(eid.station_id, eid.station_session_id, int(eid.sequence_no))]
                for eid in meas.supporting_ray_event_ids
                if (eid.station_id, eid.station_session_id, int(eid.sequence_no)) in labels_by_event
            ]
            target_ids = {s["target_id"] for s in support_labels}
            if len(support_labels) == len(meas.supporting_ray_event_ids) and len(target_ids) == 1:
                only = next(iter(target_ids))
                if only is not None:
                    meas_by_support[only].append(meas)

        for target, truth_info in frame["targets"].items():
            if truth_info["precise_hit"]:
                continue
            diagonal = float(truth_info["aabb_diagonal_m"])
            gate = precise_k_m * diagonal
            station_rays = emitted_rays[ns][target]
            raw_can_hit = False
            for count in range(minimum_stations, len(station_rays) + 1):
                for stations in combinations(sorted(station_rays), count):
                    for chosen in product(*(station_rays[s] for s in stations)):
                        pairs = [
                            (
                                (
                                    r.ray_origin_enu_m.x,
                                    r.ray_origin_enu_m.y,
                                    r.ray_origin_enu_m.z,
                                ),
                                (
                                    r.ray_direction_enu_unit.x,
                                    r.ray_direction_enu_unit.y,
                                    r.ray_direction_enu_unit.z,
                                ),
                            )
                            for r in chosen
                        ]
                        try:
                            pos, rank = unweighted_line_fit(pairs)
                        except np.linalg.LinAlgError:
                            continue
                        if rank == 3 and math.dist(pos, truth_info["position_enu_m"]) <= gate:
                            raw_can_hit = True
            groups = meas_by_support.get(target, [])
            geometry_can_hit = any(
                m.status == common_pb2.FUSION_STATUS_LOCATED
                and math.dist(
                    (m.position_enu_m.x, m.position_enu_m.y, m.position_enu_m.z),
                    truth_info["position_enu_m"],
                )
                <= gate
                for m in groups
            )
            owner = miss_owner(
                emitted_stations=len(emitted[ns][target]),
                admitted_stations=len(station_rays),
                raw_can_hit=raw_can_hit,
                geometry_can_hit=geometry_can_hit,
                correct_groups_examined=bool(groups),
                minimum_stations=minimum_stations,
            )
            misses[owner] += 1

        targets_3d = {
            key: tuple(value["position_enu_m"]) for key, value in frame["targets"].items()
        }
        gates_3d = {
            key: precise_k_m * float(value["aabb_diagonal_m"])
            for key, value in frame["targets"].items()
        }
        annotated_3d = annotate(
            {
                "eligible_targets_px": targets_3d,
                "target_gates_px": gates_3d,
                "predictions_px": [
                    (m.position_enu_m.x, m.position_enu_m.y, m.position_enu_m.z) for m in located
                ],
            },
            1.0,
        )
        ignored_pts = frame.get("ignored_targets_enu_m", [])
        ignored_diags = frame.get("ignored_target_diagonals_m", [])
        for meas, pred_label in zip(located, annotated_3d["predictions"], strict=True):
            if pred_label["kind"] == "matched":
                continue
            pos = (meas.position_enu_m.x, meas.position_enu_m.y, meas.position_enu_m.z)
            if pred_label["kind"] == "false_alarm" and any(
                math.dist(pos, p) <= precise_k_m * float(d)
                for p, d in zip(ignored_pts, ignored_diags, strict=True)
            ):
                continue
            support_labels = [
                labels_by_event[(eid.station_id, eid.station_session_id, int(eid.sequence_no))]
                for eid in meas.supporting_ray_event_ids
                if (eid.station_id, eid.station_session_id, int(eid.sequence_no)) in labels_by_event
            ]
            owner = false_output_owner(pred_label["kind"], support_labels)
            false_outputs[owner] += 1
            contributors["accepted_by_center"] += 1
            contributors["contains_device_false_alarm"] += int(
                any(s["kind"] == "false_alarm" for s in support_labels)
            )
            contributors["contains_cross_target_support"] += int(
                len({s["target_id"] for s in support_labels if s["target_id"] is not None}) > 1
            )

    return {
        "format": "integrated-attribution-v1",
        "misses": dict(misses),
        "false_outputs": dict(false_outputs),
        "false_output_contributors": dict(contributors),
        "geometry_rejections_scored_windows": dict(rejection_reasons),
    }


def attribute_run(
    scene: TestScenePackage,
    run: RunOutputPackage,
    center_frame_records: Sequence[Mapping[str, Any]],
    *,
    gate_px: float = 10.0,
    size_k_px: float | None = 0.5,
    precise_k_m: float = 0.7,
    minimum_stations: int = 2,
    warmup_s: float = 15.0,
) -> dict[str, Any]:
    """Attribute center misses and false outputs across edge and center streams."""
    if not run.has_edge_events or not run.has_center_output:
        raise ValueError("attribution requires both edge_events.pb.zst and center_output.pb.zst")
    return attribute_messages(
        scene,
        run.iter_edge_events(),
        run.iter_center_output(),
        center_frame_records,
        gate_px=gate_px,
        size_k_px=size_k_px,
        precise_k_m=precise_k_m,
        minimum_stations=minimum_stations,
        warmup_s=warmup_s,
    )
