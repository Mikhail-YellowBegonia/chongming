"""Evaluate device pipelines on existing recorded main-camera videos, without Godot."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any

import cv2

from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.evaluation import DetectionMetrics, target_pixel_gates
from chongming.modules.detection import validate_detection_policy, validate_tracking_policy
from chongming.modules.workbench_device import WorkbenchMotionModule, image_modules
from chongming.performance import distribution
from chongming.ports.device import MotionDetector, Tracker2D
from chongming.scenarios.json_content import parse_json
from chongming.scenarios.run import _provenance
from chongming.scenarios.serialization import _decode


@dataclass(frozen=True)
class ImagePolicy:
    detection: d.DetectionPolicy | d.FrameDifferencePolicy
    tracking: d.Tracking2DPolicy | d.KalmanTrackingPolicy


def evaluate(
    recording: Path,
    policy_file: Path,
    output: Path,
    *,
    gate_px: float,
    warmup_s: float = 15.0,
    size_k: float | None = None,
    observation_sink: Callable[[m.CameraFrame, tuple[m.Observation2D, ...]], None] | None = None,
) -> dict[str, Any]:
    if not 0 <= warmup_s < float("inf"):
        raise ValueError("warmup_s must be finite and nonnegative")
    DetectionMetrics(gate_px, "px")
    target_pixel_gates([], gate_px, size_k)
    policies: dict[str, ImagePolicy] = _decode(
        parse_json(policy_file.read_bytes()), dict[str, ImagePolicy]
    )
    if not policies:
        raise ValueError("at least one station policy is required")
    manifest = json.loads((recording / "manifest.json").read_text())
    video = json.loads((recording / "video.json").read_text())
    videos = {entry["path"]: entry for entry in video["streams"].values()}
    metrics = {station: DetectionMetrics(gate_px, "px") for station in policies}
    aggregate = DetectionMetrics(gate_px, "px")
    detectors: dict[str, MotionDetector] = {}
    trackers: dict[str, Tracker2D] = {}
    readers: dict[str, cv2.VideoCapture] = {}
    next_index: dict[str, int] = {}
    origins: dict[str, int] = {}
    samples: dict[str, dict[str, list[float]]] = {
        station: {
            key: []
            for key in ("detect_ms", "track_ms", "cpu_ms", "decode_ms", "convert_ms", "pipeline_ms")
        }
        for station in policies
    }
    stage_samples: dict[str, dict[str, list[float]]] = {station: {} for station in policies}
    scored_samples: dict[str, dict[str, list[float]]] = {station: {} for station in policies}
    processed = {station: 0 for station in policies}
    skipped = {station: 0 for station in policies}
    sessions: dict[str, str] = {}
    # Validate policy families before creating output or opening video decoders.
    for station, policy in policies.items():
        validate_detection_policy(policy.detection)
        validate_tracking_policy(policy.tracking)
        image_modules(policy.detection, policy.tracking, m.StationId(station), m.SessionId("check"))
    output.mkdir(parents=True, exist_ok=False)
    summary: dict[str, Any] = {"format": "device-evaluation-v1", "status": "running"}
    try:
        with (
            (recording / "online.jsonl").open() as online,
            (recording / "truth.jsonl").open() as truth,
            (output / "frames.jsonl").open("x") as evidence,
        ):
            for online_line, truth_line in zip_longest(online, truth):
                if online_line is None or truth_line is None:
                    raise ValueError("online/truth step counts differ")
                row, privileged = json.loads(online_line), json.loads(truth_line)
                if row["sample_time"] != privileged["sample_time"]:
                    raise ValueError("online/truth sample times differ")
                truths = {item["frame_id"]: item for item in privileged["camera_frames"]}
                for metadata in row["frames"]:
                    station = metadata["station_id"]
                    if metadata["camera_role"] != "main" or station not in policies:
                        continue
                    frame_truth = truths[metadata["frame_id"]]
                    if (
                        frame_truth["station_id"] != station
                        or frame_truth["station_session_id"] != metadata["session_id"]
                        or frame_truth["camera_role"] != "main"
                        or frame_truth["capture_time"] != metadata["capture_time"]
                    ):
                        raise ValueError("frame identity or capture time differs from truth")
                    path = metadata["path"]
                    if path not in readers:
                        source = (recording / path).resolve()
                        if not source.is_relative_to(recording.resolve()):
                            raise ValueError("video path escapes recording")
                        with source.open("rb") as handle:
                            if (
                                hashlib.file_digest(handle, "sha256").hexdigest()
                                != videos[path]["sha256"]
                            ):
                                raise ValueError("video digest mismatch")
                        readers[path] = cv2.VideoCapture(str(source))
                        next_index[path] = 0
                    if metadata["frame_index"] != next_index[path]:
                        raise ValueError("video indices must be contiguous from zero")
                    decode_began = time.perf_counter()
                    ok, bgr = readers[path].read()
                    decode_ms = (time.perf_counter() - decode_began) * 1000
                    if not ok:
                        raise ValueError("recorded video ended before metadata")
                    next_index[path] += 1
                    capture = _decode(metadata["capture_time"], m.CaptureTime)
                    ns = (capture.exposure_start_ns + capture.exposure_end_ns) // 2
                    origins.setdefault(station, ns)
                    if station not in detectors:
                        sessions[station] = metadata["session_id"]
                        detectors[station], trackers[station] = image_modules(
                            policies[station].detection,
                            policies[station].tracking,
                            m.StationId(station),
                            m.SessionId(sessions[station]),
                        )
                    if sessions[station] != metadata["session_id"]:
                        raise ValueError("station session changed within recording")
                    convert_began = time.perf_counter()
                    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    if rgb.shape != (metadata["height_px"], metadata["width_px"], 3):
                        raise ValueError("video dimensions differ from metadata")
                    frame = m.CameraFrame(
                        station_id=m.StationId(station),
                        station_session_id=m.SessionId(sessions[station]),
                        camera_role=m.CameraRole.MAIN,
                        frame_id=m.FrameId(metadata["frame_id"]),
                        camera_config_ref=m.ConfigDigest(metadata["camera_config_ref"]),
                        capture_time=capture,
                        image=m.ImagePlane(
                            width_px=rgb.shape[1],
                            height_px=rgb.shape[0],
                            pixel_format=m.PixelFormat.RGB8,
                            row_stride_bytes=rgb.shape[1] * 3,
                            pixels=rgb.tobytes(),
                        ),
                    )
                    convert_ms = (time.perf_counter() - convert_began) * 1000
                    cpu, began = time.process_time(), time.perf_counter()
                    candidates = detectors[station].detect(frame)
                    detected = time.perf_counter()
                    result = trackers[station].update(candidates)
                    ended = time.perf_counter()
                    timing = {
                        "detect_ms": (detected - began) * 1000,
                        "track_ms": (ended - detected) * 1000,
                        "pipeline_ms": (ended - began) * 1000,
                        "cpu_ms": (time.process_time() - cpu) * 1000,
                        "decode_ms": decode_ms,
                        "convert_ms": convert_ms,
                    }
                    if observation_sink is not None:
                        observation_sink(frame, result.observations)
                    for key, value in timing.items():
                        samples[station][key].append(value)
                    detector = detectors[station]
                    stages = (
                        detector.stage_timings_ms
                        if isinstance(detector, WorkbenchMotionModule)
                        else {}
                    )
                    for key, value in stages.items():
                        stage_samples[station].setdefault(key, []).append(value)
                    if candidates.processing_status != m.ProcessingStatus.PROCESSED:
                        raise RuntimeError("recorded frame failed device processing")
                    processed[station] += 1
                    excluded = ns - origins[station] < round(warmup_s * 1e9)
                    record: dict[str, Any] = {
                        "station_id": station,
                        "frame_id": metadata["frame_id"],
                        "time_ns": ns,
                        "warmup_excluded": excluded,
                        "timing_ms": timing,
                        "stage_timings_ms": stages,
                        "predictions_px": [list(o.centroid_px) for o in result.observations],
                    }
                    if excluded:
                        skipped[station] += 1
                    else:
                        for key, value in timing.items():
                            scored_samples[station].setdefault(key, []).append(value)
                        targets = {
                            item["truth_object_id"]: tuple(item["projection"]["centroid_px"])
                            for item in frame_truth["targets"]
                            if item["projection"]["center_in_field_of_view"]
                        }
                        predictions = [tuple(o.centroid_px) for o in result.observations]
                        gates = target_pixel_gates(frame_truth["targets"], gate_px, size_k)
                        record["eligible_targets_px"] = targets
                        record["target_gates_px"] = gates
                        record.update(
                            metrics[station].add(predictions, targets, target_gates=gates)
                        )
                        aggregate.add(predictions, targets, target_gates=gates)
                    evidence.write(json.dumps(record, allow_nan=False) + "\n")
            if any(count == 0 for count in processed.values()):
                raise ValueError("selected station has no recorded main frames")
            for path, reader in readers.items():
                if next_index[path] != videos[path]["frame_count"] or reader.read()[0]:
                    raise ValueError("video frame count differs from metadata")
        summary = {
            "format": "device-evaluation-v1",
            "status": "complete",
            "recording": str(recording.resolve()),
            "recording_code_revision": manifest["code_revision"],
            "evaluation_provenance": _provenance(),
            "policy_sha256": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
            "policies": parse_json(policy_file.read_bytes()),
            "timing_scope": "pipeline=detect+track; decode and frame conversion separate; excludes scoring, rendering and disk report writes; stage times are subsets of detect",
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "opencv": cv2.__version__,
                "opencv_threads": cv2.getNumThreads(),
            },
            "warmup_s": warmup_s,
            "size_k": size_k,
            "gate_rule": "max(gate_px, size_k * hypot(projected_width_px, projected_height_px))"
            if size_k is not None
            else "fixed gate_px",
            "responsibility": "main-camera target center in FOV; occlusion ignored",
            "matching": "maximum cardinality then minimum total pixel distance within gate",
            "aggregate": aggregate.summary(),
            "stations": {
                station: {
                    "metrics": metrics[station].summary(),
                    "processed_frames": processed[station],
                    "warmup_frames": skipped[station],
                    "algorithm_throughput_fps_including_warmup": 1000
                    * len(samples[station]["pipeline_ms"])
                    / sum(samples[station]["pipeline_ms"]),
                    "stage_timing_ms_including_warmup": {
                        key: distribution(values) for key, values in stage_samples[station].items()
                    },
                    "timing_ms_scored_frames": {
                        key: distribution(values) for key, values in scored_samples[station].items()
                    },
                    "timing_ms_including_warmup": {
                        key: distribution(values) for key, values in samples[station].items()
                    },
                }
                for station in policies
            },
        }
    except Exception as error:
        summary = {"format": "device-evaluation-v1", "status": "failed", "error": str(error)}
        raise
    finally:
        for reader in readers.values():
            reader.release()
        (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--gate-px", required=True, type=float)
    parser.add_argument(
        "--opencv-threads", type=int, help="Explicit OpenCV worker count for comparisons"
    )
    parser.add_argument("--size-k", type=float, help="Projected-envelope diagonal multiplier")
    parser.add_argument("--warmup-s", default=15.0, type=float)
    args = parser.parse_args()
    if args.opencv_threads is not None:
        if args.opencv_threads < 1:
            parser.error("--opencv-threads must be positive")
        cv2.setNumThreads(args.opencv_threads)
    result = evaluate(
        args.recording,
        args.policy,
        args.output,
        gate_px=args.gate_px,
        warmup_s=args.warmup_s,
        size_k=args.size_k,
    )
    print(json.dumps(result["aggregate"], indent=2))


if __name__ == "__main__":
    main()
