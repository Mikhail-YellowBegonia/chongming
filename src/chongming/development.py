"""Discrete clean-ray workbench. All declared modules retain their lifecycle."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import signal
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from chongming.composition.development import development_run
from chongming.composition.graph import declare_assembly
from chongming.contracts.development import CenterInputStep, CenterRegistration
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.contracts.model import (
    CameraRole,
    CaptureTime,
    FusionMeasurement,
    ProcessingTime,
    RayEvent,
    ReceivedEvent,
    StationEvent,
    TrackEvent,
)
from chongming.contracts.runtime import ModuleHealth
from chongming.contracts.scene import ScenarioStep
from chongming.ports.development import CenterAlgorithm
from chongming.ports.fusion import FusionEngine
from chongming.rendering.godot import default_renderer
from chongming.scenarios.run import SceneEvidenceWriter, _json, _provenance
from chongming.scenarios.serialization import load_scenario, validate_supported_features

logger = logging.getLogger(__name__)


class FusionEngineAdapter:
    """Use the existing FusionEngine port without exposing privileged scenario data."""

    def __init__(self, engine: FusionEngine) -> None:
        self.engine = engine

    def start(self, registration: CenterRegistration) -> None:
        for station in registration.stations:
            result = self.engine.register_station(station)
            if result.status.value not in {"registered", "already_registered"}:
                raise ValueError(f"station registration failed: {result.reason_codes}")

    def process(
        self,
        step: CenterInputStep,
    ) -> tuple[tuple[FusionMeasurement, ...], tuple[TrackEvent, ...]]:
        for solution in step.solutions:
            result = self.engine.register_calibration(solution)
            if result.status.value not in {"registered", "already_registered"}:
                raise ValueError(f"calibration registration failed: {result.reason_codes}")
        batches = [self.engine.process(event) for event in step.events]
        batches.extend(self.engine.report_progress(progress) for progress in step.progress)
        batches.append(self.engine.tick(step.time))
        return (
            tuple(m for batch in batches for m in batch.measurements),
            tuple(t for batch in batches for t in batch.tracks),
        )

    def close(self) -> None:
        close_engine = getattr(self.engine, "close", None)
        if close_engine is not None:
            close_engine()


class InputInspector:
    """Validate/register inputs; deliberately performs no localization."""

    def start(self, registration: CenterRegistration) -> None:
        self.stations = {c.station_id: c for c in registration.stations}
        self.solutions: dict[Any, Any] = {}
        self.last_sequence: dict[tuple[str, str], int] = {}

    def process(self, step: CenterInputStep) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
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
        return (), ()

    def close(self) -> None:
        self.solutions.clear()


class _TimedStep(Protocol):
    @property
    def step_no(self) -> int: ...

    @property
    def time(self) -> ProcessingTime: ...

    @property
    def stream_ended(self) -> bool: ...


class DevelopmentSession[InputT: _TimedStep](ABC):
    """Shared lifecycle, synchronous scheduling, evidence and failure cleanup.

    Subclasses select an online boundary; only the evidence writer receives the
    complete public ScenarioStep. No scenario scheduler is duplicated here.
    """

    scope: str
    stream_name: str
    algorithm_name: str
    algorithm_timing_key: str

    def __init__(
        self,
        path: Path,
        output: Path,
        *,
        video_enabled: bool = False,
    ) -> None:
        self.path = path
        self.spec = load_scenario(path)
        validate_supported_features(self.spec)
        self.run_spec = development_run(self.spec, output)
        self.assembly = declare_assembly(self.spec, self.run_spec)
        self.source = self.assembly.scene.scenario
        self.source.video_enabled = video_enabled
        with ExitStack() as resources:
            self.writer = SceneEvidenceWriter(output, self.spec)
            resources.callback(self.writer.close)
            self._online = resources.enter_context(
                (output / f"{self.stream_name}-input.jsonl").open("x", encoding="utf-8")
            )
            self._results = resources.enter_context(
                (output / f"{self.stream_name}-output.jsonl").open("x", encoding="utf-8")
            )
            self._resources = resources.pop_all()
        self._steps: Iterator[ScenarioStep] = iter(self.source.generate(self.spec))
        self.health: list[ModuleHealth] = []
        self.step_count = 0
        self.ended = False
        self.closed = False
        self.last: dict[str, Any] | None = None
        self.error: str | None = None
        self.status = "paused"
        self._step_lock = threading.Lock()
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
            self._algorithm_started = True
            registration = self._start_algorithm()
            self.writer.document(f"{self.stream_name}-registration.json", registration)
            self.writer.document("authoring.json", json.loads(path.read_text()))
            self.writer.document(
                "manifest.json",
                {
                    **_provenance(),
                    "scope": self.scope,
                    "run_id": self.run_spec.run_id,
                    "run_spec": self.run_spec,
                    "algorithm": self.algorithm_name,
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

    @abstractmethod
    def _start_algorithm(self) -> object: ...

    @abstractmethod
    def _close_algorithm(self) -> None: ...

    @abstractmethod
    def _input(self, step: ScenarioStep | None) -> InputT: ...

    @abstractmethod
    def _process(self, online: InputT) -> dict[str, Any]: ...

    def _online_evidence(self, online: InputT) -> object:
        return online

    def _count_sample(self, step: ScenarioStep) -> None:
        pass

    def _counts(self) -> dict[str, int]:
        return {}

    def _step_fields(self, online: InputT, result: dict[str, Any]) -> dict[str, Any]:
        return result

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
            online = self._input(step)
            self._online.write(_json(self._online_evidence(online)) + "\n")
            self._online.flush()
            if step is not None:
                self.writer.record(step)
            began = time.perf_counter()
            result = self._process(online)
            algorithm_ms = (time.perf_counter() - began) * 1000
            self._results.write(
                _json(
                    {
                        "step_no": online.step_no,
                        "time": online.time,
                        **result,
                        self.algorithm_timing_key: algorithm_ms,
                        "stream_ended": online.stream_ended,
                    }
                )
                + "\n"
            )
            self._results.flush()
            if step is not None:
                self.step_count += 1
                self._count_sample(step)
            else:
                self.ended = True
                self.status = "complete"
            self.last = {
                "step_no": online.step_no,
                "time_ns": online.time.time_ns,
                "stream_ended": self.ended,
                "video_enabled": self.source.video_enabled,
                **self._step_fields(online, result),
                self.algorithm_timing_key: algorithm_ms,
                "stages": asdict(step.truth.timing) if step and step.truth.timing else None,
            }
            return self.snapshot()
        except BaseException as error:
            self.status = "failed"
            self.error = f"{type(error).__name__}: {error}"
            try:
                self.close()
            except Exception:
                logger.exception("Cleanup after step failure failed")
            raise

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.status,
            "logical_steps": self.step_count,
            **self._counts(),
            "last": self.last,
            "error": self.error,
        }

    def set_video(self, enabled: bool) -> None:
        if not self._step_lock.acquire(blocking=False):
            raise RuntimeError("change video only between completed steps")
        try:
            if self.closed:
                raise RuntimeError("session is closed")
            self.source.video_enabled = enabled
        finally:
            self._step_lock.release()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors = []
        if self._algorithm_started:
            try:
                self._close_algorithm()
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


class CenterSession(DevelopmentSession[CenterInputStep]):
    """Center adapter preserving the existing plugin and evidence contract."""

    scope = "center-development"
    stream_name = "center"
    algorithm_timing_key = "center_ms"

    def __init__(
        self,
        path: Path,
        output: Path,
        *,
        algorithm: CenterAlgorithm | None = None,
        video_enabled: bool = False,
    ) -> None:
        self.algorithm = algorithm if algorithm is not None else InputInspector()
        self.algorithm_name = type(self.algorithm).__name__
        self._last_capture: dict[str, CaptureTime] = {}
        self.ray_count = 0
        super().__init__(path, output, video_enabled=video_enabled)

    def _start_algorithm(self) -> CenterRegistration:
        registration = CenterRegistration(
            stations=self.spec.station_configs,
            search=self.spec.voxel_search,
            clock_domain_id=self.spec.timeline.clock_domain.clock_domain_id,
            center_session_id=str(self.run_spec.center_session_id),
        )
        self.algorithm.start(registration)
        return registration

    def _close_algorithm(self) -> None:
        self.algorithm.close()

    def _input(self, step: ScenarioStep | None) -> CenterInputStep:
        ended = step is None
        clock = self.spec.timeline.clock_domain.clock_domain_id
        t = self.spec.timeline.end_time_ns + 1 if step is None else step.packets.sample_time.time_ns
        now = ProcessingTime(clock_domain_id=clock, time_ns=t)
        events: tuple[StationEvent, ...]
        if step is not None:
            # Empty main exposures still advance event progress when no rays were
            # emitted. This metadata is retained only by the host, never plugins.
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

    def _process(self, online: CenterInputStep) -> dict[str, Any]:
        measurements, tracks = self.algorithm.process(online)
        return {"measurements": measurements, "tracks": tracks}

    def _count_sample(self, step: ScenarioStep) -> None:
        self.ray_count += len(step.packets.rays)

    def _counts(self) -> dict[str, int]:
        return {"ray_events": self.ray_count}

    def _step_fields(self, online: CenterInputStep, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "ray_count": sum(isinstance(e.event, RayEvent) for e in online.events),
            "progress_count": len(online.progress),
            "measurements": asdict_list(result["measurements"]),
            "tracks": asdict_list(result["tracks"]),
        }


def asdict_list(items: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def development_cli(
    *,
    description: str,
    prompt: str,
    default_factory: Callable[[], Any],
    session_factory: Callable[..., DevelopmentSession[Any]],
    video_default: bool = False,
) -> None:
    """Both workbenches share pause/step/run/reset and pacing semantics."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="只校验 JSON，不创建运行或输出")
    parser.add_argument("--run", action="store_true", help="连续运行至有限终点")
    parser.add_argument("--tick-hz", type=float, default=10)
    parser.add_argument("--algorithm", help="受信任的 Python module:factory；JSON 不加载代码")
    parser.add_argument(
        "--video",
        action=argparse.BooleanOptionalAction,
        default=video_default,
        help="启停主辅图像生产；所有模块仍随系统初始化",
    )
    args = parser.parse_args()
    spec = load_scenario(args.scenario)
    validate_supported_features(spec)
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
    factory: Any = default_factory
    if args.algorithm:
        module, sep, name = args.algorithm.partition(":")
        if not sep:
            parser.error("--algorithm must be module:factory")
        factory = getattr(importlib.import_module(module), name)
    session = session_factory(
        args.scenario, args.output, algorithm=factory(), video_enabled=args.video
    )
    try:
        if not args.run:
            print("已暂停。命令：step / run [步数] / video on|off / status / reset / quit")
        while True:
            command = "run" if args.run else input(f"{prompt}> ").strip()
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
                session = session_factory(
                    args.scenario, path, algorithm=factory(), video_enabled=args.video
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


def main() -> None:
    development_cli(
        description="干净射线开发工具；默认无视频、暂停，无服务端口",
        prompt="center",
        default_factory=InputInspector,
        session_factory=CenterSession,
    )


if __name__ == "__main__":
    main()
