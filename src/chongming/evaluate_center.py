"""Offline center metrics at exact reference times, including empty sealed windows."""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from dataclasses import replace
from pathlib import Path
from typing import Any

from chongming.center_evaluation import CenterSizeMetrics, model_aabb_diagonal
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.evaluation import DetectionMetrics
from chongming.modules.scene import (
    CameraProjectionModule,
    SceneLogicModule,
    TrajectoryModule,
    VisibilityModule,
)
from chongming.scenarios.run import _provenance
from chongming.scenarios.serialization import load_compiled_scenario


def evaluate(
    recording: Path,
    output: Path,
    *,
    gate_m: float = 10,
    warmup_s: float = 0.0,
    size_k: float | None = None,
    miss_k: float = 3,
    asset_root: Path | None = None,
    diagnostic_raw: bool = False,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
) -> dict[str, Any]:
    if minimum_stations < 2:
        raise ValueError("minimum_stations must be at least two")
    if output.exists():
        raise FileExistsError(output)
    metrics = DetectionMetrics(gate_m, "m")
    size_metrics = CenterSizeMetrics(size_k, miss_k) if size_k is not None else None
    if not 0 <= warmup_s < float("inf"):
        raise ValueError("warmup_s must be finite and nonnegative")
    spec = load_compiled_scenario(recording / "scenario.json", asset_root=asset_root)
    origin_ns = spec.timeline.start_time_ns if warmup_origin_ns is None else warmup_origin_ns
    logic = SceneLogicModule(TrajectoryModule(), CameraProjectionModule(), VisibilityModule())
    # Responsibilities use FOV only, regardless of the recording's occlusion policy.
    logic.initialize(
        replace(spec.scene_definition, occlusion_mode=s.OcclusionMode.UNKNOWN),
        spec.perturbations,
        spec.random_seed,
    )
    predictions: dict[int, list[tuple[float, ...]]] = {}
    outputs: dict[str, dict[str, Any]] = {}
    times: set[int] = set()
    windows: set[tuple[int, int]] = set()
    invalid = 0
    # Load and validate all output before creating the report directory.
    with (recording / "center-output.jsonl").open() as handle:
        for line in handle:
            row = json.loads(line)
            for batch in row["batches"]:
                for window in batch["window_update"]["sealed_windows"]:
                    times.add(window["reference_time_ns"])
                    if "key" in window:
                        windows.add((window["key"]["start_ns"], window["key"]["end_ns"]))
            if diagnostic_raw and "diagnostic_raw_solutions" not in row:
                raise ValueError("raw diagnostic solutions missing from center output")
            for measurement in (
                row["diagnostic_raw_solutions"] if diagnostic_raw else row["measurements"]
            ):
                key = measurement["measurement_id"]
                if key in outputs:
                    if outputs[key] != measurement:
                        raise ValueError("conflicting duplicate measurement ID")
                    continue
                outputs[key] = measurement
                ns = measurement["reference_time_ns"]
                if (diagnostic_raw or measurement["status"] == "located") and measurement[
                    "position_enu_m"
                ] is not None:
                    position = tuple(measurement["position_enu_m"])
                    if len(position) != 3:
                        raise ValueError("center position must have three coordinates")
                    predictions.setdefault(ns, []).append(position)
                else:
                    invalid += 1
    schedule = "unique explicit sealed-window reference times"
    inputs = recording / "center-input.jsonl"
    if inputs.exists() and windows:
        captures: set[int] = set()
        with inputs.open() as handle:
            for line in handle:
                for progress in json.loads(line)["progress"]:
                    capture = progress["completed_capture_time"]
                    if capture is not None:
                        captures.add(
                            (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2
                        )
        ordered = sorted(captures)
        times = set()
        for start, end in sorted(windows):
            selected = ordered[bisect_left(ordered, start) : bisect_left(ordered, end)]
            if len(selected) != 1:
                raise ValueError(
                    "center evaluation requires one synchronous exposure time per sealed window"
                )
            times.add(selected[0])
        schedule = "one actual main exposure midpoint per sealed window, including empty outputs; synchronous stations"
    if not set(predictions).issubset(times):
        raise ValueError(
            "measurement reference time has no sealed-window schedule; recall is undefined"
        )
    records: list[dict[str, Any]] = []
    excluded = 0
    for ns in sorted(times):
        if not spec.timeline.start_time_ns <= ns <= spec.timeline.end_time_ns:
            raise ValueError("center reference time outside recorded scenario")
        if ns - origin_ns < round(warmup_s * 1e9):
            excluded += 1
            continue
        snapshot = logic.sample(s.SimulationTime(spec.timeline.clock_domain.clock_domain_id, ns, 0))
        visible: dict[str, set[str]] = {}
        for visibility in snapshot.target_visibility:
            if (
                visibility.camera_role == m.CameraRole.MAIN
                and visibility.projection.center_in_field_of_view
            ):
                visible.setdefault(str(visibility.truth_object_id), set()).add(
                    str(visibility.station_id)
                )
        eligible: dict[str, tuple[float, ...]] = {}
        ignored: list[tuple[float, ...]] = []
        diagonals = {}
        ignored_diagonals = []
        states = {}
        for target in snapshot.flying_objects:
            position = tuple(target.state.position_enu_m)
            if len(visible.get(str(target.truth_object_id), set())) >= minimum_stations:
                key = str(target.truth_object_id)
                eligible[key] = position
                if size_metrics is not None:
                    diagonals[key] = model_aabb_diagonal(target.geometry)
                states[key] = {
                    "velocity_enu_mps": target.state.velocity_enu_mps,
                    "coverage_stations": sorted(visible.get(key, set())),
                }
            else:
                ignored.append(position)
                if size_metrics is not None:
                    ignored_diagonals.append(model_aabb_diagonal(target.geometry))
        result = (
            metrics.add(predictions.get(ns, []), eligible, ignored=ignored)
            if size_metrics is None
            else size_metrics.add(
                predictions.get(ns, []), eligible, diagonals, ignored, ignored_diagonals
            )
        )
        if size_metrics is not None:
            result["ignored_targets_enu_m"] = ignored
            result["ignored_target_diagonals_m"] = ignored_diagonals
            for key, state in states.items():
                result["targets"][key].update(state)
        records.append({"reference_time_ns": ns, **result})
    report = {
        "format": "center-size-evaluation-v1"
        if size_metrics is not None
        else "center-evaluation-v1",
        "status": "complete",
        "prediction_source": "diagnostic raw intersections"
        if diagnostic_raw
        else "located geometry measurements",
        "recording": str(recording.resolve()),
        "warmup_s": warmup_s,
        "warmup_origin_ns": origin_ns,
        "evaluation_provenance": _provenance(),
        "excluded_reference_times": excluded,
        "nonlocated_outputs": invalid,
        "minimum_stations": minimum_stations,
        "responsibility_exclusion": "precise outputs ignore only hits within precise gate of nonresponsible targets",
        "responsibility": f"target center in FOV of at least {minimum_stations} distinct main stations",
        "schedule": schedule,
        "truth": "deterministic Python scene sampled at exact reference time; no renderer",
        "metrics": metrics.summary() if size_metrics is None else size_metrics.summary(),
    }
    output.mkdir(parents=True, exist_ok=False)
    with (output / "frames.jsonl").open("x") as handle:
        for record in records:
            handle.write(json.dumps(record, allow_nan=False) + "\n")
    (output / "summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum-stations", type=int, default=2)
    parser.add_argument("--gate-m", default=10, type=float)
    parser.add_argument("--size-k", type=float, help="Precise-hit model AABB diagonal multiplier")
    parser.add_argument("--miss-k", default=3.0, type=float)
    parser.add_argument("--warmup-s", type=float, default=0.0)
    parser.add_argument(
        "--warmup-origin-ns",
        type=int,
        help="Explicit exposure epoch for warmup; default scenario start",
    )
    parser.add_argument(
        "--diagnostic-raw",
        action="store_true",
        help="Score unfiltered diagnostic intersections, not production measurements",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(
                args.recording,
                args.output,
                gate_m=args.gate_m,
                warmup_s=args.warmup_s,
                size_k=args.size_k,
                miss_k=args.miss_k,
                asset_root=args.asset_root,
                diagnostic_raw=args.diagnostic_raw,
                warmup_origin_ns=args.warmup_origin_ns,
                minimum_stations=args.minimum_stations,
            )["metrics"],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
