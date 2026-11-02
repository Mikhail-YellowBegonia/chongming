"""Replay recorded ordinary clean virtual events through the center, without images."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from chongming.composition.development import development_run
from chongming.contracts import development as d
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts import scene as s
from chongming.performance import distribution
from chongming.reference import create_algorithm
from chongming.scenarios.run import _json, _provenance
from chongming.scenarios.serialization import _decode, load_compiled_scenario


def replay(
    recording: Path, output: Path, *, asset_root: Path | None = None, postintersection: bool = False
) -> dict[str, Any]:
    spec = load_compiled_scenario(recording / "scenario.json", asset_root=asset_root)
    virtual = spec.virtual_observations
    if spec.perturbations.camera_pose or spec.perturbations.camera_jitter:
        raise ValueError("ideal center test requires no camera pose or jitter perturbations")
    if virtual.detection_probability != 1 or virtual.false_alarms_per_camera_hour != 0:
        raise ValueError("ideal center test requires complete detection without false alarms")
    if any(v != 0 for row in virtual.independent_angular_covariance_rad2 for v in row) or any(
        v != 0 for row in virtual.independent_origin_covariance_m2 for v in row
    ):
        raise ValueError("ideal center test requires zero independent input errors")
    for interval in virtual.calibration_intervals:
        if any(interval.correction_error_camera_rad) or any(
            v != 0 for row in interval.correction_covariance_rad2 for v in row
        ):
            raise ValueError("ideal center test requires exact virtual calibration")
    manifest = json.loads((recording / "manifest.json").read_text())
    sessions = _decode(manifest["station_sessions"], tuple[m.StationSessionBinding, ...])
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
    (output / "scenario.json").write_bytes((recording / "scenario.json").read_bytes())
    (output / "center-registration.json").write_text(_json(registration) + "\n")
    provenance = _provenance()
    from chongming.reference import create_postintersection_algorithm

    algorithm = create_postintersection_algorithm() if postintersection else create_algorithm()
    samples = []
    last_capture = {}
    count = 0
    status = "failed"
    try:
        algorithm.start(registration)
        with (
            (recording / "online.jsonl").open() as source,
            (output / "center-input.jsonl").open("x") as inputs,
            (output / "center-output.jsonl").open("x") as outputs,
        ):

            def process(step: d.CenterInputStep) -> None:
                inputs.write(_json(step) + "\n")
                began = time.perf_counter()
                result = algorithm.process(step)
                samples.append((time.perf_counter() - began) * 1000)
                outputs.write(_json(result) + "\n")

            for count, line in enumerate(source, 1):
                row = json.loads(line)
                packets = _decode(row["packets"], s.ScenarioPacketOutput)
                now = m.ProcessingTime(
                    clock_domain_id=packets.sample_time.clock_domain_id,
                    time_ns=packets.sample_time.time_ns,
                )
                sampled = set()
                for frame in row["frames"]:
                    if frame["camera_role"] == "main":
                        sampled.add(frame["station_id"])
                        last_capture[frame["station_id"]] = _decode(
                            frame["capture_time"], m.CaptureTime
                        )
                events = sorted(
                    (*packets.calibration_events, *packets.rays, *packets.diagnostics),
                    key=lambda e: (str(e.envelope.station_id), e.envelope.sequence_no),
                )
                progress = tuple(
                    f.StationEventTimeProgress(
                        station_id=b.station_id,
                        station_session_id=b.virtual_session_id,
                        station_config_digest=next(
                            c.config_digest
                            for c in spec.station_configs
                            if c.station_id == b.station_id
                        ),
                        completed_capture_time=last_capture[str(b.station_id)],
                        received_at=now,
                        stream_ended=False,
                    )
                    for b in sessions
                    if str(b.station_id) in sampled
                )
                process(
                    d.CenterInputStep(
                        step_no=count - 1,
                        time=now,
                        solutions=packets.calibration_solutions,
                        events=tuple(m.ReceivedEvent(event=e, arrival_time=now) for e in events),
                        progress=progress,
                        stream_ended=False,
                    )
                )
                if count % 100 == 0:
                    print(f"Processed {count} recorded steps", flush=True)
            if not count:
                raise ValueError("empty recording")
            now = m.ProcessingTime(
                clock_domain_id=now.clock_domain_id, time_ns=spec.timeline.end_time_ns + 1
            )
            progress = tuple(
                f.StationEventTimeProgress(
                    station_id=b.station_id,
                    station_session_id=b.virtual_session_id,
                    station_config_digest=next(
                        c.config_digest
                        for c in spec.station_configs
                        if c.station_id == b.station_id
                    ),
                    completed_capture_time=last_capture[str(b.station_id)],
                    received_at=now,
                    stream_ended=True,
                )
                for b in sessions
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
            "format": "center-recorded-replay-v1",
            "status": status,
            "postintersection": postintersection,
            "source_recording": str(recording.resolve()),
            "steps": count,
            "input": "ordinary recorded clean virtual packets, no truth supplied to algorithm",
            "evaluation_provenance": provenance,
            "center_process_ms": distribution(samples),
        }
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--postintersection", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            replay(
                args.recording,
                args.output,
                asset_root=args.asset_root,
                postintersection=args.postintersection,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
