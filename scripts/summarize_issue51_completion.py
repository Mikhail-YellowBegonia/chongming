"""Summarize completed native ViBe trials, physical strata and full regression evidence."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from chongming.benchmark_occupancy import sha256
from chongming.difficulty_evaluation import build_index, score
from chongming.evaluation_attempt import write_json


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "values": values,
        "mean": statistics.mean(values),
        "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
        "minimum": min(values),
        "maximum": max(values),
    }


def summarize(recording: Path, trials: Path, regression: Path, output: Path) -> None:
    manifest = json.loads((trials / "manifest.json").read_text())
    if any(row["status"] == "running" for row in manifest["attempts"]):
        raise ValueError("the experiment has not finished")
    frozen = json.loads((trials / "freeze.json").read_text())
    changed = [
        name for name, digest in frozen["source_sha256"].items() if sha256(Path(name)) != digest
    ]
    if changed:
        raise ValueError(f"frozen source files changed: {changed}")
    output.mkdir(parents=True, exist_ok=False)
    index = build_index(recording, output)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    strata = {}
    attempts = []
    for entry in manifest["attempts"]:
        path = Path(entry["summary"])
        report = json.loads(path.read_text())
        if sha256(path) != entry["summary_sha256"]:
            raise ValueError(f"trial summary changed: {path}")
        attempts.append(entry)
        if report["status"] != "complete" or entry["phase"] != "full":
            if report["aggregate"] is not None:
                raise ValueError("incomplete trials must not have aggregate quality")
            continue
        frames = path.parent / "evaluation/frames.jsonl"
        quality = score(frames, index)
        original = report["aggregate"]
        if (quality["responsible_samples"], quality["hits"], quality["false_positives"]) != (
            original["tp"] + original["fn"],
            original["tp"],
            original["fp"],
        ):
            raise ValueError("stratified counts do not reproduce original aggregate")
        strata[f"{entry['method']}-seed{entry['seed']}"] = {
            "frames": str(frames.resolve()),
            "frames_sha256": sha256(frames),
            **quality,
        }
        stations = list(report["stations"].values())
        timing = [station["timing_ms_including_warmup"]["pipeline_ms"] for station in stations]
        count = sum(row["samples"] for row in timing)
        pipeline_ms = sum(row["mean"] * row["samples"] for row in timing) / count
        stage = {"difference-on": "FrameDifferenceStage", "mog2-tuned": "MOG2Stage"}.get(
            entry["method"], "VibeStage"
        )
        foreground = [station["stage_timing_ms_including_warmup"][stage] for station in stations]
        grouped[entry["method"]].append(
            {
                "seed": entry["seed"],
                "quality": original,
                "pipeline_ms": pipeline_ms,
                "foreground_ms": sum(row["mean"] * row["samples"] for row in foreground) / count,
                "worker_rss_bytes": report["supervision"]["rss_peak_bytes"],
            }
        )
    methods = {
        method: {
            "trials": rows,
            "metrics": {
                key: distribution([row["quality"][key] for row in rows])
                for key in ("tp", "fp", "fn", "recall", "precision", "f1")
            },
            "pipeline_ms": distribution([row["pipeline_ms"] for row in rows]),
            "foreground_ms": distribution([row["foreground_ms"] for row in rows]),
        }
        for method, rows in grouped.items()
    }
    log = regression.read_text()
    match = re.search(r"Ran (\d+) tests in ([\d.]+)s", log)
    if match is None:
        raise ValueError("full regression did not finish")
    failures = re.findall(r"^FAIL: (.+)$", log, re.MULTILINE)
    errors = re.findall(r"^ERROR: (test_.*)$", log, re.MULTILINE)
    skips = re.findall(r"^(test_.*) \.\.\. skipped (.+)$", log, re.MULTILINE)
    report = {
        "format": "issue51-completion-v1",
        "summarizer_sha256": sha256(Path(__file__)),
        "trial_manifest": str((trials / "manifest.json").resolve()),
        "trial_manifest_sha256": sha256(trials / "manifest.json"),
        "attempts": attempts,
        "methods": methods,
        "difficulty": strata,
        "frozen_source_verified_unchanged": True,
        "regression": {
            "log": str(regression.resolve()),
            "sha256": sha256(regression),
            "tests": int(match[1]),
            "duration_s": float(match[2]),
            "failures": failures,
            "errors": errors,
            "skips": skips,
            "passed": int(match[1]) - len(failures) - len(errors) - len(skips),
            "exit_code": 1 if failures or errors else 0,
            "environment": {
                key: frozen[key] for key in ("platform", "machine", "python", "git_revision")
            },
            "lock_sha256": {
                name: frozen["source_sha256"][name] for name in ("flake.lock", "uv.lock")
            },
            "native_vibe_library": json.loads((trials / "official-build.json").read_text())[
                "library"
            ],
            "command": "python -X faulthandler -m unittest discover -s tests -v",
        },
    }
    write_json(output / "summary.json", report)
    for method, row in methods.items():
        print(
            method,
            {
                key: round(row["metrics"][key]["mean"] * 100, 3)
                for key in ("recall", "precision", "f1")
            },
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--trials", required=True, type=Path)
    parser.add_argument("--regression-log", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summarize(args.recording, args.trials, args.regression_log, args.output)


if __name__ == "__main__":
    main()
