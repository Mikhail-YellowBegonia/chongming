"""Offline operational attribution; truth labels never enter the replayed algorithms."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterator
from itertools import combinations, product
from pathlib import Path
from typing import Any

import numpy as np

from chongming.contracts import fusion as f
from chongming.evaluation import annotate
from chongming.modules.fusion_geometry import _line_fit
from chongming.performance import distribution
from chongming.scenarios.serialization import _decode


def json_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as source:
        for line in source:
            yield json.loads(line)


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


def false_output_owner(kind: str, labels: list[dict[str, Any]]) -> str:
    if kind == "duplicate":
        return "center:duplicate_location"
    if any(label["kind"] == "false_alarm" for label in labels):
        return "device:false_positive_propagated"
    targets = {label["target_id"] for label in labels}
    if len(targets) > 1:
        return "center:cross_target_combination"
    return "device:localization_error_propagated"


def scored_performance(device: Path, center: Path, reference_times: set[int]) -> dict[str, Any]:
    policy = json.loads((center / "center-registration.json").read_text())["policy"]["windows"]
    origin, width = policy["origin_ns"], policy["width_ns"]
    starts = {origin + (ns - origin) // width * width: ns for ns in reference_times}
    device_ms: dict[int, float] = defaultdict(float)
    for row in json_rows(device / "evaluation/frames.jsonl"):
        if not row["warmup_excluded"]:
            device_ms[row["time_ns"]] += row["timing_ms"]["pipeline_ms"]
    stages: dict[str, list[float]] = defaultdict(list)
    calls, sequential, group_counts = [], [], []
    window_samples = []
    group_ms = []
    for row in json_rows(center / "timings.jsonl"):
        windows = [w for w in row["windows"] if w["window_start_ns"] in starts]
        if not windows:
            continue
        calls.append(row["center_process_ms"])
        sequential.append(
            row["center_process_ms"] + sum(device_ms[starts[w["window_start_ns"]]] for w in windows)
        )
        for window in windows:
            window_samples.append(window)
            group_counts.append(window["geometry_groups"])
            if window["geometry_groups"]:
                group_ms.append(window["geometry_ms"] / window["geometry_groups"])
            for key, value in window.items():
                if key.endswith("_ms"):
                    stages[key].append(value)
    if len(group_counts) != len(reference_times):
        raise ValueError("scored performance windows do not match evaluation schedule")
    return {
        "center_process_ms": distribution(calls),
        "stages_ms": {key: distribution(values) for key, values in stages.items()},
        "geometry_groups_per_window": distribution(group_counts),
        "geometry_ms_per_group_window_average": distribution(group_ms),
        "sequential_three_station_device_plus_center_ms": distribution(sequential),
        "slowest_geometry_windows": sorted(
            window_samples, key=lambda w: w["geometry_ms"], reverse=True
        )[:10],
        "scope": "exact scored windows only; sequential total sums separately measured device and center phases, not end-to-end latency; no decode, ray construction, serialization, disk, scoring or rendering",
    }


def attribute(device: Path, center: Path, scores: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    frame_labels = {}
    for row in json_rows(device / "evaluation/frames.jsonl"):
        if row["warmup_excluded"]:
            continue
        annotated = annotate(row, 10)
        targets = annotated["targets"]
        labels = []
        for prediction in annotated["predictions"]:
            index = prediction["target"]
            target = targets[index]["id"] if index >= 0 else None
            # Duplicate detections still carry correct directional evidence.
            if target is None and prediction["kind"] == "duplicate":
                choices = [t for t in targets if math.dist(prediction["xy"], t["xy"]) <= t["gate"]]
                target = min(
                    choices, key=lambda t: (math.dist(prediction["xy"], t["xy"]), t["id"])
                )["id"]
            labels.append(
                {
                    "target_id": target,
                    "kind": prediction["kind"],
                    "pixel_position": prediction["xy"],
                }
            )
        frame_labels[row["frame_id"]] = labels
    labels_by_event = {}
    emitted: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in json_rows(center / "edge-rays.jsonl"):
        if row["frame_id"] not in frame_labels:
            continue
        label = frame_labels[row["frame_id"]][row["prediction_index"]]
        event = row["event"]
        envelope = event["envelope"]
        event_id = (envelope["station_id"], envelope["station_session_id"], envelope["sequence_no"])
        labels_by_event[event_id] = label
        if envelope["event_type"] == "ray" and label["target_id"] is not None:
            emitted[row["time_ns"]][label["target_id"]].add(row["station_id"])
    score_summary = json.loads((scores / "summary.json").read_text())
    minimum_stations = score_summary["minimum_stations"]
    frames = {row["reference_time_ns"]: row for row in json_rows(scores / "frames.jsonl")}
    misses: Counter[str] = Counter()
    false_outputs: Counter[str] = Counter()
    contributors: Counter[str] = Counter()
    stage_reasons: Counter[str] = Counter()
    seen = set()
    with (output / "cases.jsonl").open("x") as cases:
        for result in json_rows(center / "center-output.jsonl"):
            by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for measurement in result["measurements"]:
                if measurement["status"] == "located":
                    by_time[measurement["reference_time_ns"]].append(measurement)
            for batch in result["batches"]:
                geometries = {g["window"]["start_ns"]: g for g in batch["geometry"]}
                for window in batch["window_update"]["sealed_windows"]:
                    capture_times = {
                        (
                            r["event"]["capture_time"]["exposure_start_ns"]
                            + r["event"]["capture_time"]["exposure_end_ns"]
                        )
                        // 2
                        for r in window["rays"]
                    }
                    # Empty windows remain in scores; use its containing scored time.
                    times = [
                        ns
                        for ns in frames
                        if window["key"]["start_ns"] <= ns < window["key"]["end_ns"]
                    ]
                    if not times:
                        continue
                    if len(times) != 1 or len(capture_times) > 1:
                        raise ValueError("attribution requires synchronous one-exposure windows")
                    ns = times[0]
                    if ns in seen:
                        raise ValueError("repeated scored window")
                    seen.add(ns)
                    frame = frames[ns]
                    predictions = by_time.get(ns, [])
                    geometry = geometries.get(window["key"]["start_ns"], {"results": []})
                    rays = {}
                    admitted: dict[str, dict[str, list[f.AdmittedRay]]] = defaultdict(
                        lambda: defaultdict(list)
                    )
                    for raw in window["rays"]:
                        env = raw["event"]["envelope"]
                        event_id = (
                            env["station_id"],
                            env["station_session_id"],
                            env["sequence_no"],
                        )
                        label = labels_by_event[event_id]
                        rays[event_id] = label
                        if label["target_id"] is not None:
                            admitted[label["target_id"]][env["station_id"]].append(
                                _decode(raw, f.AdmittedRay)
                            )
                    group_info: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for group in geometry["results"]:
                        measurement = group["measurement"]
                        supports = [rays[tuple(i)] for i in measurement["supporting_ray_event_ids"]]
                        target_ids = {s["target_id"] for s in supports}
                        if len(target_ids) == 1 and None not in target_ids:
                            group_info[next(iter(target_ids))].append(group)
                        for reason in group["reasons"]:
                            stage_reasons[reason] += 1
                    for target, truth in frame["targets"].items():
                        if truth["precise_hit"]:
                            continue
                        gate = truth["aabb_diagonal_m"] * 0.7
                        raw_can_hit = False
                        minimum_error = math.inf
                        raw_groups = 0
                        station_rays = admitted[target]
                        for count in range(minimum_stations, len(station_rays) + 1):
                            for stations in combinations(sorted(station_rays), count):
                                for chosen in product(*(station_rays[s] for s in stations)):
                                    raw_groups += 1
                                    try:
                                        position, _, _, rank, _ = _line_fit(chosen)
                                    except np.linalg.LinAlgError:
                                        continue
                                    if rank == 3:
                                        error = math.dist(position, truth["position_enu_m"])
                                        minimum_error = min(minimum_error, error)
                                        raw_can_hit |= error <= gate
                        groups = group_info[target]
                        geometry_can_hit = any(
                            g["measurement"]["status"] == "located"
                            and math.dist(
                                g["measurement"]["position_enu_m"], truth["position_enu_m"]
                            )
                            <= gate
                            for g in groups
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
                        cases.write(
                            json.dumps(
                                {
                                    "kind": "miss",
                                    "time_ns": ns,
                                    "target_id": target,
                                    "owner": owner,
                                    "correct_emitting_stations": sorted(emitted[ns][target]),
                                    "correct_admitted_stations": sorted(station_rays),
                                    "raw_correct_combinations": raw_groups,
                                    "raw_can_hit": raw_can_hit,
                                    "nearest_raw_correct_error_m": minimum_error
                                    if math.isfinite(minimum_error)
                                    else None,
                                    "gate_m": gate,
                                    "correct_geometry_groups": len(groups),
                                    "correct_geometry_measurement_ids": [
                                        g["measurement"]["measurement_id"] for g in groups
                                    ],
                                    "correct_ray_event_ids": [
                                        [
                                            ray.event.envelope.station_id,
                                            ray.event.envelope.station_session_id,
                                            ray.event.envelope.sequence_no,
                                        ]
                                        for values in station_rays.values()
                                        for ray in values
                                    ],
                                    "suppressed_correct_groups": [
                                        removed
                                        for removed in batch["suppressed_measurements"]
                                        if removed["measurement"]["measurement_id"]
                                        in {g["measurement"]["measurement_id"] for g in groups}
                                    ],
                                    "geometry_reasons": dict(
                                        Counter(r for g in groups for r in g["reasons"])
                                    ),
                                }
                            )
                            + "\n"
                        )
                    targets = {
                        key: value["position_enu_m"] for key, value in frame["targets"].items()
                    }
                    gates = {
                        key: 0.7 * value["aabb_diagonal_m"]
                        for key, value in frame["targets"].items()
                    }
                    annotated = annotate(
                        {
                            "eligible_targets_px": targets,
                            "target_gates_px": gates,
                            "predictions_px": [p["position_enu_m"] for p in predictions],
                        },
                        1,
                    )
                    frame_fp = 0
                    for prediction, label in zip(
                        predictions, annotated["predictions"], strict=True
                    ):
                        if label["kind"] == "matched":
                            continue
                        if label["kind"] == "false_alarm" and any(
                            math.dist(prediction["position_enu_m"], p) <= 0.7 * diagonal
                            for p, diagonal in zip(
                                frame["ignored_targets_enu_m"],
                                frame["ignored_target_diagonals_m"],
                                strict=True,
                            )
                        ):
                            continue
                        support_labels = [
                            labels_by_event[tuple(event_id)]
                            for event_id in prediction["supporting_ray_event_ids"]
                        ]
                        owner = false_output_owner(label["kind"], support_labels)
                        false_outputs[owner] += 1
                        frame_fp += 1
                        has_false_input = any(s["kind"] == "false_alarm" for s in support_labels)
                        cross_target = (
                            len(
                                {
                                    s["target_id"]
                                    for s in support_labels
                                    if s["target_id"] is not None
                                }
                            )
                            > 1
                        )
                        contributors["accepted_by_center"] += 1
                        contributors["contains_device_false_alarm"] += int(has_false_input)
                        contributors["contains_cross_target_support"] += int(cross_target)
                        cases.write(
                            json.dumps(
                                {
                                    "kind": "false_output",
                                    "time_ns": ns,
                                    "owner": owner,
                                    "measurement_id": prediction["measurement_id"],
                                    "classification": label["kind"],
                                    "position_enu_m": prediction["position_enu_m"],
                                    "supporting_ray_event_ids": prediction[
                                        "supporting_ray_event_ids"
                                    ],
                                    "support_labels": support_labels,
                                    "contains_cross_target_support": cross_target,
                                    "contains_device_false_alarm": has_false_input,
                                }
                            )
                            + "\n"
                        )
                    if frame_fp != frame["precise"]["fp"]:
                        raise ValueError("attributed false output count differs from score")
        if seen != set(frames):
            raise ValueError("not all scored windows have attribution evidence")
    summary = json.loads((scores / "summary.json").read_text())
    metrics = summary["metrics"]["precise"]
    if sum(misses.values()) != metrics["fn"] or sum(false_outputs.values()) != metrics["fp"]:
        raise ValueError("attribution totals differ from evaluation")
    report = {
        "format": "integrated-attribution-v1",
        "status": "complete",
        "misses": dict(misses),
        "false_outputs": dict(false_outputs),
        "false_output_contributors": dict(contributors),
        "geometry_rejections_scored_windows": dict(stage_reasons),
        "metrics": metrics,
        "performance": scored_performance(device, center, set(frames)),
        "scope": "operational primary ownership, not proof of exclusive physical causality; contributions retained; oracle labels and counterfactual fits are offline only",
        "miss_rule": f"<{minimum_stations} correct emitted stations: device; <{minimum_stations} admitted: center ingress; no raw correct combination can hit: device localization; otherwise center candidate/geometry/suppression stage",
        "false_output_rule": "duplicate: center; any false input: device contribution; mixed correct targets: center association; same correct target but wrong 3D point: propagated device localization",
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", type=Path)
    parser.add_argument("center", type=Path)
    parser.add_argument("scores", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(attribute(args.device, args.center, args.scores, args.output), indent=2))


if __name__ == "__main__":
    main()
