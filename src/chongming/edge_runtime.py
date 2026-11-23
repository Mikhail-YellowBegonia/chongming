"""Direct device execution and journaled image provenance; no truth inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from chongming.composition.station import (
    StationAssembly,
    validate_device_policies,
    validate_device_policy,
)
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.json_codec import _decode, dump_json, parse_json
from chongming.modules.device import DeviceInputError

__all__ = [
    "EdgeRun",
    "EdgeStepOutput",
    "load_device_policies",
    "validate_device_policies",
    "validate_device_policy",
]


def load_device_policies(
    path: Path, configs: tuple[m.StationConfig, ...]
) -> dict[m.StationId, d.DevicePolicy]:
    policies: dict[m.StationId, d.DevicePolicy] = _decode(
        parse_json(path.read_bytes()), dict[m.StationId, d.DevicePolicy]
    )
    validate_device_policies(policies, configs)
    return policies


@dataclass(frozen=True, kw_only=True)
class EdgeStepOutput:
    frames: tuple[d.DeviceFrameResult, ...]
    calibrations: tuple[m.CalibrationUpdate, ...]
    events: tuple[m.StationEvent, ...]
    solutions: tuple[m.CalibrationSolution, ...]
    progress: tuple[StationEventTimeProgress, ...]


class EdgeRun:
    """Tick stations, process this exposure group's aux/main frames, then progress.

    The source already orders exposure groups. Aux precedes main only within the
    same group; a main frame never waits for a later auxiliary exposure. The
    journal is flushed after each actual result, including before a later failure.
    """

    def __init__(self, stations: tuple[StationAssembly, ...], journal: TextIO) -> None:
        self.stations = {station.config.station_id: station for station in stations}
        self.journal = journal
        self.captures: dict[m.StationId, m.CaptureTime] = {}
        self._calibration_sequence: dict[m.StationId, int] = {}

    def _record(self, now: m.ProcessingTime, kind: str, value: Any) -> None:
        self.journal.write(dump_json({"time": now, "kind": kind, "value": value}) + "\n")
        self.journal.flush()

    def process(
        self, frames: tuple[m.CameraFrame, ...], now: m.ProcessingTime, *, ended: bool = False
    ) -> EdgeStepOutput:
        results = []
        updates = []
        events: list[m.StationEvent] = []
        progress = []

        def calibration(update: m.CalibrationUpdate | None) -> None:
            if update is None:
                return
            envelope = update.state_event.envelope
            previous = self._calibration_sequence.get(envelope.station_id, 0)
            if envelope.sequence_no == previous:
                return  # Existing auxiliary throttle returns the unchanged update.
            if envelope.sequence_no < previous:
                raise ValueError("device calibration event sequence regressed")
            self._calibration_sequence[envelope.station_id] = envelope.sequence_no
            self._record(now, "calibration_update", update)
            updates.append(update)
            events.extend((update.state_event, *update.diagnostics))

        try:
            if ended and frames:
                raise ValueError("stream end cannot contain image frames")
            for station in self.stations.values():
                calibration(station.device.tick(now))
            for frame in sorted(
                frames,
                key=lambda frame: (frame.camera_role is m.CameraRole.MAIN, str(frame.station_id)),
            ):
                self._record(
                    now,
                    "input_frame",
                    {
                        "station_id": frame.station_id,
                        "station_session_id": frame.station_session_id,
                        "camera_role": frame.camera_role,
                        "frame_id": frame.frame_id,
                        "capture_time": frame.capture_time,
                        "camera_config_ref": frame.camera_config_ref,
                        "width_px": frame.image.width_px,
                        "height_px": frame.image.height_px,
                        "row_stride_bytes": frame.image.row_stride_bytes,
                        "pixel_format": frame.image.pixel_format,
                        "pixels_sha256": hashlib.sha256(frame.image.pixels).hexdigest(),
                    },
                )
                station = self.stations[frame.station_id]
                if frame.camera_role is m.CameraRole.AUXILIARY:
                    calibration(station.device.process_auxiliary(frame, now))
                else:
                    result = station.device.process_main(frame, now)
                    self._record(now, "main_result", result)
                    results.append(result)
                    events.extend(result.events)
                    progress.append(result.progress)
                    self.captures[frame.station_id] = frame.capture_time
            if ended:
                progress.extend(
                    StationEventTimeProgress(
                        station_id=station.config.station_id,
                        station_session_id=station.device.station_session_id,
                        station_config_digest=station.config.config_digest,
                        completed_capture_time=self.captures.get(station.config.station_id),
                        received_at=now,
                        stream_ended=True,
                    )
                    for station in self.stations.values()
                )
            # Per-station ordering is authoritative across all three event kinds.
            ordered = tuple(
                sorted(
                    events,
                    key=lambda event: (str(event.envelope.station_id), event.envelope.sequence_no),
                )
            )
            output = EdgeStepOutput(
                frames=tuple(results),
                calibrations=tuple(updates),
                events=ordered,
                solutions=tuple(
                    update.solution for update in updates if update.solution is not None
                ),
                progress=tuple(progress),
            )
            self._record(
                now,
                "step_complete",
                {
                    "event_ids": [
                        (
                            event.envelope.station_id,
                            event.envelope.station_session_id,
                            event.envelope.sequence_no,
                        )
                        for event in ordered
                    ],
                    "progress": output.progress,
                    "stream_ended": ended,
                },
            )
            return output
        except Exception as error:
            self._record(
                now,
                "step_failed",
                {
                    "error": f"{type(error).__name__}: {error}",
                    "diagnostic": error.diagnostic if isinstance(error, DeviceInputError) else None,
                },
            )
            raise
