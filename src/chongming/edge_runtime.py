"""Direct device execution and journaled image provenance; no truth inputs."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from chongming.composition.graph import StationAssembly
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.modules.detection import validate_detection_policy, validate_tracking_policy
from chongming.modules.device import OPENCV_CALIBRATION_ESTIMATOR_ID, DeviceInputError
from chongming.scenarios.json_content import parse_json
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import _decode
from chongming.uncertainty import covariance_factor


def validate_device_policies(
    policies: dict[m.StationId, d.DevicePolicy], configs: tuple[m.StationConfig, ...]
) -> None:
    if set(policies) != {config.station_id for config in configs}:
        raise ValueError("device policy station IDs must match scenario station IDs exactly")
    for policy in policies.values():
        validate_detection_policy(policy.detection)
        validate_tracking_policy(policy.tracking)
        calibration = policy.calibration
        if (
            calibration.estimator_id != OPENCV_CALIBRATION_ESTIMATOR_ID
            or type(calibration.update_period_ns) is not int
            or calibration.update_period_ns < 0
            or type(calibration.stale_after_ns) is not int
            or calibration.stale_after_ns < 0
            or type(calibration.recovery_success_count) is not int
            or calibration.recovery_success_count <= 0
            or type(calibration.minimum_reference_count) is not int
            or calibration.minimum_reference_count < 4
            or not math.isfinite(calibration.maximum_reprojection_rmse_px)
            or calibration.maximum_reprojection_rmse_px <= 0
            or (
                calibration.maximum_rotation_only_parallax_px is not None
                and (
                    not math.isfinite(calibration.maximum_rotation_only_parallax_px)
                    or calibration.maximum_rotation_only_parallax_px < 0
                )
            )
        ):
            raise ValueError("invalid explicitly configured device calibration policy")
        rays = policy.rays
        if rays.uncertainty_model_id != "tangent_gaussian_v1":
            raise ValueError("unsupported device ray uncertainty model")
        has_residual = False
        for covariance, label in (
            (rays.fallback_centroid_covariance_px2, "fallback_centroid_covariance_px2"),
            (rays.unmodelled_high_frequency_jitter_covariance_rad2, "residual_jitter_rad2"),
            (rays.unmodelled_relative_flex_covariance_rad2, "residual_flex_rad2"),
        ):
            if covariance is not None:
                factor = covariance_factor(covariance, 2, label)
                has_residual |= label != "fallback_centroid_covariance_px2" and bool(factor.any())
        if has_residual and (
            not isinstance(rays.residual_independence_basis, str)
            or not rays.residual_independence_basis.strip()
        ):
            raise ValueError("nonzero residuals require an explicit independence basis")


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
        self.journal.write(_json({"time": now, "kind": kind, "value": value}) + "\n")
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
