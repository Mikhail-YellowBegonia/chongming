"""Build citation tables from frozen issue #51 comparison manifests."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from chongming.benchmark_occupancy import sha256
from chongming.evaluation_attempt import write_json


def spread(values: list[float]) -> dict[str, Any]:
    return {
        "runs": len(values),
        "values": values,
        "mean": statistics.mean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values),
        "max": max(values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center", required=True, type=Path)
    parser.add_argument("--device", required=True, type=Path)
    parser.add_argument("--invalid-center", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {"center": {}, "device": {}, "attempts": [], "sources": {}}
    for kind, root in (("center", args.center), ("device", args.device)):
        manifest = json.loads((root / "manifest.json").read_text())
        result["sources"][kind] = {
            "manifest": str((root / "manifest.json").resolve()),
            "sha256": sha256(root / "manifest.json"),
        }
        arms = sorted({entry["arm"] for entry in manifest["attempts"]})
        for arm in arms:
            entries = [entry for entry in manifest["attempts"] if entry["arm"] == arm]
            reports = [json.loads(Path(entry["summary"]).read_text()) for entry in entries]
            assert all(report["status"] == "complete" for report in reports)
            quality = [
                report["aggregate"]["precise"] if kind == "center" else report["aggregate"]
                for report in reports
            ]
            assert all(q == quality[0] for q in quality), "repeat quality changed"
            summary = {
                "quality": quality[0],
                "peak_rss_MiB": spread(
                    [r["supervision"]["rss_peak_bytes"] / 1024**2 for r in reports]
                ),
            }
            if kind == "center":
                summary["algorithm_ms"] = spread(
                    [r["timings"]["algorithm_ms"]["mean"] for r in reports]
                )
                summary["phases_ms"] = {
                    key: spread([r["timings"][key]["mean"] for r in reports])
                    for key in ("candidate_ms", "group_ms", "solve_ms", "dedup_ms")
                }
                summary["matched_error_m"] = reports[0]["matched_error_m"]
                rows = json.loads(
                    (
                        Path(entries[0]["summary"]).parent / "evaluation/windows-summary.json"
                    ).read_text()
                )
                summary["groups"] = sum(row["groups"] for row in rows)
                summary["solver_calls"] = sum(row["solver_calls"] for row in rows)
                summary["rejected_groups"] = sum(
                    row["groups"] - len(row["located_evidence_ids"]) for row in rows
                )
                stations = sorted({s for row in rows for s in row["rays_per_station"]})
                summary["rays_per_station"] = {
                    s: {
                        "min": min(row["rays_per_station"].get(s, 0) for row in rows),
                        "max": max(row["rays_per_station"].get(s, 0) for row in rows),
                    }
                    for s in stations
                }
                summary["distinct_voxels"] = {
                    "min": min(row["supported_voxels"] for row in rows),
                    "max": max(row["supported_voxels"] for row in rows),
                }
                summary["candidate_limits"] = [
                    reason for row in rows for reason in row["candidate_reasons"]
                ]
            else:
                summary["pipeline_ms"] = spread(
                    [
                        statistics.mean(
                            s["timing_ms_scored_frames"]["pipeline_ms"]["mean"]
                            for s in r["stations"].values()
                        )
                        for r in reports
                    ]
                )
                summary["regions"] = entries[0]["regions"]
                summary["motion"] = entries[0]["motion"]
                assert all(
                    entry["regions"] == entries[0]["regions"]
                    and entry["motion"] == entries[0]["motion"]
                    for entry in entries
                )
                operator = []
                candidates = []
                for entry in entries:
                    samples = []
                    load = []
                    with (
                        Path(entry["summary"]).parent / "evaluation/frames.jsonl"
                    ).open() as handle:
                        for line in handle:
                            row = json.loads(line)
                            if row["warmup_excluded"]:
                                continue
                            stage = (
                                "MOG2Stage" if arm.startswith("mog2") else "FrameDifferenceStage"
                            )
                            samples.append(row["stage_timings_ms"][stage])
                            load.append(row["counts"]["accepted_candidates"])
                    operator.append(statistics.mean(samples))
                    candidates.append({"mean": statistics.mean(load), "max": max(load)})
                summary["foreground_ms"] = spread(operator)
                summary["candidate_load_scored"] = candidates
            result[kind][arm] = summary
            for entry in entries:
                result["attempts"].append({"kind": kind, **entry})
    left = json.loads((args.center / "voxel-refined-1/evaluation/windows-summary.json").read_text())
    right = json.loads(
        (args.center / "direct-refined-1/evaluation/windows-summary.json").read_text()
    )
    covered = valid = 0
    for voxel, direct in zip(left, right, strict=True):
        assert voxel["window_start_ns"] == direct["window_start_ns"]
        proposed = {tuple(tuple(e) for e in ids) for ids in voxel["evidence_ids"]}
        accepted = {tuple(tuple(e) for e in ids) for ids in direct["located_evidence_ids"]}
        covered += len(proposed & accepted)
        valid += len(accepted)
    result["center_coverage"] = {
        "direct_accepted_support_groups": valid,
        "retained_by_voxel": covered,
        "coverage": covered / valid if valid else None,
    }
    result["center_speed_ratio"] = (
        result["center"]["direct-refined"]["algorithm_ms"]["mean"]
        / result["center"]["voxel-refined"]["algorithm_ms"]["mean"]
    )
    invalid = json.loads((args.invalid_center / "manifest.json").read_text())
    write_json(
        args.invalid_center / "invalidation.json",
        {
            "reason": "empty-window warmup used window midpoint instead of actual exposure; initial fixture is diagnostic only",
            "superseded_by": str(args.center.resolve()),
        },
    )
    for entry in invalid["attempts"]:
        result["attempts"].append(
            {
                "kind": "center",
                "arm": entry["arm"],
                "repeat": entry["repeat"],
                "status": "invalid_fixture",
                "quality": None,
                "summary": entry["summary"],
                "sha256": entry["sha256"],
                "reason": "warmup_schedule_boundary",
            }
        )
    result["sources"]["invalid_center"] = {
        "manifest": str((args.invalid_center / "manifest.json").resolve()),
        "sha256": sha256(args.invalid_center / "manifest.json"),
        "invalidation_sha256": sha256(args.invalid_center / "invalidation.json"),
    }
    write_json(args.output / "summary.json", result)
    for arm, stats in result["center"].items():
        q = stats["quality"]
        print(
            "CENTER",
            arm,
            "R/P/F1",
            q["recall"],
            q["precision"],
            q["f1"],
            "TP/FP/FN",
            q["tp"],
            q["fp"],
            q["fn"],
            "ms",
            stats["algorithm_ms"],
            "groups",
            stats["groups"],
            "rejects",
            stats["rejected_groups"],
            "errors",
            stats["matched_error_m"],
        )
    print("COVERAGE", result["center_coverage"], "RATIO", result["center_speed_ratio"])
    for arm, stats in result["device"].items():
        q = stats["quality"]
        print(
            "DEVICE",
            arm,
            "R/P/F1",
            q["recall"],
            q["precision"],
            q["f1"],
            "ms",
            stats["pipeline_ms"],
            "foreground",
            stats["foreground_ms"],
            "RSS",
            stats["peak_rss_MiB"],
            "load",
            stats["candidate_load_scored"][0],
        )


if __name__ == "__main__":
    main()
