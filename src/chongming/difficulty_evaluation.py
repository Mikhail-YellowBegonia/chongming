"""Offline range/body-size difficulty strata; matching gates remain unchanged."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from chongming.benchmark_occupancy import sha256
from chongming.center_evaluation import model_aabb_diagonal
from chongming.evaluation import annotate
from chongming.evaluation_attempt import write_json
from chongming.scenarios.serialization import load_compiled_scenario


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


def bin_bounds(ratio: float) -> tuple[float, float]:
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("difficulty ratio must be finite and positive")
    lower = 2.0 ** math.floor(math.log2(ratio))
    return lower, 2 * lower


def build_index(recording: Path, output: Path) -> dict[str, dict[str, Any]]:
    spec = load_compiled_scenario(recording / "scenario.json", asset_root=Path("scenarios"))
    sizes = {
        str(target.truth_object_id): model_aabb_diagonal(target.geometry)
        for target in spec.scene_definition.flying_objects
    }
    index: dict[str, dict[str, Any]] = {}
    with (
        (recording / "truth.jsonl").open() as handle,
        (output / "difficulty-samples.jsonl").open("x") as evidence,
    ):
        for line in handle:
            for frame in json.loads(line)["camera_frames"]:
                if frame["camera_role"] != "main":
                    continue
                values = {}
                for target in frame["targets"]:
                    if not target["projection"]["center_in_field_of_view"]:
                        continue
                    target_id = target["truth_object_id"]
                    sight = target["visibility"]["sight_segment"]
                    center = tuple(sight["optical_center_enu_m"])
                    point = tuple(sight["reference_point_enu_m"])
                    ratio = range_to_size(center, point, sizes[target_id])
                    lower, upper = bin_bounds(ratio)
                    values[target_id] = {
                        "range_m": math.dist(center, point),
                        "body_aabb_diagonal_m": sizes[target_id],
                        "difficulty": ratio,
                        "bin_lower": lower,
                        "bin_upper": upper,
                    }
                index[frame["frame_id"]] = values
                evidence.write(
                    json.dumps(
                        {
                            "frame_id": frame["frame_id"],
                            "station_id": frame["station_id"],
                            "capture_time": frame["capture_time"],
                            "targets": values,
                        },
                        allow_nan=False,
                    )
                    + "\n"
                )
    write_json(
        output / "definition.json",
        {
            "formula": "camera-to-target Euclidean range / physical body-space model AABB diagonal after asset scaling",
            "scope": "per main station and target exposure; no station aggregation; offline truth only",
            "bins": "fixed powers of two, lower inclusive and upper exclusive; declared independently of detections",
            "matching": "unchanged max(10px,0.5*projected_AABB_diagonal_px); this ratio is not a matching gate",
            "precision": "reported globally; unmatched predictions have no known true target size and no assigned difficulty",
            "source_sha256": {
                name: sha256(recording / name)
                for name in ("scenario.json", "truth.jsonl", "truth-static.json")
            },
            "model_diagonals_m": sizes,
            "samples_sha256": sha256(output / "difficulty-samples.jsonl"),
            "issue_comment": "https://github.com/Mikhail-YellowBegonia/chongming/issues/51#issuecomment-6760259305",
        },
    )
    return index


def score(frames: Path, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    bins: dict[float, Counter[str]] = {}
    ranges: dict[float, list[float]] = {}
    fp = 0
    with frames.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["warmup_excluded"]:
                continue
            fp += row["fp"]
            for target in annotate(row, 10)["targets"]:
                difficulty = index[row["frame_id"]][target["id"]]
                lower = difficulty["bin_lower"]
                counts = bins.setdefault(lower, Counter())
                counts["responsible_samples"] += 1
                counts["hits"] += target["matched"]
                counts["misses"] += not target["matched"]
                ranges.setdefault(lower, []).append(difficulty["difficulty"])
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", nargs="+", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    index = build_index(args.recording, args.output)
    for path in args.frames:
        if json.loads((path.parent / "summary.json").read_text())["status"] != "complete":
            raise ValueError("full difficulty scoring requires a completed full-input evaluation")
    reports = {
        str(path.resolve()): {"frames_sha256": sha256(path), **score(path, index)}
        for path in args.frames
    }
    write_json(args.output / "summary.json", reports)
    for name, report in reports.items():
        print(name, report["responsible_samples"], report["recall"], flush=True)


if __name__ == "__main__":
    main()
