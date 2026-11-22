"""Offline range/body-size difficulty strata; matching gates remain unchanged."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from chongming_proto.truth import truth_pb2
from chongming_scorer.metrics import annotate
from chongming_scorer.storage import TestScenePackage


def range_to_size(
    optical_center: tuple[float, ...], target: tuple[float, ...], size_m: float
) -> float:
    if (
        len(optical_center) != 3
        or len(target) != 3
        or not all(math.isfinite(v) for v in (*optical_center, *target, size_m))
        or size_m <= 0
    ):
        raise ValueError("finite 3D positions and positive physical model diagonal required")
    distance = math.dist(optical_center, target)
    if distance <= 0:
        raise ValueError("difficulty requires positive camera-to-target range")
    return distance / size_m


def range_ratio_from_distance(range_m: float, size_m: float) -> float:
    if not math.isfinite(range_m) or range_m <= 0:
        raise ValueError("difficulty requires positive camera-to-target range")
    if not math.isfinite(size_m) or size_m <= 0:
        raise ValueError("physical model diagonal must be finite and positive")
    return range_m / size_m


def bin_bounds(ratio: float) -> tuple[float, float]:
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("difficulty ratio must be finite and positive")
    lower = 2.0 ** math.floor(math.log2(ratio))
    return lower, 2 * lower


def build_difficulty_index(scene: TestScenePackage) -> dict[str, dict[str, dict[str, Any]]]:
    """Build frame_id -> target_id difficulty strata index from a Test Scene."""
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for truth_frame in scene.iter_truth_frames():
        diagonals = {
            target.target_id: target.body_aabb_diagonal_m
            for target in truth_frame.targets
            if target.HasField("body_aabb_diagonal_m") and target.body_aabb_diagonal_m > 0
        }
        for proj in truth_frame.projections:
            if proj.occlusion_status == truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV:
                continue
            if not proj.HasField("frame_id") or not proj.HasField("range_m"):
                continue
            diagonal = diagonals.get(proj.target_id)
            if diagonal is None:
                continue
            ratio = range_ratio_from_distance(proj.range_m, diagonal)
            lower, upper = bin_bounds(ratio)
            index.setdefault(proj.frame_id, {})[proj.target_id] = {
                "range_m": proj.range_m,
                "body_aabb_diagonal_m": diagonal,
                "difficulty": ratio,
                "bin_lower": lower,
                "bin_upper": upper,
            }
    return index


def score_frame_rows(
    rows: Iterable[Mapping[str, Any]],
    index: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    gate_px: float = 10.0,
) -> dict[str, Any]:
    bins: dict[float, Counter[str]] = {}
    ranges: dict[float, list[float]] = {}
    fp = 0
    for row in rows:
        if row["warmup_excluded"]:
            continue
        fp += int(row["fp"])
        for target in annotate(row, gate_px)["targets"]:
            difficulty = index[row["frame_id"]][target["id"]]
            lower = float(difficulty["bin_lower"])
            counts = bins.setdefault(lower, Counter())
            counts["responsible_samples"] += 1
            counts["hits"] += int(bool(target["matched"]))
            counts["misses"] += int(not target["matched"])
            ranges.setdefault(lower, []).append(float(difficulty["difficulty"]))
    hits = sum(row["hits"] for row in bins.values())
    samples = sum(row["responsible_samples"] for row in bins.values())
    return {
        "responsible_samples": samples,
        "hits": hits,
        "false_positives": fp,
        "recall": hits / samples if samples else None,
        "precision": hits / (hits + fp) if hits + fp else None,
        "bins": [
            {
                "lower": lower,
                "upper": 2 * lower,
                **counts,
                "recall": counts["hits"] / counts["responsible_samples"],
                "observed_min": min(ranges[lower]),
                "observed_max": max(ranges[lower]),
            }
            for lower, counts in sorted(bins.items())
        ],
    }


def score(
    frames: Path,
    index: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    gate_px: float = 10.0,
) -> dict[str, Any]:
    with frames.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    return score_frame_rows(rows, index, gate_px=gate_px)


def score_difficulty_strata(
    scene: TestScenePackage,
    edge_frame_records: Sequence[Mapping[str, Any]],
    *,
    gate_px: float = 10.0,
) -> dict[str, Any] | None:
    """Compute difficulty strata across scored edge frames when truth carries size & range."""
    index = build_difficulty_index(scene)
    if not index:
        return None
    scored_rows = [row for row in edge_frame_records if not row["warmup_excluded"]]
    if not scored_rows:
        return None
    for row in scored_rows:
        frame_id = row["frame_id"]
        if frame_id not in index:
            return None
        if any(target_id not in index[frame_id] for target_id in row["eligible_targets_px"]):
            return None
    return score_frame_rows(scored_rows, index, gate_px=gate_px)
