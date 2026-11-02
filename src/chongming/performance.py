"""Read-only performance reports from recorded evidence; no renderer is started."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def distribution(values: list[float]) -> dict[str, Any]:
    """Nearest-rank percentiles, including cold-start samples."""
    if not values:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "mean": math.fsum(values) / len(values),
        "p50": ordered[math.ceil(len(values) * 0.5) - 1],
        "p95": ordered[math.ceil(len(values) * 0.95) - 1],
        "max": ordered[-1],
    }


def report(recording: Path) -> dict[str, Any]:
    """Summarize measured stages without treating nested durations as additive."""
    manifest = json.loads((recording / "manifest.json").read_text())
    summary = json.loads((recording / "summary.json").read_text())
    samples: dict[str, list[float]] = {}
    times: list[int] = []
    for line in (recording / "timing.jsonl").read_text().splitlines():
        row = json.loads(line)
        times.append(row["sample_time"]["time_ns"])
        stages = row["stages"]
        durations = {key: value for key, value in stages.items() if key.endswith("_ms")}
        durations["evidence_ms"] = row["evidence_ms"]
        # Evidence is written after generation; render_ms already includes gpu_ms.
        for key, value in durations.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{key} must be a numeric duration")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
            samples.setdefault(key, []).append(float(value))
        samples.setdefault("step_wall_ms", []).append(stages["total_ms"] + row["evidence_ms"])
    center_path = recording / "center-output.jsonl"
    if center_path.exists():
        for line in center_path.read_text().splitlines():
            value = json.loads(line)["center_ms"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("center_ms must be a numeric duration")
            if not math.isfinite(value) or value < 0:
                raise ValueError("center_ms must be finite and nonnegative")
            samples.setdefault("center_ms", []).append(float(value))
    metrics = {key: distribution(values) for key, values in sorted(samples.items())}
    wall = samples.get("step_wall_ms", [])
    return {
        "format": "chongming-performance-v1",
        "recording": str(recording.resolve()),
        "provenance": {
            key: manifest.get(key)
            for key in (
                "code_revision",
                "dependency_lock_digest",
                "authoring_content_digest",
                "machine",
                "platform",
                "dependencies",
            )
        },
        "status": summary["status"],
        "counts": summary["counts"],
        "metrics_ms": metrics,
        "recorded_span_s": None if len(times) < 2 else (max(times) - min(times)) / 1e9,
        "measured_step_wall_s": math.fsum(wall) / 1000 if wall else None,
        "throughput_steps_per_s": len(wall) * 1000 / math.fsum(wall)
        if wall and math.fsum(wall) > 0
        else None,
        "unavailable": ["freeview_fps", "gpu_hardware_ms", "visual_quality", "tracking_quality"]
        + ([] if "center_ms" in metrics else ["center_ms"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", type=Path, help="New JSON report; existing files are refused")
    args = parser.parse_args()
    result = report(args.recording)
    document = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as output:
            output.write(document)
    else:
        print(document, end="")


if __name__ == "__main__":
    main()
