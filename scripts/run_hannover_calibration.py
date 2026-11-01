"""Render Hannover and evaluate auxiliary-camera visual calibration."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

from chongming.contracts.scene import ScenarioSpec
from chongming.joint_calibration import JointCalibrationRunner
from chongming.rendering.godot import close_default_renderer
from chongming.scenarios.authoring import compile_authoring
from chongming.scenarios.demo import demo_source


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    return float(value)


def _timing_stats(results: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = sorted(float(result[key]) for result in results if key in result)
    if not values:
        return {"p50_ms": None, "p95_ms": None, "max_ms": None}

    def percentile(rank: float) -> float:
        index = min(len(values) - 1, math.ceil(len(values) * rank) - 1)
        return round(values[index], 3)

    return {
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(values[-1], 3),
    }


def _load_with_offset(
    path: Path, offset: tuple[float, float, float]
) -> tuple[ScenarioSpec, dict[str, Any]]:
    document = json.loads(path.read_text())
    document["camera_position_offset_enu_m"] = list(offset)
    return compile_authoring(document, asset_root=path.parent), document


def run(
    scenario_path: Path,
    *,
    offset: tuple[float, float, float],
    render_every: int,
    max_steps: int | None,
) -> dict[str, Any]:
    spec, document = _load_with_offset(scenario_path, offset)
    runner = JointCalibrationRunner(spec)
    source = demo_source(spec, render_every=render_every, video_enabled=True)
    results: list[dict[str, Any]] = []
    scene_timings: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, step in enumerate(source.generate(spec)):
        if max_steps is not None and index >= max_steps:
            break
        results.extend(runner.process_step(step))
        if step.truth.timing is not None:
            scene_timings.append(
                {
                    "total_ms": round(step.truth.timing.total_ms, 3),
                    "gpu_ms": round(step.truth.timing.gpu_ms, 3),
                    "gpu_submissions": step.truth.timing.gpu_submissions,
                    "rendered": step.video.rendered,
                }
            )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    per_station: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        per_station.setdefault(result["station_id"], []).append(result)
    return {
        "scenario": {
            "path": str(scenario_path),
            "scenario_id": spec.scenario_id,
            "duration_s": document.get("duration_s"),
            "camera_position_offset_enu_m": list(offset),
            "render_every": render_every,
            "steps": len(scene_timings),
        },
        "stations": per_station,
        "performance": {
            "wall_ms": round(elapsed_ms, 3),
            "scene_timings": scene_timings,
            "calibration_samples": len(results),
            "calibration_processing_ms": round(
                sum(float(result["processing_ms"]) for result in results), 3
            ),
            "timing_ms": {
                "recognition": _timing_stats(results, "recognition_ms"),
                "estimation": _timing_stats(results, "estimation_ms"),
                "state": _timing_stats(results, "state_ms"),
                "total": _timing_stats(results, "processing_ms"),
                "process_cpu": _timing_stats(results, "processing_cpu_ms"),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Hannover 虚拟场景与视觉标定联合测试")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--position-offset",
        nargs=3,
        type=float,
        default=(0.005, 0.0, 0.0),
        metavar=("E", "N", "U"),
        help="共同相机位置扰动（米），默认 5 mm East",
    )
    parser.add_argument("--render-every", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.render_every <= 0:
        parser.error("--render-every must be positive")
    if args.max_steps is not None and args.max_steps <= 0:
        parser.error("--max-steps must be positive")
    offset: tuple[float, float, float] = tuple(
        _number(item, "position offset") for item in args.position_offset
    )  # type: ignore[assignment]
    try:
        report = run(
            args.scenario,
            offset=offset,
            render_every=args.render_every,
            max_steps=args.max_steps,
        )
    finally:
        close_default_renderer()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
