"""Offline saved-ray candidate and quantization comparisons; production code is unchanged."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import shutil
import sys
import time
from bisect import bisect_left
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from chongming.benchmark_occupancy import sha256, supervise
from chongming.contracts import development as d
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.evaluate_center import evaluate
from chongming.evaluation_attempt import write_json
from chongming.modules.fusion import CalibrationWeightingModule
from chongming.modules.fusion_geometry import (
    _STATION_SCORE,
    GeometryRefinementModule,
    VoxelCandidateModule,
    _event_id,
)
from chongming.modules.fusion_postintersection import (
    ExhaustiveAssociationModule,
    candidate_groups,
    suppress_duplicates,
)
from chongming.performance import distribution
from chongming.scenarios.run import _json, _provenance
from chongming.scenarios.serialization import _decode
from chongming.voxel_diagnostics import VoxelDiagnostics

ARMS = ("voxel-refined", "direct-refined", "voxel-coarse")


def scheduled_exposure(window: dict[str, Any], captures: list[int]) -> int:
    key = window["key"]
    times = captures[bisect_left(captures, key["start_ns"]) : bisect_left(captures, key["end_ns"])]
    if len(times) != 1:
        raise ValueError("fixture requires exactly one scheduled exposure per sealed window")
    return times[0]


def direct_candidates(
    window: f.SealedObservationWindow, policy: f.FusionPolicy
) -> f.CandidateBatch:
    """One full-volume candidate retains every independent-station ray alternative."""
    weights = CalibrationWeightingModule(rules=policy.admission.calibration_usage_rules)
    support: dict[m.StationId, list[m.EventId]] = {}
    for ray in window.rays:
        if weights.weight(ray).weight_scale > 0:
            support.setdefault(ray.event.envelope.station_id, []).append(_event_id(ray))
    candidate = f.SpatialCandidate(
        candidate_id="direct/all",
        window=window.key,
        method=f.CandidateMethod.DIRECT_GEOMETRY,
        origin=f.CandidateOrigin.UNGUIDED_SEARCH,
        prior_track_id=None,
        coarse_position_enu_m=None,
        region_min_enu_m=policy.candidates.search.min_enu_m,
        region_max_enu_m=policy.candidates.search.max_enu_m,
        independent_station_support=tuple(
            f.IndependentStationSupport(
                station_id=s, alternative_ray_event_ids=tuple(sorted(set(ids)))
            )
            for s, ids in sorted(support.items())
        ),
        score=float(len(support)),
        score_definition=_STATION_SCORE,
    )
    return f.CandidateBatch(
        window=window.key,
        method=f.CandidateMethod.DIRECT_GEOMETRY,
        search=policy.candidates.search,
        status=f.CandidateBatchStatus.CANDIDATES_PROPOSED,
        candidates=(candidate,) if len(support) >= 3 else (),
        examined_ray_event_ids=tuple(sorted(_event_id(ray) for ray in window.rays)),
        unguided_search_performed=True,
        reason_codes=(),
    )


def bounded(
    item: m.FusionMeasurement, window: f.SealedObservationWindow, search: m.VoxelSearchSpec
) -> m.FusionMeasurement:
    if item.status is not m.FusionStatus.LOCATED:
        return item
    assert item.position_enu_m is not None
    point = np.asarray(item.position_enu_m)
    reason = None
    if np.any(point < search.min_enu_m) or np.any(point > search.max_enu_m):
        reason = "outside_search_volume"
    available = {_event_id(ray): ray for ray in window.rays}
    near, far = (0.0, math.inf) if search.forward_range_m is None else search.forward_range_m
    for event in item.supporting_ray_event_ids:
        ray = available[event].event
        depth = float(np.dot(point - ray.ray_origin_enu_m, ray.ray_direction_enu_unit))
        if not near <= depth <= far:
            reason = "outside_forward_range"
    return (
        item
        if reason is None
        else replace(
            item,
            status=m.FusionStatus.GEOMETRY_DEGENERATE,
            position_enu_m=None,
            reason_codes=(*item.reason_codes, reason),
        )
    )


def coarse_measurement(
    window: f.SealedObservationWindow,
    group: f.AssociatedRayGroup,
    point: m.Vec3,
    policy: f.FusionPolicy,
) -> m.FusionMeasurement:
    available = {_event_id(ray): ray for ray in window.rays}
    rays = [available[event].event for event in group.supporting_ray_event_ids]
    directions = np.asarray([ray.ray_direction_enu_unit for ray in rays])
    offsets = np.asarray(point) - np.asarray([ray.ray_origin_enu_m for ray in rays])
    depths = np.einsum("ij,ij->i", offsets, directions)
    residuals = np.linalg.norm(offsets - depths[:, None] * directions, axis=1)
    angles = [
        math.atan2(float(np.linalg.norm(np.cross(a, b))), abs(float(np.dot(a, b))))
        for a, b in itertools.combinations(directions, 2)
    ]
    normal = sum((np.eye(3) - np.outer(v, v) for v in directions), start=np.zeros((3, 3)))
    condition = float(np.linalg.cond(normal))
    reasons = []
    if max(angles) < policy.geometry.minimum_intersection_angle_rad:
        reasons.append("near_parallel_rays")
    if not math.isfinite(condition) or condition > policy.geometry.maximum_condition_number:
        reasons.append("poor_condition")
    if float(residuals.max()) > policy.geometry.maximum_residual_m:
        reasons.append("excessive_residual_at_coarse_point")
    midpoints = [
        (ray.capture_time.exposure_start_ns + ray.capture_time.exposure_end_ns) // 2 for ray in rays
    ]
    if max(midpoints) - min(midpoints) > policy.geometry.maximum_unaligned_span_ns:
        reasons.append("unaligned_exposures")
    return m.FusionMeasurement(
        center_session_id=window.key.center_session_id,
        measurement_id=m.MeasurementId(f"coarse/{window.key.start_ns}/{group.hypothesis_id}"),
        clock_domain_id=window.key.capture_clock_domain_id,
        world_frame_id=policy.admission.world_frame_id,
        reference_time_ns=group.reference_time_ns,
        support_exposure_span_ns=group.support_exposure_span_ns,
        status=m.FusionStatus.GEOMETRY_DEGENERATE if reasons else m.FusionStatus.LOCATED,
        position_enu_m=None if reasons else point,
        conditional_position_covariance_enu_m2=None,
        motion_alignment=None,
        supporting_ray_event_ids=group.supporting_ray_event_ids,
        supporting_station_ids=group.supporting_station_ids,
        residuals_m=tuple(float(v) for v in residuals),
        geometry_condition=condition if math.isfinite(condition) else None,
        minimum_intersection_angle_rad=min(angles),
        association_hypothesis_id=group.hypothesis_id,
        reason_codes=tuple(reasons),
    )


def freeze(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    for name in ("center-registration.json", "center-input.jsonl", "scenario.json"):
        shutil.copyfile(source / name, output / name)
    selected = []
    eligible = 0
    captures = set()
    with (source / "center-input.jsonl").open() as handle:
        for line in handle:
            for progress in json.loads(line)["progress"]:
                capture = progress["completed_capture_time"]
                if capture is not None:
                    captures.add((capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2)
    ordered = sorted(captures)
    with (source / "center-output.jsonl").open() as handle:
        for line in handle:
            for batch in json.loads(line)["batches"]:
                for window in batch["window_update"]["sealed_windows"]:
                    times = {
                        (
                            ray["event"]["capture_time"]["exposure_start_ns"]
                            + ray["event"]["capture_time"]["exposure_end_ns"]
                        )
                        // 2
                        for ray in window["rays"]
                    }
                    ns = scheduled_exposure(window, ordered)
                    if ns - 1_000_000 < 15_000_000_000:
                        continue
                    if len(times) > 1 or (times and times != {ns}):
                        raise ValueError("fixture requires synchronous exposure groups")
                    if eligible % 15 == 0:
                        selected.append(window)
                    eligible += 1
    if len(selected) != 30:
        raise ValueError(f"expected 30 fixed windows, found {len(selected)}")
    (output / "windows.jsonl").write_text("".join(json.dumps(w) + "\n" for w in selected))
    write_json(
        output / "manifest.json",
        {
            "selection": "every fifteenth scored window in chronological order; no selection by truth or outputs",
            "eligible_windows": eligible,
            "selected_windows": len(selected),
            "minimum_stations": 3,
            "scope": "post-admission synchronous geometry; no track predictions; not full-recording integrated quality",
            "source": str(source.resolve()),
            "source_sha256": {
                name: sha256(source / name)
                for name in (
                    "center-registration.json",
                    "center-input.jsonl",
                    "center-output.jsonl",
                    "scenario.json",
                )
            },
            "fixture_sha256": sha256(output / "windows.jsonl"),
            "provenance": _provenance(),
            "budgets": {
                "wall_s_per_arm": 300,
                "memory_bytes": 4 * 1024**3,
                "returned_window_s": 10,
                "groups_per_window": 50000,
            },
            "coarse_representative": "existing candidate region midpoint; choose lexicographically first candidate ID for repeated evidence; not necessarily one cell center",
            "common_rules": "three independent stations; same input calibration/uncertainty; common volume/depth gates and 3m complete-link deduplication; coarse residual is evaluated at its representative, with no iterative fit/covariance or convergence test",
        },
    )


def worker(fixture: Path, arm: str, output: Path) -> None:
    cv2.setNumThreads(10)
    registration = _decode(
        json.loads((fixture / "center-registration.json").read_text()), d.CenterRegistration
    )
    policy = registration.policy
    assert policy.geometry.minimum_independent_stations == 3
    weights = CalibrationWeightingModule(rules=policy.admission.calibration_usage_rules)
    diagnostics = VoxelDiagnostics(enabled=True)
    voxel = VoxelCandidateModule(
        policy=policy.candidates,
        motion_policy=policy.geometry,
        weights=weights,
        diagnostics=diagnostics,
    )
    geometry = GeometryRefinementModule(
        center_session_id=m.SessionId(registration.center_session_id),
        world_frame_id=policy.admission.world_frame_id,
        policy=policy.geometry,
        weights=weights,
    )
    association = ExhaustiveAssociationModule(minimum_stations=3)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("scenario.json", "center-input.jsonl"):
        shutil.copyfile(fixture / name, output / name)
    rows = []
    progress: dict[str, Any] = {
        "phase": "initialization",
        "processed_windows": 0,
        "planned_windows": 30,
    }
    write_json(output / "progress.json", progress)
    status = "failed"
    reason = None
    try:
        with (output / "center-output.jsonl").open("x", buffering=1) as evidence:
            for line in (fixture / "windows.jsonl").read_text().splitlines():
                window = _decode(json.loads(line), f.SealedObservationWindow)
                progress.update(
                    {
                        "phase": "geometry",
                        "current_window": window.key.start_ns,
                        "phase_started_monotonic": time.monotonic(),
                    }
                )
                write_json(output / "progress.json", progress)
                began = time.perf_counter()
                candidates = (
                    direct_candidates(window, policy)
                    if arm == "direct-refined"
                    else voxel.propose(window, ())
                )
                candidate_ms = (time.perf_counter() - began) * 1000
                theoretical = direct_candidates(window, policy)
                cardinalities = [
                    len(s.alternative_ray_event_ids)
                    for c in theoretical.candidates
                    for s in c.independent_station_support
                ]
                if cardinalities and math.prod(cardinalities) > 50000:
                    reason = "support_combination_limit"
                    status = "resource_limit"
                    break
                group_began = time.perf_counter()
                groups = association.associate(window, candidates, ())
                group_ms = (time.perf_counter() - group_began) * 1000
                available = {_event_id(ray): ray for ray in window.rays}
                candidates_by_id = {c.candidate_id: c for c in candidates.candidates}
                supported = candidate_groups(candidates, 3)
                solved = []
                solve_began = time.perf_counter()
                for group in groups.selected_groups:
                    if arm == "voxel-coarse":
                        chosen = min(supported[group.supporting_ray_event_ids])
                        point = candidates_by_id[chosen].coarse_position_enu_m
                        assert point is not None
                        item = coarse_measurement(window, group, point, policy)
                    else:
                        item = geometry._refine_group(window, group, available, ()).measurement
                    solved.append(bounded(item, window, policy.candidates.search))
                solve_ms = (time.perf_counter() - solve_began) * 1000
                dedup_began = time.perf_counter()
                kept, suppressed = suppress_duplicates(tuple(solved), 3.0)
                dedup_ms = (time.perf_counter() - dedup_began) * 1000
                total_ms = (time.perf_counter() - began) * 1000
                evidence.write(
                    _json(
                        {
                            "measurements": kept,
                            "batches": [{"window_update": {"sealed_windows": [window]}}],
                        }
                    )
                    + "\n"
                )
                row = {
                    "window_start_ns": window.key.start_ns,
                    "rays_per_station": dict(
                        Counter(str(ray.event.envelope.station_id) for ray in window.rays)
                    ),
                    "groups": len(groups.selected_groups),
                    "solver_calls": 0 if arm == "voxel-coarse" else len(groups.selected_groups),
                    "evidence_ids": [
                        group.supporting_ray_event_ids for group in groups.selected_groups
                    ],
                    "located_evidence_ids": [
                        item.supporting_ray_event_ids
                        for item in solved
                        if item.status is m.FusionStatus.LOCATED
                    ],
                    "rejections": dict(Counter(r for item in solved for r in item.reason_codes)),
                    "candidate_reasons": list(candidates.reason_codes),
                    "suppressed": len(suppressed),
                    "supported_voxels": 0
                    if arm == "direct-refined" or diagnostics.snapshot is None
                    else len(diagnostics.snapshot.scored_voxels),
                    "candidate_ms": candidate_ms,
                    "group_ms": group_ms,
                    "solve_ms": solve_ms,
                    "dedup_ms": dedup_ms,
                    "algorithm_ms": total_ms,
                }
                rows.append(row)
                progress["processed_windows"] = len(rows)
                write_json(output / "progress.json", progress)
                if total_ms > 10000:
                    reason = "returned_window_time_limit"
                    status = "resource_limit"
                    break
        if reason is None:
            status = "complete"
    finally:
        write_json(output / "windows-summary.json", rows)
        write_json(
            output / "summary.json",
            {
                "status": status,
                "stop_reason": reason,
                "aggregate": None,
                "attempt": progress,
                "arm": arm,
                "timings": {
                    k: distribution([row[k] for row in rows])
                    for k in ("candidate_ms", "group_ms", "solve_ms", "dedup_ms", "algorithm_ms")
                },
                "groups": sum(row["groups"] for row in rows),
                "solver_calls": sum(row["solver_calls"] for row in rows),
            },
        )
    if status == "complete":
        score = evaluate(
            output,
            output / "score",
            asset_root=Path("scenarios"),
            size_k=0.7,
            miss_k=3,
            minimum_stations=3,
            warmup_s=0,
        )
        errors = []
        for line in (output / "score/frames.jsonl").read_text().splitlines():
            errors.extend(json.loads(line)["precise"]["matched_errors"])
        summary = json.loads((output / "summary.json").read_text())
        summary.update(
            {
                "aggregate": score["metrics"],
                "matched_error_m": distribution(errors),
                "error_scope": "precise one-to-one matches only",
            }
        )
        write_json(output / "summary.json", summary)


def run(source: Path, output: Path, repeats: int) -> None:
    output.mkdir(parents=True, exist_ok=False)
    freeze(source, output / "fixture")
    manifest: dict[str, Any] = {
        "fixture": str((output / "fixture").resolve()),
        "fixture_manifest_sha256": sha256(output / "fixture/manifest.json"),
        "attempts": [],
    }
    write_json(output / "manifest.json", manifest)
    for repeat in range(repeats):
        # Rotate order across repetitions without changing the sample population.
        for arm in ARMS[repeat % 3 :] + ARMS[: repeat % 3]:
            path = output / f"{arm}-{repeat + 1}"
            command = [
                sys.executable,
                "-m",
                "chongming.benchmark_center_attribution",
                "--worker",
                str(output / "fixture"),
                arm,
                str(path / "evaluation"),
            ]
            result = supervise(
                command, path, wall_limit_s=300, memory_limit_bytes=4 * 1024**3, phase_limit_s=10
            )
            manifest["attempts"].append(
                {
                    "arm": arm,
                    "repeat": repeat + 1,
                    "status": result["status"],
                    "summary": str((path / "summary.json").resolve()),
                    "sha256": sha256(path / "summary.json"),
                }
            )
            write_json(output / "manifest.json", manifest)
            print(arm, repeat + 1, result["status"], flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=3, metavar=("FIXTURE", "ARM", "OUTPUT"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        fixture, arm, output = args.worker
        worker(Path(fixture), arm, Path(output))
    else:
        run(args.source, args.output, args.repeats)


if __name__ == "__main__":
    main()
