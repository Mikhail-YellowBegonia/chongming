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
from chongming.evaluation_attempt import (
    EvaluationAttempt,
    EvaluationBudget,
    EvaluationLimit,
    write_json,
)
from chongming.modules.detection import validate_detection_policy, validate_tracking_policy
from chongming.modules.workbench_device import WorkbenchMotionModule, image_modules
from chongming.performance import distribution
from chongming.ports.device import MotionDetector, Tracker2D
from chongming.scenarios.json_content import parse_json
from chongming.scenarios.run import _provenance
from chongming.scenarios.serialization import _decode
from chongming.vision.vibe import VibeStage, seed_native


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
    budget: EvaluationBudget | None = None,
    max_frames_per_station: int | None = None,
) -> dict[str, Any]:
    if not 0 <= warmup_s < float("inf"):
        raise ValueError("warmup_s must be finite and nonnegative")
    DetectionMetrics(gate_px, "px")
    target_pixel_gates([], gate_px, size_k)
    if max_frames_per_station is not None and (
        type(max_frames_per_station) is not int or max_frames_per_station < 1
    ):
        raise ValueError("max_frames_per_station must be a positive integer")
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
    seeds = {
        p.detection.vibe_seed
        for p in policies.values()
        if isinstance(p.detection, d.FrameDifferencePolicy) and p.detection.mode == "vibe"
    }
    if len(seeds) > 1:
        raise ValueError("ViBe station policies must share one process-global RNG seed")
    if seeds:
        seed_native(next(iter(seeds)))
    for station, policy in policies.items():
        validate_detection_policy(policy.detection)
        validate_tracking_policy(policy.tracking)
        image_modules(policy.detection, policy.tracking, m.StationId(station), m.SessionId("check"))
    output.mkdir(parents=True, exist_ok=False)
    attempt = EvaluationAttempt(output, budget, list(policies)) if budget is not None else None
    summary: dict[str, Any] = {"format": "device-evaluation-v1", "status": "running"}
    write_json(output / "summary.json", summary)
    try:
        with (
            (recording / "online.jsonl").open() as online,
            (recording / "truth.jsonl").open() as truth,
            (output / "frames.jsonl").open("x", buffering=1) as evidence,
            (output / "candidates.jsonl").open("x", buffering=1) as candidate_evidence,
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
                    if (
                        max_frames_per_station is not None
                        and processed[station] >= max_frames_per_station
                    ):
                        continue
                    if attempt is not None:
                        attempt.state["current_frame"] = {
                            "station_id": station,
                            "frame_id": metadata["frame_id"],
                            "frame_index": metadata["frame_index"],
                        }
                        attempt.phase("decode")
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
                    if attempt is not None:
                        attempt.state["current_frame"]["time_ns"] = ns
                        attempt.phase("detect")
                    cpu, began = time.process_time(), time.perf_counter()
                    candidates = detectors[station].detect(frame)
                    detected = time.perf_counter()
                    detect_cpu = time.process_time() - cpu
                    if candidates.processing_status != m.ProcessingStatus.PROCESSED:
                        raise RuntimeError("recorded frame failed device processing")
                    detector = detectors[station]
                    stages = (
                        detector.stage_timings_ms
                        if isinstance(detector, WorkbenchMotionModule)
                        else {}
                    )
                    counts = (
                        detector.frame_counts
                        if isinstance(detector, WorkbenchMotionModule)
                        else {"accepted_candidates": len(candidates.candidates)}
                    )
                    candidate_evidence.write(
                        json.dumps(
                            {
                                "station_id": station,
                                "frame_id": metadata["frame_id"],
                                "frame_index": metadata["frame_index"],
                                "time_ns": ns,
                                "counts": counts,
                                "stage_timings_ms": stages,
                                "detect_ms": (detected - began) * 1000,
                            },
                            allow_nan=False,
                        )
                        + "\n"
                    )
                    suppress_overload = False
                    if attempt is not None:
                        attempt.state["detected_frames"][station] += 1
                        attempt.state["last_detected_frame"] = attempt.state["current_frame"].copy()
                        attempt.state["candidate_peak"] = max(
                            attempt.state["candidate_peak"], len(candidates.candidates)
                        )
                        attempt.state["last_counts"] = counts
                        attempt.checkpoint()
                        attempt.check_phase((detected - began) * 1000)
                        suppress_overload = attempt.budget.skip_tracking(
                            len(candidates.candidates), ns - origins[station]
                        )
                        if suppress_overload:
                            attempt.state["overload_suppressed_frames"][station] += 1
                        attempt.phase("tracking_skipped" if suppress_overload else "track")
                    track_began = time.perf_counter()
                    track_cpu = time.process_time()
                    observations = (
                        ()
                        if suppress_overload
                        else trackers[station].update(candidates).observations
                    )
                    ended = time.perf_counter()
                    track_cpu = time.process_time() - track_cpu
                    timing = {
                        "detect_ms": (detected - began) * 1000,
                        "track_ms": 0.0 if suppress_overload else (ended - track_began) * 1000,
                        "pipeline_ms": (
                            detected - began + (0 if suppress_overload else ended - track_began)
                        )
                        * 1000,
                        "cpu_ms": (detect_cpu + (0 if suppress_overload else track_cpu)) * 1000,
                        "decode_ms": decode_ms,
                        "convert_ms": convert_ms,
                    }
                    if attempt is not None:
                        attempt.check_phase(timing["track_ms"])
                        attempt.phase("score")
                    if observation_sink is not None:
                        observation_sink(frame, observations)
                    for key, value in timing.items():
                        samples[station][key].append(value)
                    for key, value in stages.items():
                        stage_samples[station].setdefault(key, []).append(value)
                    processed[station] += 1
                    excluded = ns - origins[station] < round(warmup_s * 1e9)
                    record: dict[str, Any] = {
                        "station_id": station,
                        "frame_id": metadata["frame_id"],
                        "time_ns": ns,
                        "warmup_excluded": excluded,
                        "timing_ms": timing,
                        "stage_timings_ms": stages,
                        "counts": counts,
                        "tracking_suppressed": suppress_overload,
                        "suppression_reason": "warmup_overload" if suppress_overload else None,
                        "observation_count": len(observations),
                        "predictions_px": [list(o.centroid_px) for o in observations],
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
                        predictions = [tuple(o.centroid_px) for o in observations]
                        gates = target_pixel_gates(frame_truth["targets"], gate_px, size_k)
                        record["eligible_targets_px"] = targets
                        record["target_gates_px"] = gates
                        record.update(
                            metrics[station].add(predictions, targets, target_gates=gates)
                        )
                        aggregate.add(predictions, targets, target_gates=gates)
                    evidence.write(json.dumps(record, allow_nan=False) + "\n")
                    if attempt is not None:
                        attempt.state["processed_frames"][station] = processed[station]
                        attempt.state["scored_frames"][station] = (
                            processed[station] - skipped[station]
                        )
                        attempt.state["last_valid_frame"] = attempt.state["current_frame"].copy()
                        attempt.phase("metadata")
            if any(count == 0 for count in processed.values()):
                raise ValueError("selected station has no recorded main frames")
            for path, reader in readers.items():
                expected = videos[path]["frame_count"]
                if max_frames_per_station is not None:
                    expected = min(expected, max_frames_per_station)
                if next_index[path] != expected or (
                    max_frames_per_station is None and reader.read()[0]
                ):
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
        if max_frames_per_station is not None:
            summary["status"] = "probe_complete"
            summary["partial_quality_diagnostic"] = summary.pop("aggregate")
            summary["aggregate"] = None
            for station_summary in summary["stations"].values():
                station_summary["partial_metrics_diagnostic"] = station_summary.pop("metrics")
                station_summary["metrics"] = None
    except EvaluationLimit as error:
        summary = {
            "format": "device-evaluation-v1",
            "status": "resource_limit",
            "stop_reason": str(error),
            "aggregate": None,
            "partial_quality_diagnostic": aggregate.summary(),
        }
    except Exception as error:
        summary = {
            "format": "device-evaluation-v1",
            "status": "failed",
            "error": str(error),
            "aggregate": None,
            "partial_quality_diagnostic": aggregate.summary(),
        }
        raise
    finally:
        for reader in readers.values():
            reader.release()
        for detector in detectors.values():
            if isinstance(detector, WorkbenchMotionModule):
                for stage in detector.stages:
                    if isinstance(stage, VibeStage):
                        stage.reset()
        run_context: dict[str, Any] = {
            "recording": str(recording.resolve()),
            "policy_sha256": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
            "policies": parse_json(policy_file.read_bytes()),
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "opencv": cv2.__version__,
                "opencv_threads": cv2.getNumThreads(),
            },
            "warmup_s": warmup_s,
            "gate_px": gate_px,
            "size_k": size_k,
        }
        summary.update(run_context)
        if attempt is not None:
            attempt.checkpoint()
            summary["attempt"] = attempt.state
            summary["stop_stage"] = attempt.state["phase"]
        summary["max_frames_per_station"] = max_frames_per_station
        write_json(output / "summary.json", summary)
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
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--overload-hold-s", type=float, default=10.0)
    parser.add_argument("--phase-limit-s", type=float, default=2.0)
    parser.add_argument("--max-frames-per-station", type=int)
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
        budget=None
        if args.candidate_limit is None
        else EvaluationBudget(args.candidate_limit, args.overload_hold_s, args.phase_limit_s),
        max_frames_per_station=args.max_frames_per_station,
    )
    print(json.dumps(result["aggregate"], indent=2))


if __name__ == "__main__":
    main()
