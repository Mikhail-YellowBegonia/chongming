"""Discrete device/center workbench. All declared modules retain their lifecycle."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from chongming.center_ui import CenterRunUi
from chongming.composition.development import development_run
from chongming.composition.graph import declare_assembly
from chongming.contracts.development import (
    CenterAlgorithmResult,
    CenterInputStep,
    CenterRegistration,
)
from chongming.contracts.device import DevicePolicy
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.model import (
    CameraRole,
    CaptureTime,
    DiagnosticEvent,
    ProcessingTime,
    RayEvent,
    ReceivedEvent,
    StationEvent,
    StationId,
)
from chongming.contracts.runtime import EventPath, ModuleHealth
from chongming.contracts.scene import ScenarioStep
from chongming.edge_runtime import (
    EdgeRun,
    EdgeStepOutput,
    load_device_policies,
    validate_device_policies,
)
from chongming.ports.development import CenterAlgorithm
from chongming.rendering.godot import default_renderer
from chongming.scenarios.run import SceneEvidenceWriter, _json, _provenance
from chongming.scenarios.serialization import load_scenario, validate_supported_features

logger = logging.getLogger(__name__)


class InputInspector:
    """Validate/register inputs; deliberately performs no localization."""

    def start(self, registration: CenterRegistration) -> None:
        self.stations = {c.station_id: c for c in registration.stations}
        self.solutions: dict[Any, Any] = {}
        self.last_sequence: dict[tuple[str, str], int] = {}

    def process(self, step: CenterInputStep) -> CenterAlgorithmResult:
        for solution in step.solutions:
            self.solutions[solution.solution_ref] = solution
        for received in step.events:
            event = received.event
            envelope = event.envelope
            config = self.stations[envelope.station_id]
            if envelope.station_config_digest != config.config_digest:
                raise ValueError("event config mismatch")
            key = (str(envelope.station_id), str(envelope.station_session_id))
            if envelope.sequence_no <= self.last_sequence.get(key, 0):
                raise ValueError("non-increasing event sequence")
            self.last_sequence[key] = envelope.sequence_no
            if isinstance(event, RayEvent):
                ref = event.calibration_solution_ref
                if ref not in self.solutions:
                    raise ValueError("ray calibration not registered")
                direction = event.ray_direction_enu_unit
                if not math.isclose(sum(v * v for v in direction), 1, abs_tol=1e-8):
                    raise ValueError("ray direction is not a unit vector")
        return CenterAlgorithmResult(measurements=(), tracks=(), batches=())

    def close(self) -> None:
        self.solutions.clear()


class CenterSession:
    """Synchronous step ownership: callers cannot advance through monitoring."""

    def __init__(
        self,
        path: Path,
        output: Path,
        *,
        algorithm: CenterAlgorithm | None = None,
        video_enabled: bool = False,
        mode: int = 3,
        device_policies: dict[StationId, DevicePolicy] | None = None,
    ) -> None:
        if type(mode) is not int or mode not in (2, 3, 4):
            raise ValueError("mode must be 2, 3 or 4")
        if mode == 2 and algorithm is not None:
            raise ValueError("mode 2 does not accept a center algorithm")
        if mode == 3 and device_policies is not None:
            raise ValueError("device policies are only used in modes 2 and 4")
        self.mode = mode
        self.video_enabled = video_enabled
        self.path = path
        self.spec = load_scenario(path)
        validate_supported_features(self.spec)
        self.run_spec = development_run(self.spec, output)
        if mode != 3:
            if device_policies is None:
                raise ValueError("modes 2 and 4 require explicit per-station device policies")
            validate_device_policies(device_policies, self.spec.station_configs)
            self.run_spec = replace(
                self.run_spec,
                center_input=EventPath.IMAGE_PIPELINE,
                stations=tuple(
                    replace(station, device_policy=device_policies[station.sessions.station_id])
                    for station in self.run_spec.stations
                ),
            )
        self.assembly = declare_assembly(self.spec, self.run_spec)
        self.source = self.assembly.scene.scenario
        self.source.video_enabled = video_enabled or mode != 3
        self.source.virtual_enabled = mode == 3
        if algorithm is None and mode == 4:
            from chongming.reference import create_algorithm

            algorithm = create_algorithm()
        self.algorithm: CenterAlgorithm | None = (
            None if mode == 2 else algorithm if algorithm is not None else InputInspector()
        )
        with ExitStack() as resources:
            self.writer = SceneEvidenceWriter(output, self.spec)
            resources.callback(self.writer.close)
            self._online = (
                resources.enter_context((output / "center-input.jsonl").open("x", encoding="utf-8"))
                if mode != 2
                else None
            )
            self._results = (
                resources.enter_context(
                    (output / "center-output.jsonl").open("x", encoding="utf-8")
                )
                if mode != 2
                else None
            )
            self.edge = (
                EdgeRun(
                    self.assembly.stations,
                    resources.enter_context(
                        (output / "edge-output.jsonl").open("x", encoding="utf-8")
                    ),
                )
                if mode != 3
                else None
            )
            self._resources = resources.pop_all()
        self._steps: Iterator[ScenarioStep] = iter(self.source.generate(self.spec))
        self._last_capture: dict[str, CaptureTime] = {}
        self.health: list[ModuleHealth] = []
        self.step_count = 0
        self.ray_count = 0
        self.ended = False
        self.closed = False
        self.last: dict[str, Any] | None = None
        self.error: str | None = None
        self.status = "paused"
        self._step_lock = threading.Lock()
        self._ui: CenterRunUi | None = None
        self._algorithm_started = False
        self._started_bindings = []
        try:
            now = ProcessingTime(
                clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id, time_ns=0
            )
            for binding in self.assembly.bindings:
                # Include a module whose initializer fails after allocating resources.
                self._started_bindings.append(binding)
                self.health.append(binding.module.start_module(binding.node.module_key, now))
            registration = CenterRegistration(
                stations=self.spec.station_configs,
                center_session_id=str(self.run_spec.center_session_id),
                policy=self.run_spec.fusion_policy,
                clock_mapping=self.run_spec.processing_to_capture_clock,
                started_at=now,
            )
            if self.algorithm is not None:
                self._algorithm_started = True
                self.algorithm.start(registration)
                self.writer.document("center-registration.json", registration)
            self.writer.document("authoring.json", json.loads(path.read_text()))
            self.writer.document(
                "manifest.json",
                {
                    **_provenance(),
                    "scope": "edge-development"
                    if mode == 2
                    else "integrated-development"
                    if mode == 4
                    else "center-development",
                    "execution_mode": mode,
                    "input_source": "rendered_images" if mode != 3 else "virtual_rays",
                    "virtual_events_enabled": self.source.virtual_enabled,
                    "sensor_rendering_required": mode != 3,
                    "center_algorithm_started": self._algorithm_started,
                    "run_id": self.run_spec.run_id,
                    "run_spec": self.run_spec,
                    "algorithm": type(self.algorithm).__name__
                    if self.algorithm is not None
                    else None,
                    "startup": self.health,
                    "all_modules_initialized": True,
                    "algorithms_complete": False,
                    "video_enabled": video_enabled,
                    "renderer_version": default_renderer().version,
                    "station_sessions": self.source.station_sessions,
                },
            )
        except BaseException as error:
            self.status = "failed"
            self.error = f"{type(error).__name__}: {error}"
            try:
                self.close()
            except Exception:
                logger.exception("Cleanup after initialization failure failed")
            raise

    def _input(
        self, step: ScenarioStep | None, edge: EdgeStepOutput | None = None
    ) -> CenterInputStep:
        ended = step is None
        clock = self.spec.timeline.clock_domain.clock_domain_id
        t = self.spec.timeline.end_time_ns + 1 if step is None else step.packets.sample_time.time_ns
        now = ProcessingTime(clock_domain_id=clock, time_ns=t)
        if edge is not None:
            return CenterInputStep(
                step_no=self.step_count,
                time=now,
                solutions=edge.solutions,
                events=tuple(ReceivedEvent(event=event, arrival_time=now) for event in edge.events),
                progress=edge.progress,
                stream_ended=ended,
            )
        events: tuple[StationEvent, ...]
        if step is not None:
            for frame in step.truth.camera_frames:
                if frame.camera_role is CameraRole.MAIN:
                    self._last_capture[str(frame.station_id)] = frame.capture_time
            solutions = step.packets.calibration_solutions
            events = tuple(
                sorted(
                    (
                        *step.packets.calibration_events,
                        *step.packets.rays,
                        *step.packets.diagnostics,
                    ),
                    key=lambda e: (str(e.envelope.station_id), e.envelope.sequence_no),
                )
            )
            sampled = {
                str(f.station_id)
                for f in step.truth.camera_frames
                if f.camera_role is CameraRole.MAIN
            }
        else:
            solutions, events = (), ()
            sampled = {str(c.station_id) for c in self.spec.station_configs}
        progress = tuple(
            StationEventTimeProgress(
                station_id=binding.station_id,
                station_session_id=binding.virtual_session_id,
                station_config_digest=next(
                    c.config_digest
                    for c in self.spec.station_configs
                    if c.station_id == binding.station_id
                ),
                completed_capture_time=self._last_capture.get(str(binding.station_id)),
                received_at=now,
                stream_ended=ended,
            )
            for binding in self.source.station_sessions
            if str(binding.station_id) in sampled
        )
        return CenterInputStep(
            step_no=self.step_count,
            time=now,
            solutions=solutions,
            events=tuple(ReceivedEvent(event=e, arrival_time=now) for e in events),
            progress=progress,
            stream_ended=ended,
        )

    def step(self) -> dict[str, Any]:
        if not self._step_lock.acquire(blocking=False):
            raise RuntimeError("a step is already in progress")
        try:
            return self._step()
        finally:
            self._step_lock.release()

    def _step(self) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("session is closed")
        if self.ended:
            return self.snapshot()
        try:
            step = next(self._steps, None)
            edge_output = None
            edge_ms = None
            if self.edge is not None:
                if step is not None and step.video.skipped_frame_ids:
                    raise RuntimeError("edge modes require every scheduled sensor image")
                began = time.perf_counter()
                edge_output = self.edge.process(
                    step.video.frames if step is not None else (),
                    ProcessingTime(
                        clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id,
                        time_ns=step.video.sample_time.time_ns
                        if step is not None
                        else self.spec.timeline.end_time_ns + 1,
                    ),
                    ended=step is None,
                )
                edge_ms = (time.perf_counter() - began) * 1000
            online = self._input(step, edge_output)
            if self._online is not None:
                self._online.write(_json(online) + "\n")
                self._online.flush()
            if step is not None:
                evidence_step = (
                    step
                    if self.video_enabled
                    else replace(step, video=replace(step.video, frames=()))
                )
                self.writer.record(evidence_step)
            result = None
            center_ms = None
            if self.algorithm is not None:
                began = time.perf_counter()
                result = self.algorithm.process(online)
                center_ms = (time.perf_counter() - began) * 1000
                assert self._results is not None
                self._results.write(
                    _json(
                        {
                            "step_no": online.step_no,
                            "time": online.time,
                            "measurements": result.measurements,
                            "tracks": result.tracks,
                            "batches": result.batches,
                            "center_ms": center_ms,
                            "stream_ended": online.stream_ended,
                        }
                    )
                    + "\n"
                )
                self._results.flush()
            rays = sum(isinstance(received.event, RayEvent) for received in online.events)
            if step is not None:
                self.step_count += 1
                self.ray_count += rays
            else:
                self.ended = True
                self.status = "complete"
            self.last = {
                "step_no": online.step_no,
                "time_ns": online.time.time_ns,
                "ray_count": rays,
                "progress_count": len(online.progress),
                "stream_ended": self.ended,
                "video_enabled": self.video_enabled,
                "measurements": asdict_list(result.measurements) if result is not None else [],
                "tracks": asdict_list(result.tracks) if result is not None else [],
                "batches": asdict_list(result.batches) if result is not None else [],
                "center_ms": center_ms,
                "edge_ms": edge_ms,
                "edge_frame_count": len(edge_output.frames) if edge_output is not None else 0,
                "edge_diagnostic_count": sum(
                    isinstance(event, DiagnosticEvent) for event in edge_output.events
                )
                if edge_output is not None
                else 0,
                "stages": asdict(step.truth.timing) if step and step.truth.timing else None,
            }
            if self._ui is not None:
                self._ui.publish(step, result, edge_output=edge_output)
            return self.snapshot()
        except BaseException as error:
            self.status = "failed"
            self.error = f"{type(error).__name__}: {error}"
            try:
                self.close()
            except Exception:
                logger.exception("Cleanup after step failure failed")
            raise

    def run_ui(self, *, tick_hz: float = 10) -> None:
        """Run synchronously to completion or quit, then close this worker and run."""
        if self.closed or self.step_count or self.ended or self._ui is not None:
            raise RuntimeError("start a new session for each UI run")
        try:
            self._ui = CenterRunUi(self, default_renderer())
            self._ui.run(tick_hz)
        except BaseException as error:
            self.status = "failed"
            self.error = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.close()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.status,
            "logical_steps": self.step_count,
            "ray_events": self.ray_count,
            "last": self.last,
            "error": self.error,
        }

    def set_video(self, enabled: bool) -> None:
        if not self._step_lock.acquire(blocking=False):
            raise RuntimeError("change video only between completed steps")
        try:
            if self.closed:
                raise RuntimeError("session is closed")
            self.video_enabled = enabled
            self.source.video_enabled = enabled or self.mode != 3
        finally:
            self._step_lock.release()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors = []
        if self._algorithm_started:
            try:
                assert self.algorithm is not None
                self.algorithm.close()
            except Exception as error:
                logger.exception("Algorithm cleanup failed")
                errors.append(str(error))
        now = ProcessingTime(
            clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id,
            time_ns=self.last["time_ns"] if self.last else 0,
        )
        shutdown = []
        for binding in reversed(self._started_bindings):
            try:
                shutdown.append(binding.module.stop_module(binding.node.module_key, now))
            except Exception as error:
                logger.exception("Module cleanup failed")
                errors.append(str(error))
        close_source = getattr(self._steps, "close", None)
        if close_source is not None:
            try:
                close_source()
            except Exception as error:
                logger.exception("Source cleanup failed")
                errors.append(str(error))
        try:
            self.writer.document(
                "summary.json",
                {
                    **self.snapshot(),
                    "state": self.status if self.status in {"failed", "complete"} else "stopped",
                    "shutdown": shutdown,
                    "shutdown_errors": errors,
                },
            )
        finally:
            try:
                self._resources.close()
            finally:
                from chongming.rendering.godot import close_default_renderer

                close_default_renderer()
        if errors:
            raise RuntimeError(f"shutdown failed: {errors}")


def asdict_list(items: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="设备、中心及集成开发工具；默认模式 3、无录像、暂停"
    )
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="只校验 JSON，不创建运行或输出")
    parser.add_argument("--run", action="store_true", help="连续运行至有限终点")
    parser.add_argument("--ui", action="store_true", help="Godot 显示与诊断，有限结束后退出")
    parser.add_argument(
        "--video",
        action="store_true",
        help="显示传感器图像并录制证据视频；不控制模式 2／4 的算法成像",
    )
    parser.add_argument(
        "--mode", type=int, choices=(2, 3, 4), default=3, help="2 边缘、3 中心（默认）、4 集成"
    )
    parser.add_argument(
        "--device-policy", type=Path, help="模式 2／4 必填：按站点 ID 提供完整 DevicePolicy 的 JSON"
    )
    parser.add_argument("--tick-hz", type=float, default=10)
    parser.add_argument("--algorithm", help="受信任的 Python module:factory；JSON 不加载代码")
    args = parser.parse_args()
    spec = load_scenario(args.scenario)
    validate_supported_features(spec)
    if args.mode == 2 and args.algorithm is not None:
        parser.error("mode 2 does not accept --algorithm")
    if args.mode == 3 and args.device_policy is not None:
        parser.error("--device-policy is only used in modes 2 and 4")
    policies = None
    if args.mode != 3:
        if args.device_policy is None:
            parser.error("modes 2 and 4 require --device-policy")
        try:
            policies = load_device_policies(args.device_policy, spec.station_configs)
        except (OSError, TypeError, ValueError) as error:
            parser.error(str(error))
    if args.check:
        print(
            f"有效：{spec.scenario_id}，{len(spec.station_configs)} 站，"
            f"{len(spec.scene_definition.flying_objects)} 目标"
        )
        return
    if args.output is None:
        parser.error("--output is required unless --check")
    if not math.isfinite(args.tick_hz) or not 0 < args.tick_hz <= 240:
        parser.error("--tick-hz must be in (0, 240]")
    factory: Any = InputInspector
    if args.mode == 4 or (args.mode == 3 and args.ui):
        from chongming.reference import create_algorithm

        factory = create_algorithm
    if args.algorithm:
        module, sep, name = args.algorithm.partition(":")
        if not sep:
            parser.error("--algorithm must be module:factory")
        factory = getattr(importlib.import_module(module), name)
    session = CenterSession(
        args.scenario,
        args.output,
        algorithm=factory() if args.mode != 2 else None,
        video_enabled=args.video,
        mode=args.mode,
        device_policies=policies,
    )
    try:
        if args.ui:
            session.run_ui(tick_hz=args.tick_hz)
            return
        if not args.run:
            print("已暂停。命令：step / run [步数] / video on|off / status / reset / quit")
        while True:
            command = "run" if args.run else input("center> ").strip()
            parts = command.split()
            if not parts:
                continue
            if parts[0] == "quit":
                break
            if parts[0] == "step":
                print(_json(session.step()))
            elif parts[0] == "status":
                print(_json(session.snapshot()))
            elif parts[0] == "video" and parts[1:] in (["on"], ["off"]):
                session.set_video(parts[1] == "on")
            elif parts[0] == "reset":
                session.close()
                path = args.output.with_name(f"{args.output.name}-reset-{time.time_ns()}")
                session = CenterSession(
                    args.scenario,
                    path,
                    algorithm=factory() if args.mode != 2 else None,
                    video_enabled=args.video,
                    mode=args.mode,
                    device_policies=policies,
                )
            elif parts[0] == "run":
                if len(parts) > 2 or (
                    len(parts) == 2 and (not parts[1].isdigit() or int(parts[1]) <= 0)
                ):
                    print("步数必须为正整数：run [步数]")
                    continue
                count = int(parts[1]) if len(parts) == 2 else None
                interrupted = False

                def request_pause(signum: int, frame: Any) -> None:
                    nonlocal interrupted
                    interrupted = True

                old_handler = signal.signal(signal.SIGINT, request_pause)
                try:
                    while not interrupted and not session.ended and (count is None or count > 0):
                        began = time.perf_counter()
                        print(_json(session.step()))
                        if count is not None:
                            count -= 1
                        time.sleep(max(0, 1 / args.tick_hz - (time.perf_counter() - began)))
                finally:
                    signal.signal(signal.SIGINT, old_handler)
                if interrupted:
                    print("已暂停；当前步已提交。")
                    if args.run:
                        break
                if args.run:
                    break
            else:
                print("命令：step / run [步数] / video on|off / status / reset / quit")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
