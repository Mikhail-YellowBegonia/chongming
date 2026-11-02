"""Exhaustive candidate intersections, isolated from formal association and tracking."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from chongming.contracts import development as d
from chongming.contracts import fusion as f
from chongming.modules.fusion import CalibrationWeightingModule
from chongming.modules.fusion_association import CrossStationAssociationModule
from chongming.modules.fusion_geometry import (
    _STATION_SCORE,
    GeometryRefinementModule,
    _event_id,
    _exposure_span,
    _line_fit,
    _reference_time,
)
from chongming.modules.fusion_motion import midpoint_span
from chongming.modules.fusion_postintersection import candidate_groups
from chongming.performance import distribution
from chongming.scenarios.run import _json, _provenance
from chongming.scenarios.serialization import _decode


def intersect(
    window: f.SealedObservationWindow,
    candidates: f.CandidateBatch,
    policy: f.FusionPolicy,
) -> list[dict[str, Any]]:
    """Retain raw least-squares points even when production gates reject a group."""
    weights = CalibrationWeightingModule(rules=policy.admission.calibration_usage_rules)
    geometry = GeometryRefinementModule(
        center_session_id=window.key.center_session_id,
        world_frame_id=candidates.search.world_frame_id,
        policy=policy.geometry,
        weights=weights,
    )
    association = CrossStationAssociationModule(
        policy=policy.association, motion_policy=policy.geometry, weights=weights
    )
    available = {_event_id(ray): ray for ray in window.rays}
    rows = []
    for ids, candidate_ids in candidate_groups(candidates).items():
        chosen = tuple(available[event_id] for event_id in ids)
        reference = _reference_time(chosen)
        group = f.AssociatedRayGroup(
            hypothesis_id="exhaustive/" + hashlib.sha256(repr(ids).encode()).hexdigest(),
            candidate_id=candidate_ids[0],
            window=window.key,
            reference_time_ns=reference,
            support_exposure_span_ns=_exposure_span(chosen),
            supporting_ray_event_ids=ids,
            supporting_station_ids=tuple(ray.event.envelope.station_id for ray in chosen),
            prior_track_id=None,
            motion_alignment_model=f.MotionAlignmentModel.NARROW_WINDOW_WITHOUT_PRIOR,
            score=float(len(chosen)),
            score_definition=_STATION_SCORE,
        )
        # Each group is evaluated independently: overlapping evidence is intentional
        # here, never a batch of disjoint production associations or track updates.
        result = geometry._refine_group(window, group, available, ())
        reasons = ["geometry:" + reason.value for reason in result.reasons]
        raw_position = None
        residuals: list[float] = []
        depths: list[float] = []
        rank = None
        condition = None
        try:
            point, errors, forward, rank, singular = _line_fit(chosen)
        except np.linalg.LinAlgError:
            reasons.append("raw:solver_failure")
        else:
            residuals, depths = errors.tolist(), forward.tolist()
            if rank < 3:
                reasons.append("raw:nonunique_solution")
            else:
                raw_position = point.tolist()
                condition = float((singular[0] / singular[-1]) ** 2)
            if np.any(forward < 0):
                reasons.append("raw:nonforward_solution")
            if np.any(point < candidates.search.min_enu_m) or np.any(
                point > candidates.search.max_enu_m
            ):
                reasons.append("raw:outside_search_volume")
            if candidates.search.forward_range_m is not None:
                near, far = candidates.search.forward_range_m
                if np.any(forward < near) or np.any(forward > far):
                    reasons.append("raw:outside_forward_range")
            if float(errors.max()) > policy.association.spatial_gate_m:
                reasons.append("association:excessive_residual")
        if midpoint_span(chosen) > policy.association.temporal_gate_ns:
            reasons.append("association:temporal_gate_failed")
        gate, _, _ = association._gate(chosen, candidates.search, reference, None)
        if gate is not None:
            reasons.append("association:" + gate.value)
        rows.append(
            {
                "hypothesis_id": group.hypothesis_id,
                "reference_time_ns": reference,
                "supporting_ray_event_ids": ids,
                "candidate_ids": sorted(set(candidate_ids)),
                "raw_position_enu_m": raw_position,
                "raw_rank": rank,
                "raw_condition": condition,
                "raw_residuals_m": residuals,
                "raw_forward_depths_m": depths,
                "classifications": sorted(set(reasons)),
                "geometry_measurement": result.measurement,
            }
        )
    return rows


def diagnose(recording: Path, output: Path) -> dict[str, Any]:
    registration = _decode(
        json.loads((recording / "center-registration.json").read_text()), d.CenterRegistration
    )
    output.mkdir(parents=True, exist_ok=False)
    for name in ("scenario.json", "center-input.jsonl", "center-registration.json"):
        shutil.copyfile(recording / name, output / name)
    counts: Counter[str] = Counter()
    timings = []
    windows = 0
    status = "failed"
    try:
        with (
            (recording / "center-output.jsonl").open() as source,
            (output / "center-output.jsonl").open("x") as results,
            (output / "solutions.jsonl").open("x") as details,
        ):
            for line in source:
                row = json.loads(line)
                for batch in row["batches"]:
                    sealed = {
                        w["key"]["start_ns"]: _decode(w, f.SealedObservationWindow)
                        for w in batch["window_update"]["sealed_windows"]
                    }
                    for raw_candidates in batch["candidates"]:
                        candidates = _decode(raw_candidates, f.CandidateBatch)
                        window = sealed[candidates.window.start_ns]
                        began = time.perf_counter()
                        solutions = intersect(window, candidates, registration.policy)
                        timings.append((time.perf_counter() - began) * 1000)
                        raw_points = []
                        accepted = []
                        for solution in solutions:
                            counts["groups"] += 1
                            counts.update(solution["classifications"])
                            measurement = solution["geometry_measurement"]
                            accepted.append(measurement)
                            if solution["raw_position_enu_m"] is not None:
                                counts["raw_points"] += 1
                                raw_points.append(
                                    {
                                        "measurement_id": f"raw/{window.key.start_ns}/{solution['hypothesis_id']}",
                                        "reference_time_ns": solution["reference_time_ns"],
                                        "position_enu_m": solution["raw_position_enu_m"],
                                    }
                                )
                            counts["geometry_status:" + measurement.status.value] += 1
                            details.write(_json({"window": window.key, **solution}) + "\n")
                        results.write(
                            _json(
                                {
                                    "measurements": accepted,
                                    "diagnostic_raw_solutions": raw_points,
                                    "batches": [{"window_update": {"sealed_windows": [window]}}],
                                }
                            )
                            + "\n"
                        )
                        windows += 1
                        if windows % 100 == 0:
                            print(f"Intersected {windows} windows", flush=True)
        status = "complete"
    finally:
        report = {
            "format": "center-exhaustive-diagnostic-v1",
            "status": status,
            "source": str(recording.resolve()),
            "scope": "all unique cross-station groups supported by voxel candidates; no association selection, no tracking, no truth input",
            "raw_solution": "unweighted line least-squares diagnostic; rejected solutions are not production measurements",
            "windows": windows,
            "counts": dict(counts),
            "intersection_ms": distribution(timings),
            "provenance": _provenance(),
        }
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.recording, args.output)["counts"], indent=2))


if __name__ == "__main__":
    main()
