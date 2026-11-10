"""Fixed-policy device repeats and common-region diagnostics on Hannover recordings."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from chongming.benchmark_occupancy import arm_policies, freeze_recording, sha256, supervise
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.evaluate_recording import ImagePolicy
from chongming.evaluation import annotate
from chongming.evaluation_attempt import write_json
from chongming.modules.workbench_device import WorkbenchMotionModule
from chongming.scenarios.serialization import _decode
from chongming.vision.occupancy import OccupancyFilterStage


def policies(root: Path) -> dict[str, dict[str, Any]]:
    original = arm_policies(root)
    selected = {
        name: original[name] for name in ("difference-on", "mog2-tuned", "mog2-off-erode3-dilate3")
    }
    both = json.loads(json.dumps(original["mog2-tuned"]))
    for station in both.values():
        station["detection"]["apply_erosion"] = True
        station["detection"]["morphology_kernel_size"] = 3
    selected["mog2-on-erode3-dilate3"] = both
    return selected


def reference_regions(
    recording: Path, policy: dict[str, Any], output: Path
) -> dict[str, np.ndarray]:
    """Freeze one common mask per station at frame 450 using only input images."""
    cv2.setNumThreads(10)
    detectors = {}
    readers = {}
    masks = {}
    try:
        with (recording / "online.jsonl").open() as handle:
            for line in handle:
                for metadata in json.loads(line)["frames"]:
                    if metadata["camera_role"] != "main" or metadata["frame_index"] > 450:
                        continue
                    station = metadata["station_id"]
                    if station not in detectors:
                        config = _decode(policy[station], ImagePolicy)
                        assert isinstance(config.detection, d.FrameDifferencePolicy)
                        detectors[station] = WorkbenchMotionModule(policy=config.detection)
                        readers[station] = cv2.VideoCapture(str(recording / metadata["path"]))
                    ok, bgr = readers[station].read()
                    if not ok:
                        raise ValueError("reference-region video ended early")
                    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    frame = m.CameraFrame(
                        station_id=m.StationId(station),
                        station_session_id=m.SessionId(metadata["session_id"]),
                        camera_role=m.CameraRole.MAIN,
                        frame_id=m.FrameId(metadata["frame_id"]),
                        camera_config_ref=m.ConfigDigest(metadata["camera_config_ref"]),
                        capture_time=_decode(metadata["capture_time"], m.CaptureTime),
                        image=m.ImagePlane(
                            width_px=rgb.shape[1],
                            height_px=rgb.shape[0],
                            pixel_format=m.PixelFormat.RGB8,
                            row_stride_bytes=rgb.shape[1] * 3,
                            pixels=rgb.tobytes(),
                        ),
                    )
                    detectors[station].detect(frame)
                    if metadata["frame_index"] == 450:
                        stage = detectors[station].stages[3]
                        assert (
                            isinstance(stage, OccupancyFilterStage) and stage.score_map is not None
                        )
                        mask = (stage.score_map > stage.config.mask_threshold).astype(np.uint8)
                        if stage.dilation_kernel is not None:
                            mask = np.asarray(
                                cv2.dilate(mask, stage.dilation_kernel), dtype=np.uint8
                            )
                        masks[station] = mask
        if len(masks) != 3:
            raise ValueError("three reference masks required")
        for station, mask in masks.items():
            np.save(output / f"{station}-region.npy", mask)
        write_json(
            output / "regions.json",
            {
                "reference": "selected frame-difference occupancy policy, frozen at main frame 450 (last excluded warmup frame); common to every arm",
                "coordinate_rule": "floor(original-image target centroid / 2); fixed region membership; global matching before stratification",
                "area": {
                    station: {
                        "pixels": int(np.count_nonzero(mask)),
                        "fraction": float(mask.mean()),
                        "sha256": sha256(output / f"{station}-region.npy"),
                    }
                    for station, mask in masks.items()
                },
                "policy": policy,
            },
        )
        return masks
    finally:
        for reader in readers.values():
            reader.release()


def region_scores(frames: Path, masks: dict[str, np.ndarray]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {"inside": Counter(), "outside": Counter()}
    with frames.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["warmup_excluded"]:
                continue
            annotated = annotate(row, 10)
            mask = masks[row["station_id"]]
            for target in annotated["targets"]:
                x, y = (math.floor(v / 2) for v in target["xy"])
                if not 0 <= x < mask.shape[1] or not 0 <= y < mask.shape[0]:
                    raise ValueError("responsible target centroid outside mask coordinates")
                region = "inside" if mask[y, x] else "outside"
                counts[region]["responsible_samples"] += 1
                counts[region]["hits"] += target["matched"]
                counts[region]["misses"] += not target["matched"]
    return {
        key: {
            **value,
            "recall": value["hits"] / value["responsible_samples"]
            if value["responsible_samples"]
            else None,
        }
        for key, value in counts.items()
    }


def classify_trace(trace: list[tuple[int, tuple[float, float]]]) -> dict[str, str]:
    """Image motion labels from a contiguous visible truth trajectory, never detections."""
    current_ns, current = trace[-1]
    slow = "uncovered"
    if len(trace) >= 31:
        previous_ns, previous = trace[-31]
        speed = math.dist(current, previous) / ((current_ns - previous_ns) / 1e9)
        slow = "slow" if speed <= 5.0 else "other_motion"
    older = [i for i, (ns, _) in enumerate(trace) if current_ns - ns >= 2_000_000_000]
    recurrence = "return_uncovered"
    if older:
        distances = np.linalg.norm(np.asarray([point for _, point in trace]) - current, axis=1)
        near = [i for i in older if distances[i] <= 10.0]
        recurrence = (
            "return_to_region"
            if near and np.any(distances[min(near) + 1 :] > 20.0)
            else "no_return"
        )
    return {"motion": slow, "recurrence": recurrence}


def motion_conditions(recording: Path, output: Path) -> dict[str, dict[str, dict[str, str]]]:
    histories: dict[tuple[str, str], list[tuple[int, tuple[float, float]]]] = {}
    labels: dict[str, dict[str, dict[str, str]]] = {}
    with (recording / "truth.jsonl").open() as handle:
        for line in handle:
            for frame in json.loads(line)["camera_frames"]:
                if frame["camera_role"] != "main":
                    continue
                capture = frame["capture_time"]
                ns = (capture["exposure_start_ns"] + capture["exposure_end_ns"]) // 2
                current = {}
                for target in frame["targets"]:
                    key = (frame["station_id"], target["truth_object_id"])
                    trace = histories.setdefault(key, [])
                    projection = target["projection"]
                    if not projection["center_in_field_of_view"]:
                        trace.clear()
                        continue
                    xy = projection["centroid_px"]
                    if trace and ns - trace[-1][0] > 50_000_000:
                        trace.clear()
                    trace.append((ns, (float(xy[0]), float(xy[1]))))
                    current[target["truth_object_id"]] = classify_trace(trace)
                labels[frame["frame_id"]] = current
    write_json(
        output / "motion-definition.json",
        {
            "source": "offline recorded truth projections; identical labels across arms",
            "slow": "apparent center displacement over 30 frames / elapsed time <=5 original-image px/s; needs contiguous FOV history",
            "return_to_region": "within10px of an earlier center at least2s earlier, with an intervening displacement >20px from current center; contiguous FOV history",
            "uncovered": "insufficient contiguous visible history; not relabeled as slow or repeated",
            "labels_sha256": None,
        },
    )
    write_json(output / "motion-labels.json", labels)
    definition = json.loads((output / "motion-definition.json").read_text())
    definition["labels_sha256"] = sha256(output / "motion-labels.json")
    write_json(output / "motion-definition.json", definition)
    return labels


def motion_scores(frames: Path, labels: dict[str, dict[str, dict[str, str]]]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {}
    with frames.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["warmup_excluded"]:
                continue
            for target in annotate(row, 10)["targets"]:
                for label in labels[row["frame_id"]][target["id"]].values():
                    stats = counts.setdefault(label, Counter())
                    stats["responsible_samples"] += 1
                    stats["hits"] += target["matched"]
    return {
        key: {**value, "recall": value["hits"] / value["responsible_samples"]}
        for key, value in counts.items()
    }


def run(recording: Path, output: Path, repeats: int) -> None:
    selected = policies(Path.cwd())
    frozen = freeze_recording(recording, Path.cwd())
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "freeze.json", frozen)
    masks = reference_regions(recording, selected["difference-on"], output)
    motion = motion_conditions(recording, output)
    manifest: dict[str, Any] = {
        "freeze_sha256": sha256(output / "freeze.json"),
        "repeats": repeats,
        "budgets": {
            "wall_s": 1800,
            "memory_bytes": 4 * 1024**3,
            "phase_s": 2,
            "candidate_limit": 100,
            "overload_hold_s": 10,
            "opencv_threads": 10,
        },
        "policy_order": "rotated across repeats; sequential execution; all frames train; 451 warmup and 449 scored frames per station",
        "attempts": [],
    }
    write_json(output / "manifest.json", manifest)
    names = list(selected)
    for repeat in range(repeats):
        for name in names[repeat % 4 :] + names[: repeat % 4]:
            policy = output / f"{name}.json"
            if not policy.exists():
                write_json(policy, selected[name])
            path = output / f"{name}-{repeat + 1}"
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
            report = supervise(
                command, path, wall_limit_s=1800, memory_limit_bytes=4 * 1024**3, phase_limit_s=2
            )
            entry = {
                "arm": name,
                "repeat": repeat + 1,
                "status": report["status"],
                "summary": str((path / "summary.json").resolve()),
                "summary_sha256": sha256(path / "summary.json"),
                "policy_sha256": sha256(policy),
                "regions": region_scores(path / "evaluation/frames.jsonl", masks)
                if report["status"] == "complete"
                else None,
                "motion": motion_scores(path / "evaluation/frames.jsonl", motion)
                if report["status"] == "complete"
                else None,
            }
            manifest["attempts"].append(entry)
            write_json(output / "manifest.json", manifest)
            print(name, repeat + 1, report["status"], flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    run(args.recording, args.output, args.repeats)


if __name__ == "__main__":
    main()
