"""Official native ViBe comparisons with fixed postprocessing and explicit RNG seeds."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from chongming.benchmark_occupancy import arm_policies, freeze_recording, sha256, supervise
from chongming.evaluation_attempt import write_json

METHODS = (
    "difference-on",
    "mog2-tuned",
    "vibe-off",
    "vibe-on",
    "vibe-off-erosion",
    "vibe-on-erosion",
)


def make_policy(method: str, seed: int, build: dict[str, Any]) -> dict[str, Any]:
    controls = arm_policies(Path.cwd())
    if method in ("difference-on", "mog2-tuned"):
        return controls[method]
    document: dict[str, Any] = json.loads(json.dumps(controls["mog2-tuned"]))
    for station in document.values():
        detection = station["detection"]
        detection.update(
            {
                "algorithm_id": "vibe-workbench-v1",
                "mode": "vibe",
                "apply_occupancy": method in ("vibe-on", "vibe-on-erosion"),
                "apply_erosion": method.endswith("erosion"),
                "blob_dilation_size": 3,
                "vibe_library_path": build["library"],
                "vibe_library_sha256": build["library_sha256"],
                "vibe_seed": seed,
            }
        )
        for key in (
            "mog2_history",
            "mog2_variance_threshold",
            "mog2_learning_rate",
            "mog2_detect_shadows",
        ):
            detection[key] = None
    return document


def run(recording: Path, build_file: Path, output: Path) -> None:
    build = json.loads(build_file.read_text())
    if sha256(Path(build["library"])) != build["library_sha256"]:
        raise ValueError("official ViBe build changed")
    frozen = freeze_recording(recording, Path.cwd())
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "freeze.json", frozen)
    shutil.copyfile(build_file, output / "official-build.json")
    manifest: dict[str, Any] = {
        "format": "official-vibe-comparison-v1",
        "freeze_sha256": sha256(output / "freeze.json"),
        "official_build_sha256": sha256(output / "official-build.json"),
        "seeds": [42, 43, 44],
        "budgets": {
            "probe_frames_per_station": 360,
            "full_frames_per_station": 900,
            "probe_wall_s": 300,
            "full_wall_s": 1800,
            "memory_bytes": 4 * 1024**3,
            "phase_s": 2,
            "candidate_limit": 100,
            "overload_hold_s": 10,
        },
        "comparison": "official unmodified current C defaults30/10/2/8; grayscale; no demo median filter; shared postprocessing and tracker; C kernel is sequential; OpenCV threads10; postprocessing blob dilation3, except selected frame-difference control5",
        "planned": [{"method": name, "seed": seed} for seed in (42, 43, 44) for name in METHODS],
        "attempts": [],
    }
    write_json(output / "manifest.json", manifest)
    try:
        for repeat, seed in enumerate((42, 43, 44)):
            for method in METHODS[repeat:] + METHODS[:repeat]:
                policy = output / f"{method}-seed{seed}.json"
                write_json(policy, make_policy(method, seed, build))
                phases = ("probe", "full") if method.startswith("vibe") else ("full",)
                for phase in phases:
                    path = output / f"{method}-seed{seed}-{phase}"
                    entry = {
                        "method": method,
                        "seed": seed,
                        "phase": phase,
                        "planned_frames_per_station": 360 if phase == "probe" else 900,
                        "policy_sha256": sha256(policy),
                        "status": "running",
                    }
                    manifest["attempts"].append(entry)
                    write_json(output / "manifest.json", manifest)
                    command = [
                        sys.executable,
                        "-m",
                        "chongming.evaluate_recording",
                        str(recording.resolve()),
                        "--policy",
                        str(policy.resolve()),
                        "--output",
                        str((path / "evaluation").resolve()),
                        "--gate-px",
                        "10",
                        "--size-k",
                        "0.5",
                        "--warmup-s",
                        "15",
                        "--opencv-threads",
                        "10",
                        "--candidate-limit",
                        "100",
                        "--overload-hold-s",
                        "10",
                        "--phase-limit-s",
                        "2",
                    ]
                    if phase == "probe":
                        command.extend(["--max-frames-per-station", "360"])
                    report = supervise(
                        command,
                        path,
                        wall_limit_s=300 if phase == "probe" else 1800,
                        memory_limit_bytes=4 * 1024**3,
                        phase_limit_s=2,
                    )
                    entry.update(
                        {
                            "status": report["status"],
                            "stop_reason": report.get("stop_reason"),
                            "summary": str((path / "summary.json").resolve()),
                            "summary_sha256": sha256(path / "summary.json"),
                            "quality": report.get("aggregate"),
                        }
                    )
                    write_json(output / "manifest.json", manifest)
                    print(method, seed, phase, entry["status"], flush=True)
                    if report["status"] == "failed":
                        raise RuntimeError(f"unexpected worker failure; inspect {path}")
                    if report["status"] == "resource_limit":
                        break
    finally:
        write_json(output / "manifest.json", manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--build-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.recording, args.build_manifest, args.output)


if __name__ == "__main__":
    main()
