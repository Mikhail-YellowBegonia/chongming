"""Core scoring engine for Edge, Center, and Integrated Run Output packages."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from chongming_proto.online import common_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.attribution import attribute_run
from chongming_scorer.difficulty import score_difficulty_strata
from chongming_scorer.metrics import (
    CenterSizeMetrics,
    DetectionMetrics,
    projection_pixel_gates,
)
from chongming_scorer.performance import summarize_edge_timings
from chongming_scorer.storage import RunOutputPackage, TestScenePackage


def build_score_event(
    run_id: str,
    experiment_time_ns: int,
    matched_errors: Sequence[float],
    *,
    unmatched_count: int,
    unevaluable_count: int,
    error_unit: str,
    extra_metrics: Mapping[str, tuple[float | None, str]] | None = None,
) -> truth_pb2.ScoreEvent:
    """Create a ScoreEvent; error statistics are defined on matched samples only."""
    event = truth_pb2.ScoreEvent(
        run_id=run_id,
        experiment_time_ns=int(experiment_time_ns),
        matched_count=len(matched_errors),
        unmatched_count=int(unmatched_count),
        unevaluable_count=int(unevaluable_count),
        error_unit=error_unit,
    )
    if matched_errors:
        event.mean_error = math.fsum(matched_errors) / len(matched_errors)
        event.rmse = math.sqrt(math.fsum(err * err for err in matched_errors) / len(matched_errors))
        event.max_error = max(matched_errors)
    if extra_metrics:
        for name, (value, unit) in sorted(extra_metrics.items()):
            metric = truth_pb2.ScoreMetric(name=name, unit=unit)
            if value is not None:
                metric.value = float(value)
            event.extra_metrics.append(metric)
    return event


def score_edge_run(
    scene: TestScenePackage,
    run: RunOutputPackage,
    *,
    gate_px: float = 10.0,
    size_k: float | None = 0.5,
    warmup_s: float = 15.0,
    include_difficulty: bool = True,
) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
    """Score Edge DetectionBatch outputs against immutable Test Scene truth."""
    if not 0 <= warmup_s < math.inf:
        raise ValueError("warmup_s must be finite and nonnegative")
    DetectionMetrics(gate_px, "px")
    projection_pixel_gates((), gate_px, size_k)
    run.verify_matches_scene(scene)
    if not run.has_edge_events:
        raise FileNotFoundError("Run Output is missing edge_events.pb.zst")

    frame_index: dict[str, truth_pb2.FrameIndexEntry] = {}
    for entry in scene.iter_frame_index():
        if entry.camera_role == common_pb2.CAMERA_ROLE_MAIN:
            frame_index[entry.frame_id] = entry

    projections_by_frame: dict[str, list[truth_pb2.StationTargetProjection]] = defaultdict(list)
    projections_by_time: dict[tuple[str, int], list[truth_pb2.StationTargetProjection]] = (
        defaultdict(list)
    )
    truth_times: set[int] = set()
    for truth_frame in scene.iter_truth_frames():
        truth_times.add(truth_frame.experiment_time_ns)
        for proj in truth_frame.projections:
            if proj.HasField("frame_id") and proj.frame_id:
                projections_by_frame[proj.frame_id].append(proj)
            projections_by_time[(proj.station_id, truth_frame.experiment_time_ns)].append(proj)

    batches = [
        msg.detection_batch
        for msg in run.iter_edge_events()
        if msg.WhichOneof("payload") == "detection_batch"
    ]
    if not batches:
        raise ValueError("edge_events.pb.zst contains no DetectionBatch messages")

    origins: dict[str, int] = {}
    for batch in batches:
        if batch.processing_status != common_pb2.PROCESSING_STATUS_PROCESSED:
            raise RuntimeError(f"edge frame {batch.main_frame_ref.frame_id!r} failed processing")
        station = batch.main_frame_ref.station_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2
        if station not in origins or ns < origins[station]:
            origins[station] = ns

    warmup_ns = round(warmup_s * 1e9)
    aggregate = DetectionMetrics(gate_px, "px")
    station_metrics: dict[str, DetectionMetrics] = {
        station: DetectionMetrics(gate_px, "px") for station in sorted(origins)
    }
    processed: dict[str, int] = dict.fromkeys(origins, 0)
    warmup_frames: dict[str, int] = dict.fromkeys(origins, 0)
    frame_records: list[dict[str, Any]] = []
    per_time_errors: dict[int, list[float]] = defaultdict(list)
    per_time_fp: dict[int, int] = defaultdict(int)
    per_time_fn: dict[int, int] = defaultdict(int)
    per_time_ignored: dict[int, int] = defaultdict(int)

    for batch in batches:
        station = batch.main_frame_ref.station_id
        frame_id = batch.main_frame_ref.frame_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2
        indexed = frame_index.get(frame_id)
        if indexed is not None:
            indexed_ns = (
                indexed.capture_time.exposure_start_ns + indexed.capture_time.exposure_end_ns
            ) // 2
            if (
                indexed.station_id != station
                or indexed.station_session_id != batch.main_frame_ref.station_session_id
                or indexed_ns != ns
            ):
                raise ValueError(
                    f"frame identity or capture time differs from scene index for {frame_id!r}"
                )
        elif ns not in truth_times:
            raise ValueError(f"edge frame {frame_id!r} has no matching truth frame")

        processed[station] += 1
        excluded = ns - origins[station] < warmup_ns
        record: dict[str, Any] = {
            "station_id": station,
            "frame_id": frame_id,
            "time_ns": ns,
            "warmup_excluded": excluded,
            "decode_ms": batch.decode_duration_ns / 1e6,
            "cv_ms": batch.cv_duration_ns / 1e6,
            "observation_count": len(batch.observations),
            "predictions_px": [
                [obs.centroid_px.x, obs.centroid_px.y] for obs in batch.observations
            ],
        }
        if excluded:
            warmup_frames[station] += 1
        else:
            projs = projections_by_frame.get(frame_id) or projections_by_time.get((station, ns), [])
            targets: dict[str, tuple[float, ...]] = {}
            for proj in projs:
                if proj.occlusion_status == truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV:
                    continue
                if not proj.HasField("projected_centroid_px"):
                    raise ValueError(
                        f"in-FOV projection missing projected_centroid_px for {proj.target_id!r}"
                    )
                targets[proj.target_id] = (
                    proj.projected_centroid_px.x,
                    proj.projected_centroid_px.y,
                )
            gates = projection_pixel_gates(projs, gate_px, size_k)
            predictions = [(obs.centroid_px.x, obs.centroid_px.y) for obs in batch.observations]
            row_result = station_metrics[station].add(predictions, targets, target_gates=gates)
            aggregate.add(predictions, targets, target_gates=gates)
            record["eligible_targets_px"] = targets
            record["target_gates_px"] = gates
            record.update(row_result)
            per_time_errors[ns].extend(row_result["matched_errors"])
            per_time_fp[ns] += row_result["fp"]
            per_time_fn[ns] += row_result["fn"]
            per_time_ignored[ns] += row_result["ignored_predictions"]
        frame_records.append(record)

    score_events = [
        build_score_event(
            run.run_id,
            ns,
            per_time_errors[ns],
            unmatched_count=per_time_fp[ns],
            unevaluable_count=per_time_ignored[ns],
            error_unit="px",
            extra_metrics={"missed_targets": (float(per_time_fn[ns]), "count")},
        )
        for ns in sorted(per_time_fp)
    ]

    timings = summarize_edge_timings(batches)
    difficulty = (
        score_difficulty_strata(scene, frame_records, gate_px=gate_px)
        if include_difficulty
        else None
    )
    report: dict[str, Any] = {
        "format": "chongming-scorer-edge-v1",
        "status": "complete",
        "run_id": run.run_id,
        "test_scene_sha256": scene.content_sha256,
        "warmup_s": warmup_s,
        "gate_px": gate_px,
        "size_k": size_k,
        "gate_rule": (
            "max(gate_px, size_k * projected_diagonal_px)"
            if size_k is not None
            else "fixed gate_px"
        ),
        "responsibility": "main-camera target center in FOV; occlusion ignored",
        "matching": "maximum cardinality then minimum total pixel distance within gate",
        "aggregate": aggregate.summary(),
        "stations": {
            station: {
                "metrics": station_metrics[station].summary(),
                "processed_frames": processed[station],
                "warmup_frames": warmup_frames[station],
                **timings["stations"][station],
            }
            for station in sorted(origins)
        },
        "timings": timings,
    }
    if difficulty is not None:
        report["difficulty"] = difficulty
    return score_events, report, frame_records


def score_center_run(
    scene: TestScenePackage,
    run: RunOutputPackage,
    *,
    gate_m: float = 10.0,
    size_k: float | None = None,
    miss_k: float = 3.0,
    warmup_s: float = 0.0,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
    """Score Center FusionMeasurement outputs against immutable Test Scene truth."""
    if minimum_stations < 2:
        raise ValueError("minimum_stations must be at least two")
    if not 0 <= warmup_s < math.inf:
        raise ValueError("warmup_s must be finite and nonnegative")
    DetectionMetrics(gate_m, "m")
    size_metrics = CenterSizeMetrics(size_k, miss_k) if size_k is not None else None
    metrics = DetectionMetrics(gate_m, "m")
    run.verify_matches_scene(scene)
    if not run.has_center_output:
        raise FileNotFoundError("Run Output is missing center_output.pb.zst")

    origin_ns = scene.start_time_ns if warmup_origin_ns is None else warmup_origin_ns
    truth_by_time: dict[int, truth_pb2.TruthFrame] = {
        frame.experiment_time_ns: frame for frame in scene.iter_truth_frames()
    }

    predictions: dict[int, list[tuple[float, ...]]] = defaultdict(list)
    seen_measurements: dict[str, bytes] = {}
    schedule_times: set[int] = set()
    nonlocated = 0

    for msg in run.iter_center_output():
        kind = msg.WhichOneof("payload")
        if kind == "progress_report":
            report_msg = msg.progress_report
            if report_msg.source_role == common_pb2.COMPONENT_ROLE_CENTER:
                schedule_times.add(report_msg.completed_time_ns)
        elif kind == "fusion_measurement":
            meas = msg.fusion_measurement
            serialized = meas.SerializeToString()
            previous = seen_measurements.get(meas.measurement_id)
            if previous is not None:
                if previous != serialized:
                    raise ValueError(
                        f"conflicting duplicate measurement ID {meas.measurement_id!r}"
                    )
                continue
            seen_measurements[meas.measurement_id] = serialized
            if meas.status == common_pb2.FUSION_STATUS_LOCATED and meas.HasField("position_enu_m"):
                pos = (
                    meas.position_enu_m.x,
                    meas.position_enu_m.y,
                    meas.position_enu_m.z,
                )
                predictions[meas.reference_time_ns].append(pos)
            else:
                nonlocated += 1

    if not set(predictions).issubset(schedule_times):
        raise ValueError(
            "measurement reference time has no sealed-window schedule; recall is undefined"
        )

    warmup_ns = round(warmup_s * 1e9)
    excluded = 0
    records: list[dict[str, Any]] = []
    score_events: list[truth_pb2.ScoreEvent] = []

    for ns in sorted(schedule_times):
        if not scene.start_time_ns <= ns <= scene.end_time_ns:
            raise ValueError("center reference time outside recorded scenario")
        if ns - origin_ns < warmup_ns:
            excluded += 1
            continue
        truth_frame = truth_by_time.get(ns)
        if truth_frame is None:
            raise ValueError(f"missing TruthFrame for evaluated reference_time_ns={ns}")

        visible: dict[str, set[str]] = defaultdict(set)
        for proj in truth_frame.projections:
            if proj.occlusion_status == truth_pb2.OCCLUSION_STATUS_UNSPECIFIED:
                raise ValueError(
                    f"projection for target {proj.target_id!r} has unspecified occlusion_status"
                )
            if proj.occlusion_status != truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV:
                visible[proj.target_id].add(proj.station_id)

        eligible: dict[str, tuple[float, ...]] = {}
        ignored: list[tuple[float, ...]] = []
        diagonals: dict[str, float] = {}
        ignored_diagonals: list[float] = []
        states: dict[str, dict[str, Any]] = {}

        for target in truth_frame.targets:
            pos = (
                target.position_enu_m.x,
                target.position_enu_m.y,
                target.position_enu_m.z,
            )
            station_set = visible.get(target.target_id, set())
            if len(station_set) >= minimum_stations:
                eligible[target.target_id] = pos
                if size_metrics is not None:
                    if (
                        not target.HasField("body_aabb_diagonal_m")
                        or target.body_aabb_diagonal_m <= 0
                    ):
                        raise ValueError(
                            f"TargetTruth {target.target_id!r} missing positive body_aabb_diagonal_m"
                        )
                    diagonals[target.target_id] = target.body_aabb_diagonal_m
                states[target.target_id] = {
                    "velocity_enu_mps": [
                        target.velocity_enu_mps.x,
                        target.velocity_enu_mps.y,
                        target.velocity_enu_mps.z,
                    ],
                    "coverage_stations": sorted(station_set),
                }
            else:
                ignored.append(pos)
                if size_metrics is not None:
                    if (
                        not target.HasField("body_aabb_diagonal_m")
                        or target.body_aabb_diagonal_m <= 0
                    ):
                        raise ValueError(
                            f"TargetTruth {target.target_id!r} missing positive body_aabb_diagonal_m"
                        )
                    ignored_diagonals.append(target.body_aabb_diagonal_m)

        preds = predictions.get(ns, [])
        if size_metrics is None:
            result = metrics.add(preds, eligible, ignored=ignored)
            primary = result
        else:
            result = size_metrics.add(preds, eligible, diagonals, ignored, ignored_diagonals)
            result["ignored_targets_enu_m"] = ignored
            result["ignored_target_diagonals_m"] = ignored_diagonals
            for key, state in states.items():
                result["targets"][key].update(state)
            primary = result["precise"]

        records.append({"reference_time_ns": ns, **result})
        score_events.append(
            build_score_event(
                run.run_id,
                ns,
                primary["matched_errors"],
                unmatched_count=primary["fp"],
                unevaluable_count=primary["ignored_predictions"],
                error_unit="m",
                extra_metrics={"missed_targets": (float(primary["fn"]), "count")},
            )
        )

    report: dict[str, Any] = {
        "format": (
            "center-size-evaluation-v1" if size_metrics is not None else "center-evaluation-v1"
        ),
        "status": "complete",
        "run_id": run.run_id,
        "test_scene_sha256": scene.content_sha256,
        "warmup_s": warmup_s,
        "warmup_origin_ns": origin_ns,
        "excluded_reference_times": excluded,
        "nonlocated_outputs": nonlocated,
        "minimum_stations": minimum_stations,
        "responsibility_exclusion": (
            "precise outputs ignore only hits within precise gate of nonresponsible targets"
        ),
        "responsibility": f"target center in FOV of at least {minimum_stations} distinct main stations",
        "metrics": metrics.summary() if size_metrics is None else size_metrics.summary(),
    }
    return score_events, report, records


def score_run(
    scene_dir: Path,
    run_dir: Path,
    *,
    output_dir: Path | None = None,
    mode: str | None = None,
    gate_px: float = 10.0,
    size_k_px: float | None = 0.5,
    gate_m: float = 10.0,
    size_k_m: float | None = None,
    miss_k: float = 3.0,
    warmup_s: float | None = None,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Score a Run Output package against an immutable Test Scene and persist results."""
    scene = TestScenePackage.open(scene_dir, verify_hashes=verify_hashes)
    run = RunOutputPackage.open(run_dir, verify_hashes=verify_hashes)
    run.verify_matches_scene(scene)
    effective_mode = run.mode if mode is None else mode

    if effective_mode == "edge":
        edge_warmup = 15.0 if warmup_s is None else warmup_s
        scores, report, _ = score_edge_run(
            scene,
            run,
            gate_px=gate_px,
            size_k=size_k_px,
            warmup_s=edge_warmup,
        )
        run.write_scores(scores, report, output_dir=output_dir)
        return report

    if effective_mode == "center":
        center_warmup = 0.0 if warmup_s is None else warmup_s
        scores, report, _ = score_center_run(
            scene,
            run,
            gate_m=gate_m,
            size_k=size_k_m,
            miss_k=miss_k,
            warmup_s=center_warmup,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
        )
        run.write_scores(scores, report, output_dir=output_dir)
        return report

    if effective_mode == "integrated":
        effective_warmup = 15.0 if warmup_s is None else warmup_s
        edge_scores, edge_report, _ = score_edge_run(
            scene,
            run,
            gate_px=gate_px,
            size_k=size_k_px,
            warmup_s=effective_warmup,
        )
        center_scores, center_report, center_records = score_center_run(
            scene,
            run,
            gate_m=gate_m,
            size_k=size_k_m,
            miss_k=miss_k,
            warmup_s=effective_warmup,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
        )
        attribution: dict[str, Any] | None = None
        if size_k_m is not None:
            attribution = attribute_run(
                scene,
                run,
                center_records,
                gate_px=gate_px,
                size_k_px=size_k_px,
                precise_k_m=size_k_m,
                minimum_stations=minimum_stations,
                warmup_s=effective_warmup,
            )
        combined_report: dict[str, Any] = {
            "format": "chongming-scorer-integrated-v1",
            "status": "complete",
            "run_id": run.run_id,
            "test_scene_sha256": scene.content_sha256,
            "edge": edge_report,
            "center": center_report,
        }
        if attribution is not None:
            combined_report["attribution"] = attribution
        all_scores = sorted(
            [*edge_scores, *center_scores],
            key=lambda item: (item.experiment_time_ns, item.error_unit),
        )
        run.write_scores(all_scores, combined_report, output_dir=output_dir)
        return combined_report

    raise ValueError(f"unsupported score mode: {effective_mode!r}")
