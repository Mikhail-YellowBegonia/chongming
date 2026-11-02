"""Recorded image detections -> measured rays -> post-intersection center; no Godot."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2

from chongming.composition.development import development_run
from chongming.contracts import development as d
from chongming.contracts import device as dev
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.evaluate_recording import evaluate as evaluate_device
from chongming.modules.device import EventSequencerModule
from chongming.modules.rays import RayGenerationModule
from chongming.performance import distribution
from chongming.reference import create_postintersection_algorithm
from chongming.scenarios.run import _json, _provenance
from chongming.scenarios.serialization import _decode, load_compiled_scenario


def record_observations(recording: Path, policy: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with (output / "observations.jsonl").open("x") as observations:

        def save(frame: m.CameraFrame, values: tuple[m.Observation2D, ...]) -> None:
            observations.write(_json({"frame_id": frame.frame_id, "observations": values}) + "\n")

        return evaluate_device(
            recording,
            policy,
            output / "evaluation",
            gate_px=10,
            size_k=0.5,
            warmup_s=15,
            observation_sink=save,
        )


def replay(
    recording: Path,
    observations: Path,
    output: Path,
    *,
    asset_root: Path,
    pixel_sigma_px: float,
    warmup_s: float = 15.0,
) -> dict[str, Any]:
    if not math.isfinite(pixel_sigma_px) or pixel_sigma_px <= 0:
        raise ValueError("pixel_sigma_px must be finite and positive")
    if not math.isfinite(warmup_s) or warmup_s < 0:
        raise ValueError("warmup_s must be finite and nonnegative")
    spec = load_compiled_scenario(recording / "scenario.json", asset_root=asset_root)
    if spec.perturbations.camera_pose or spec.perturbations.camera_jitter:
        raise ValueError(
            "this conditional-calibration experiment requires no pose/jitter perturbations"
        )
    configs = {str(c.station_id): c for c in spec.station_configs}
    manifest = json.loads((recording / "manifest.json").read_text())
    sessions = {
        b["station_id"]: m.SessionId(b["image_session_id"]) for b in manifest["station_sessions"]
    }
    sequencers = {
        station: EventSequencerModule(
            station_id=configs[station].station_id,
            station_session_id=session,
            schema_version="event-v1",
        )
        for station, session in sessions.items()
    }
    ray_policy = dev.RayGenerationPolicy(
        uncertainty_model_id="tangent_gaussian_v1",
        fallback_centroid_covariance_px2=((pixel_sigma_px**2, 0), (0, pixel_sigma_px**2)),
        unmodelled_high_frequency_jitter_covariance_rad2=((0, 0), (0, 0)),
        unmodelled_relative_flex_covariance_rad2=((0, 0), (0, 0)),
    )
    builders = {
        station: RayGenerationModule(policy=ray_policy, sequencer=seq)
        for station, seq in sequencers.items()
    }
    run = development_run(spec, output)
    now = m.ProcessingTime(clock_domain_id=spec.timeline.clock_domain.clock_domain_id, time_ns=0)
    registration = d.CenterRegistration(
        stations=spec.station_configs,
        center_session_id=str(uuid4()),
        policy=run.fusion_policy,
        clock_mapping=run.processing_to_capture_clock,
        started_at=now,
    )
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(recording / "scenario.json", output / "scenario.json")
    (output / "center-registration.json").write_text(_json(registration) + "\n")
    algorithm = create_postintersection_algorithm()
    solutions: dict[m.CalibrationSolutionRef, m.CalibrationSolution] = {}
    states: dict[str, m.CalibrationState] = {}
    last_capture = {}
    warmup_origins: dict[str, int] = {}
    timings: list[float] = []
    stage_samples: dict[str, list[float]] = {}
    scored_timings: list[float] = []
    scored_stage_samples: dict[str, list[float]] = {}
    scored_window_starts: set[int] = set()
    count = 0
    status = "failed"

    def remap_ref(ref: m.CalibrationSolutionRef) -> m.CalibrationSolutionRef:
        return replace(ref, station_session_id=sessions[str(ref.station_id)])

    try:
        algorithm.start(registration)
        with (
            (recording / "online.jsonl").open() as online,
            observations.open() as detected,
            (output / "center-input.jsonl").open("x") as inputs,
            (output / "center-output.jsonl").open("x") as outputs,
            (output / "edge-rays.jsonl").open("x") as edge_rays,
            (output / "timings.jsonl").open("x") as timing_file,
        ):

            def process(step: d.CenterInputStep) -> None:
                inputs.write(_json(step) + "\n")
                began = time.perf_counter()
                result = algorithm.process(step)
                duration = (time.perf_counter() - began) * 1000
                timings.append(duration)
                assert algorithm.center is not None
                stages = algorithm.center.fusion.stage_timings
                scored = any(
                    int(sample["window_start_ns"]) in scored_window_starts for sample in stages
                )
                if scored:
                    scored_timings.append(duration)
                for sample in stages:
                    for name, value in sample.items():
                        if name.endswith("_ms"):
                            stage_samples.setdefault(name, []).append(float(value))
                            if int(sample["window_start_ns"]) in scored_window_starts:
                                scored_stage_samples.setdefault(name, []).append(float(value))
                timing_file.write(
                    _json(
                        {
                            "step_no": step.step_no,
                            "time_ns": step.time.time_ns,
                            "center_process_ms": duration,
                            "warmup_excluded": not scored,
                            "windows": stages,
                        }
                    )
                    + "\n"
                )
                outputs.write(_json(result) + "\n")

            for count, line in enumerate(online, 1):
                row = json.loads(line)
                packets = _decode(row["packets"], s.ScenarioPacketOutput)
                now = m.ProcessingTime(
                    clock_domain_id=packets.sample_time.clock_domain_id,
                    time_ns=packets.sample_time.time_ns,
                )
                new_solutions = []
                events: list[m.StationEvent] = []
                for solution in packets.calibration_solutions:
                    remapped = replace(solution, solution_ref=remap_ref(solution.solution_ref))
                    solutions[remapped.solution_ref] = remapped
                    new_solutions.append(remapped)
                for state in packets.calibration_states:
                    station = str(state.station_id)
                    states[station] = replace(
                        state,
                        station_session_id=sessions[station],
                        solution_ref=None
                        if state.solution_ref is None
                        else remap_ref(state.solution_ref),
                    )
                for event in packets.calibration_events:
                    station = str(event.envelope.station_id)
                    events.append(
                        m.CalibrationStateEvent(
                            envelope=sequencers[station].next_envelope(
                                m.EventType.CALIBRATION_STATE, configs[station].config_digest
                            ),
                            state=states[station],
                            solution=None
                            if event.solution is None
                            else solutions[remap_ref(event.solution.solution_ref)],
                        )
                    )
                sampled = []
                for metadata in row["frames"]:
                    if metadata["camera_role"] != "main":
                        continue
                    frame = json.loads(next(detected))
                    if frame["frame_id"] != metadata["frame_id"]:
                        raise ValueError(
                            "observation order or frame identity differs from recording"
                        )
                    station = metadata["station_id"]
                    if metadata["session_id"] != sessions[station]:
                        raise ValueError("recorded image session differs from registration")
                    capture = _decode(metadata["capture_time"], m.CaptureTime)
                    warmup_origins.setdefault(
                        station, (capture.exposure_start_ns + capture.exposure_end_ns) // 2
                    )
                    last_capture[station] = capture
                    sampled.append(station)
                    state = states[station]
                    snapshot = m.CalibrationSnapshot(
                        main_capture_time=capture,
                        state=state,
                        solution=solutions[state.solution_ref] if state.solution_ref else None,
                        usage={
                            m.CalibrationHealth.VALID: m.CalibrationUsage.VALID,
                            m.CalibrationHealth.FROZEN: m.CalibrationUsage.FROZEN,
                            m.CalibrationHealth.STALE: m.CalibrationUsage.STALE,
                        }.get(state.health),
                        reason_codes=state.reason_codes,
                    )
                    values = _decode(frame["observations"], tuple[m.Observation2D, ...])
                    if (capture.exposure_start_ns + capture.exposure_end_ns) // 2 - warmup_origins[
                        station
                    ] < round(warmup_s * 1e9):
                        continue
                    exposure_ns = (capture.exposure_start_ns + capture.exposure_end_ns) // 2
                    window_policy = registration.policy.windows
                    scored_window_starts.add(
                        window_policy.origin_ns
                        + (exposure_ns - window_policy.origin_ns)
                        // window_policy.width_ns
                        * window_policy.width_ns
                    )
                    for index, observation in enumerate(values):
                        if (
                            observation.main_frame_ref.frame_id != metadata["frame_id"]
                            or observation.capture_time != capture
                        ):
                            raise ValueError("observation scope or capture time mismatch")
                        event = builders[station].build(observation, configs[station], snapshot)
                        events.append(event)
                        edge_rays.write(
                            _json(
                                {
                                    "frame_id": metadata["frame_id"],
                                    "prediction_index": index,
                                    "time_ns": (capture.exposure_start_ns + capture.exposure_end_ns)
                                    // 2,
                                    "station_id": station,
                                    "event": event,
                                }
                            )
                            + "\n"
                        )
                progress = tuple(
                    f.StationEventTimeProgress(
                        station_id=configs[station].station_id,
                        station_session_id=sessions[station],
                        station_config_digest=configs[station].config_digest,
                        completed_capture_time=last_capture[station],
                        received_at=now,
                        stream_ended=False,
                    )
                    for station in sampled
                )
                process(
                    d.CenterInputStep(
                        step_no=count - 1,
                        time=now,
                        solutions=tuple(new_solutions),
                        events=tuple(m.ReceivedEvent(event=e, arrival_time=now) for e in events),
                        progress=progress,
                        stream_ended=False,
                    )
                )
                if count % 100 == 0:
                    print(f"Integrated {count} recorded steps", flush=True)
            if next(detected, None) is not None:
                raise ValueError("extra observation frames after recording end")
            now = m.ProcessingTime(
                clock_domain_id=now.clock_domain_id, time_ns=spec.timeline.end_time_ns + 1
            )
            progress = tuple(
                f.StationEventTimeProgress(
                    station_id=configs[station].station_id,
                    station_session_id=sessions[station],
                    station_config_digest=configs[station].config_digest,
                    completed_capture_time=last_capture[station],
                    received_at=now,
                    stream_ended=True,
                )
                for station in configs
            )
            process(
                d.CenterInputStep(
                    step_no=count,
                    time=now,
                    solutions=(),
                    events=(),
                    progress=progress,
                    stream_ended=True,
                )
            )
        status = "complete"
    finally:
        algorithm.close()
        report = {
            "format": "integrated-recorded-replay-v1",
            "status": status,
            "steps": count,
            "source_recording": str(recording.resolve()),
            "observations": str(observations.resolve()),
            "pixel_sigma_px": pixel_sigma_px,
            "warmup_s": warmup_s,
            "warmup_origin_by_station_ns": warmup_origins,
            "warmup_delivery": "device trained on all frames; untrusted warmup observations not delivered to center; progress and calibration retained",
            "pixel_error_model": "explicit uncalibrated isotropic experiment assumption",
            "calibration": "recorded known calibration remapped to image sessions; auxiliary calibration algorithm not evaluated",
            "residual_jitter_flex": "explicit zero conditional on unperturbed recorded camera poses",
            "center_process_ms": distribution(timings),
            "center_process_ms_scored": distribution(scored_timings),
            "stage_ms_scored": {
                name: distribution(values) for name, values in scored_stage_samples.items()
            },
            "stage_ms": {name: distribution(values) for name, values in stage_samples.items()},
            "timing_scope": "center processing only; window stages exclude ingress outside sealed-window pipeline; serialization, disk IO, scoring, image decoding and device processing excluded",
            "provenance": _provenance(),
        }
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pixel-sigma-px", type=float, required=True)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    args.output.mkdir(parents=True, exist_ok=False)
    record_observations(args.recording, args.policy, args.output / "device")
    replay(
        args.recording,
        args.output / "device/observations.jsonl",
        args.output / "center",
        asset_root=args.asset_root,
        pixel_sigma_px=args.pixel_sigma_px,
    )


if __name__ == "__main__":
    main()
