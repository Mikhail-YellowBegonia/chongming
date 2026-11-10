"""Bounded Hannover recording comparisons for issue #51; no scene rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from chongming.evaluation_attempt import write_json

ARMS = ("difference-on", "difference-off", "mog2-on", "mog2-off", "mog2-tuned")
SUPPLEMENTAL_ARMS = ("difference-off-dilate3", "mog2-off-dilate3")
EROSION_ARMS = ("difference-off-erode3-dilate3", "mog2-off-erode3-dilate3")


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def arm_policies(root: Path) -> dict[str, dict[str, Any]]:
    difference = json.loads(
        (root / "scenarios/policies/hannover_vision_inc26_dilate15.json").read_text()
    )
    mog2 = json.loads((root / "scenarios/policies/hannover_mog2_occupancy_start.json").read_text())
    tuned = json.loads(
        (root / "scenarios/policies/hannover_mog2_occupancy_tuned_control.json").read_text()
    )
    arms = {}
    for name in (*ARMS, *SUPPLEMENTAL_ARMS, *EROSION_ARMS):
        source = tuned if name == "mog2-tuned" else mog2 if name.startswith("mog2") else difference
        policy = json.loads(json.dumps(source))
        for station in policy.values():
            station["detection"]["apply_occupancy"] = "-off" not in name
            if name in (*SUPPLEMENTAL_ARMS, *EROSION_ARMS):
                station["detection"]["blob_dilation_size"] = 3
            if name in EROSION_ARMS:
                station["detection"]["apply_erosion"] = True
                station["detection"]["morphology_kernel_size"] = 3
        arms[name] = policy
    return arms


def freeze_recording(recording: Path, root: Path) -> dict[str, Any]:
    """Hash all input files, and validate video hashes and main exposure ordering."""
    video = json.loads((recording / "video.json").read_text())
    streams = {entry["path"]: entry for entry in video["streams"].values()}
    inputs = {}
    for path in sorted(recording.rglob("*")):
        if path.is_file():
            relative = path.relative_to(recording).as_posix()
            inputs[relative] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    for path, entry in streams.items():
        if inputs[path]["sha256"] != entry["sha256"]:
            raise ValueError(f"recorded video digest mismatch: {path}")
    counts: Counter[str] = Counter()
    scored: Counter[str] = Counter()
    origins: dict[str, int] = {}
    last: dict[str, int] = {}
    paths: dict[str, str] = {}
    with (recording / "online.jsonl").open() as online:
        for line in online:
            for frame in json.loads(line)["frames"]:
                if frame["camera_role"] != "main":
                    continue
                station = frame["station_id"]
                capture = frame["capture_time"]
                ns = (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2
                if frame["frame_index"] != counts[station] or (
                    station in last and ns <= last[station]
                ):
                    raise ValueError("main video indices or exposure order are invalid")
                if station in paths and paths[station] != frame["path"]:
                    raise ValueError("main video changed within recording")
                paths[station], last[station] = frame["path"], ns
                origins.setdefault(station, ns)
                counts[station] += 1
                scored[station] += ns - origins[station] >= 15_000_000_000
    if sorted(counts.values()) != [900, 900, 900] or sorted(scored.values()) != [449, 449, 449]:
        raise ValueError("benchmark requires Hannover's three 900-frame main streams")
    for station, video_path in paths.items():
        if streams[video_path]["frame_count"] != counts[station]:
            raise ValueError("main frame count differs from video metadata")
    sources = [*root.joinpath("src").rglob("*.py"), *root.joinpath("tests").rglob("*.py")]
    sources += [root / name for name in ("flake.nix", "flake.lock", "uv.lock", "pyproject.toml")]
    sources += list(root.joinpath("scenarios/policies").glob("*.json"))
    return {
        "recording": str(recording.resolve()),
        "inputs": inputs,
        "main_frames": dict(counts),
        "scored_frames": dict(scored),
        "warmup_origins_ns": origins,
        "source_sha256": {p.relative_to(root).as_posix(): sha256(p) for p in sorted(sources)},
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=root, text=True),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def history_index(results: Path) -> list[dict[str, Any]]:
    """Index archived reports without inventing missing failure or implementation context."""
    records = []
    for path in sorted(results.rglob("summary.json")):
        if not any(token in str(path.relative_to(results)) for token in ("device", "mog2", "vibe")):
            continue
        report = json.loads(path.read_text())
        records.append(
            {
                "report": str(path.resolve()),
                "sha256": sha256(path),
                "status": report.get("status", "not archived"),
                "input": report.get("recording", "not archived"),
                "implementation": report.get("evaluation_provenance", "not archived"),
                "policies": report.get("policies", "not archived"),
                "termination_reason": report.get(
                    "stop_reason", report.get("error", "not archived")
                ),
                "failure_stage": report.get("stop_stage", "not archived"),
                "command": report.get("command", "not archived"),
                "screenshots_logs": "not indexed; inspect the original output directory",
            }
        )
    return records


def supervise(
    command: list[str],
    output: Path,
    *,
    wall_limit_s: float,
    memory_limit_bytes: int,
    phase_limit_s: float,
) -> dict[str, Any]:
    """Supervise one worker, retaining a summary even after SIGKILL or startup errors."""
    output.mkdir(parents=True, exist_ok=False)
    work = output / "evaluation"
    started = time.monotonic()
    rss_peak = 0
    progress: dict[str, Any] = {}
    stop_reason = None
    with (output / "worker.log").open("x") as log:
        try:
            worker = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        except OSError as error:
            report = {
                "status": "failed",
                "stop_reason": "worker_launch_error",
                "stop_stage": "startup",
                "aggregate": None,
                "error": {
                    "type": type(error).__name__,
                    "errno": error.errno,
                    "message": str(error),
                },
                "attempt": {"processed_frames": 0, "scored_frames": 0, "current_frame": None},
                "supervision": {
                    "command": command,
                    "exit_code": None,
                    "wall_s": time.monotonic() - started,
                    "rss_peak_bytes": 0,
                    "limits": {
                        "wall_s": wall_limit_s,
                        "memory_bytes": memory_limit_bytes,
                        "phase_s": phase_limit_s,
                    },
                },
            }
            write_json(output / "summary.json", report)
            return report
        try:
            while worker.poll() is None:
                now = time.monotonic()
                snapshot = work / "progress.json"
                if snapshot.exists():
                    progress = json.loads(snapshot.read_text())
                rss = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(worker.pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if rss.returncode == 0 and rss.stdout.strip():
                    rss_peak = max(rss_peak, int(rss.stdout.strip()) * 1024)
                if now - started > wall_limit_s:
                    stop_reason = "wall_time_limit"
                elif rss_peak > memory_limit_bytes:
                    stop_reason = "memory_limit"
                elif progress.get("phase") in ("detect", "track") and (
                    now - progress["phase_started_monotonic"] > phase_limit_s
                ):
                    stop_reason = "phase_time_limit"
                if stop_reason is not None:
                    worker.kill()
                    break
                time.sleep(0.1)
            worker.wait()
        except BaseException:
            worker.kill()
            worker.wait()
            stop_reason = "manual_stop"
            raise
        finally:
            snapshot = work / "progress.json"
            if snapshot.exists():
                progress = json.loads(snapshot.read_text())
            summary_path = work / "summary.json"
            report = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            rss_peak = max(rss_peak, progress.get("rss_peak_bytes", 0))
            if stop_reason is None and rss_peak > memory_limit_bytes:
                stop_reason = "memory_limit"
            if stop_reason is not None or worker.returncode != 0:
                if (
                    report.get("status") not in ("resource_limit", "failed")
                    or stop_reason is not None
                ):
                    report = {
                        "status": "manual_stop"
                        if stop_reason == "manual_stop"
                        else "resource_limit"
                        if stop_reason
                        else "failed",
                        "stop_reason": stop_reason or "worker_exit",
                        "aggregate": None,
                        "attempt": progress,
                        "stop_stage": progress.get("phase", "startup"),
                    }
            elif report.get("status") not in ("complete", "probe_complete", "resource_limit"):
                report = {
                    "status": "failed",
                    "stop_reason": "missing_final_summary",
                    "aggregate": None,
                    "attempt": progress,
                }
            report["supervision"] = {
                "command": command,
                "exit_code": worker.returncode,
                "wall_s": time.monotonic() - started,
                "rss_peak_bytes": rss_peak,
                "memory_sampling_s": 0.1,
                "memory_scope": "worker process RSS; includes decoder and native threads",
                "limits": {
                    "wall_s": wall_limit_s,
                    "memory_bytes": memory_limit_bytes,
                    "phase_s": phase_limit_s,
                },
            }
            write_json(output / "summary.json", report)
    return report


def run(
    recording: Path, output: Path, *, probe: bool, arms: list[str], root: Path
) -> dict[str, Any]:
    policies = arm_policies(root)
    frozen = freeze_recording(recording, root)
    history = history_index(root / "results")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "freeze.json", frozen)
    write_json(
        output / "history.json",
        {
            "archived_reports": history,
            "reported_history": "Maintainer reports excessive unfiltered MOG2/ViBe candidates and erosion damage to small targets; not a newly measured result.",
            "vibe": {
                "status": "unsupported_unrecovered",
                "description": "historical attempt, not reproduced in this evaluation",
                "source_version": "not archived",
                "parameters": "not archived",
                "failure_stage": "not archived",
                "termination_reason": "not archived",
            },
        },
    )
    manifest: dict[str, Any] = {
        "format": "occupancy-benchmark-v1",
        "stage": "probe" if probe else "full",
        "freeze_sha256": sha256(output / "freeze.json"),
        "scoring": {"warmup_s": 15, "gate_px": 10, "size_k": 0.5},
        "budgets": {
            "wall_s": 300 if probe else 1800,
            "memory_bytes": 4 * 1024**3,
            "phase_s": 2,
            "candidate_limit": 100,
            "overload_hold_s": 10,
            "opencv_threads": 10,
        },
        "planned_frames_per_station": 360 if probe else 900,
        "policy_note": "Opening/closing off. Blob dilation is logged per arm; sizes 3 and 5 need not be held equal (maintainer decision). Tuned MOG2 remains separately identified. Named erode3 arms add one 3x3 elliptical erosion before occupancy and blob extraction; all other arms leave independent erosion off.",
        "quality_scope": "device detection/tracking only; no new center or integrated quality claim",
        "attempts": [{"arm": name, "status": "pending"} for name in arms],
    }
    write_json(output / "manifest.json", manifest)
    try:
        for entry in manifest["attempts"]:
            name = entry["arm"]
            policy = output / f"{name}.json"
            write_json(policy, policies[name])
            entry.update({"status": "running", "policy_sha256": sha256(policy)})
            write_json(output / "manifest.json", manifest)
            work = output / name / "evaluation"
            command = [
                sys.executable,
                "-m",
                "chongming.evaluate_recording",
                str(recording.resolve()),
                "--policy",
                str(policy.resolve()),
                "--output",
                str(work.resolve()),
                "--size-k",
                "0.5",
                "--gate-px",
                "10",
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
            if probe:
                command += ["--max-frames-per-station", "360"]
            report = supervise(
                command,
                output / name,
                wall_limit_s=300 if probe else 1800,
                memory_limit_bytes=4 * 1024**3,
                phase_limit_s=2,
            )
            entry.update(
                {
                    "status": report["status"],
                    "summary": str((output / name / "summary.json").resolve()),
                    "summary_sha256": sha256(output / name / "summary.json"),
                    "quality": report.get("aggregate"),
                    "stop_reason": report.get("stop_reason"),
                }
            )
            print(name, entry["status"], entry["stop_reason"], flush=True)
            write_json(output / "manifest.json", manifest)
    finally:
        for entry in manifest["attempts"]:
            if entry["status"] == "running":
                summary_path = output / entry["arm"] / "summary.json"
                if summary_path.exists():
                    report = json.loads(summary_path.read_text())
                    entry.update(
                        {
                            "status": report["status"],
                            "summary": str(summary_path.resolve()),
                            "summary_sha256": sha256(summary_path),
                            "quality": report.get("aggregate"),
                            "stop_reason": report.get("stop_reason"),
                        }
                    )
        write_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--arms", choices=(*ARMS, *SUPPLEMENTAL_ARMS, *EROSION_ARMS), nargs="+", default=list(ARMS)
    )
    args = parser.parse_args()
    run(args.recording, args.output, probe=args.probe, arms=args.arms, root=Path.cwd())


if __name__ == "__main__":
    main()
