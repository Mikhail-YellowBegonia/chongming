"""Independent device workbench using only the public ordinary video exit."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chongming.contracts.development import (
    DeviceAlgorithmOutput,
    DeviceInputStep,
    DeviceRegistration,
    DeviceStationRegistration,
)
from chongming.contracts.model import (
    CameraFrame,
    CameraRole,
    DiagnosticEvent,
    EventType,
    FrameRef,
    ProcessingTime,
    StationId,
)
from chongming.contracts.scene import ScenarioStep
from chongming.development import DevelopmentSession, development_cli
from chongming.modules.device import EventSequencerModule
from chongming.ports.development import DeviceAlgorithm


def _frame_ref(frame: CameraFrame) -> FrameRef:
    return FrameRef(
        station_id=frame.station_id,
        station_session_id=frame.station_session_id,
        camera_role=frame.camera_role,
        frame_id=frame.frame_id,
    )


class FrameInspector:
    """Runnable boundary probe; it does not claim detection or calibration success."""

    def start(self, registration: DeviceRegistration) -> None:
        self._sequencers = {
            station.config.station_id: EventSequencerModule(
                station_id=station.config.station_id,
                station_session_id=station.station_session_id,
                schema_version="event-v1",
            )
            for station in registration.stations
        }

    def process_main(self, frame: CameraFrame, now: ProcessingTime) -> DeviceAlgorithmOutput:
        diagnostic = DiagnosticEvent(
            envelope=self._sequencers[frame.station_id].next_envelope(
                EventType.DIAGNOSTIC, frame.camera_config_ref
            ),
            related_frame_ref=_frame_ref(frame),
            capture_time=frame.capture_time,
            category="calibration_unavailable",
            reason_codes=("algorithm_unimplemented", "no_trusted_calibration"),
            related_config_ref=frame.camera_config_ref,
            related_calibration_ref=None,
        )
        return DeviceAlgorithmOutput(events=(diagnostic,))

    def process_auxiliary(self, frame: CameraFrame, now: ProcessingTime) -> DeviceAlgorithmOutput:
        return DeviceAlgorithmOutput(reason_codes=("calibration_algorithm_unimplemented",))

    def tick(self, step: DeviceInputStep) -> DeviceAlgorithmOutput:
        return DeviceAlgorithmOutput(reason_codes=("algorithm_unimplemented",))

    def close(self) -> None:
        self._sequencers.clear()


@dataclass(frozen=True, kw_only=True)
class _DeviceSample:
    step_no: int
    time: ProcessingTime
    stream_ended: bool
    video_enabled: bool
    frames: tuple[CameraFrame, ...]


class DeviceSession(DevelopmentSession[_DeviceSample]):
    """Dispatch each available station/role independently, then one logical tick."""

    scope = "device-development"
    stream_name = "device"
    algorithm_timing_key = "device_ms"

    def __init__(
        self,
        path: Path,
        output: Path,
        *,
        algorithm: DeviceAlgorithm | None = None,
        video_enabled: bool = True,
    ) -> None:
        self.algorithm = algorithm if algorithm is not None else FrameInspector()
        self.algorithm_name = type(self.algorithm).__name__
        self.frame_count = 0
        self.main_count = 0
        self.auxiliary_count = 0
        self._stations: dict[StationId, DeviceStationRegistration] = {}
        super().__init__(path, output, video_enabled=video_enabled)

    def _start_algorithm(self) -> DeviceRegistration:
        registration = DeviceRegistration(
            stations=tuple(
                DeviceStationRegistration(
                    config=config,
                    station_session_id=next(
                        binding.image_session_id
                        for binding in self.source.station_sessions
                        if binding.station_id == config.station_id
                    ),
                )
                for config in self.spec.station_configs
            ),
            clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id,
            run_id=self.run_spec.run_id,
        )
        self._stations = {item.config.station_id: item for item in registration.stations}
        self.algorithm.start(registration)
        return registration

    def _close_algorithm(self) -> None:
        self.algorithm.close()

    def _input(self, step: ScenarioStep | None) -> _DeviceSample:
        # Deliberately use only the ordinary video exit: no packet calibration,
        # camera truth, target IDs, actual poses or perturbation configuration.
        video = step.video if step is not None else None
        return _DeviceSample(
            step_no=self.step_count,
            time=ProcessingTime(
                clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id,
                time_ns=(
                    video.sample_time.time_ns
                    if video is not None
                    else self.spec.timeline.end_time_ns + 1
                ),
            ),
            stream_ended=video is None,
            video_enabled=self.source.video_enabled,
            frames=video.frames if video is not None else (),
        )

    def _online_evidence(self, online: _DeviceSample) -> object:
        # Pixels are stored once by SceneEvidenceWriter's PNG streams.
        return {
            "step_no": online.step_no,
            "time": online.time,
            "video_enabled": online.video_enabled,
            "stream_ended": online.stream_ended,
            "frames": tuple(
                {
                    "ref": _frame_ref(frame),
                    "capture_time": frame.capture_time,
                    "camera_config_ref": frame.camera_config_ref,
                    "width_px": frame.image.width_px,
                    "height_px": frame.image.height_px,
                    "pixel_format": frame.image.pixel_format,
                    "row_stride_bytes": frame.image.row_stride_bytes,
                }
                for frame in online.frames
            ),
        }

    def _validate_frame(self, frame: CameraFrame) -> None:
        station = self._stations.get(frame.station_id)
        if station is None:
            raise ValueError(f"unregistered frame station: {frame.station_id}")
        if frame.station_session_id != station.station_session_id:
            raise ValueError("frame image session does not match registration")
        if frame.camera_config_ref != station.config.config_digest:
            raise ValueError("frame camera configuration does not match registration")
        if frame.camera_role not in (CameraRole.MAIN, CameraRole.AUXILIARY):
            raise ValueError("unsupported frame camera role")

    def _process(self, online: _DeviceSample) -> dict[str, Any]:
        calls = []
        # Validate the whole input group before any station state is mutated.
        for frame in online.frames:
            self._validate_frame(frame)
        # At a simultaneous exposure MAIN is dispatched first. Auxiliary output
        # can never become a same-exposure prerequisite for main processing.
        for frame in sorted(online.frames, key=lambda f: f.camera_role is CameraRole.AUXILIARY):
            began = time.perf_counter()
            if frame.camera_role is CameraRole.MAIN:
                result = self.algorithm.process_main(frame, online.time)
            else:
                result = self.algorithm.process_auxiliary(frame, online.time)
            duration_ms = (time.perf_counter() - began) * 1000
            if not isinstance(result, DeviceAlgorithmOutput):
                raise TypeError("device callbacks must return DeviceAlgorithmOutput")
            calls.append(
                {
                    "kind": frame.camera_role.value,
                    "frame_ref": _frame_ref(frame),
                    "output": result,
                    "algorithm_ms": duration_ms,
                }
            )
        began = time.perf_counter()
        result = self.algorithm.tick(
            DeviceInputStep(
                step_no=online.step_no,
                time=online.time,
                video_enabled=online.video_enabled,
                stream_ended=online.stream_ended,
            )
        )
        duration_ms = (time.perf_counter() - began) * 1000
        if not isinstance(result, DeviceAlgorithmOutput):
            raise TypeError("device tick must return DeviceAlgorithmOutput")
        calls.append({"kind": "tick", "output": result, "algorithm_ms": duration_ms})
        return {"calls": calls, "frame_count": len(online.frames)}

    def _count_sample(self, step: ScenarioStep) -> None:
        self.frame_count += len(step.video.frames)
        self.main_count += sum(f.camera_role is CameraRole.MAIN for f in step.video.frames)
        self.auxiliary_count += sum(
            f.camera_role is CameraRole.AUXILIARY for f in step.video.frames
        )

    def _counts(self) -> dict[str, int]:
        return {
            "image_frames": self.frame_count,
            "main_frames": self.main_count,
            "auxiliary_frames": self.auxiliary_count,
        }


def main() -> None:
    development_cli(
        description="端侧帧算法开发工具；默认生产视频、暂停，无服务端口",
        prompt="device",
        default_factory=FrameInspector,
        session_factory=DeviceSession,
        video_default=True,
    )


if __name__ == "__main__":
    main()
