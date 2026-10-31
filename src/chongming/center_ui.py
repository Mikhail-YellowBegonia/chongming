"""Shared synchronous visualization for device, center and integrated runs."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from chongming.contracts.development import CenterAlgorithmResult
from chongming.contracts.model import (
    CalibrationHealth,
    CalibrationSnapshot,
    CalibrationSolution,
    CalibrationSolutionRef,
    CalibrationState,
    CalibrationUsage,
    CameraFrame,
    CameraRole,
    CaptureTime,
    DiagnosticEvent,
    RayEvent,
    StationId,
)
from chongming.contracts.scene import CameraFrameTruth, LogicalSceneSnapshot, ScenarioStep
from chongming.contracts.visualization import AlgorithmOutputUpdate, TruthComparisonSnapshot
from chongming.edge_runtime import EdgeStepOutput
from chongming.preview import GodotUiChannel, _preview_metadata, _UiLogBuffer
from chongming.reference import ReferenceFusionAlgorithm
from chongming.rendering.godot import GodotRenderer
from chongming.visualization import (
    center_fusion_event,
    edge_station_event,
    station_visualization_state,
    truth_snapshots,
)

if TYPE_CHECKING:
    from chongming.development import CenterSession

logger = logging.getLogger(__name__)


class CenterRunUi:
    """One run, one worker, and one acknowledged increment per algorithm call."""

    def __init__(self, session: CenterSession, renderer: GodotRenderer) -> None:
        self.session = session
        self.renderer = renderer
        if isinstance(session.algorithm, ReferenceFusionAlgorithm):
            session.algorithm.voxel_diagnostics.enabled = True
        self.channel = GodotUiChannel(renderer.ui_exchange)
        self.running = True
        self._station = str(session.spec.station_configs[0].station_id)
        self._freeview = True
        self._scene: LogicalSceneSnapshot | None = None
        self._display_step: ScenarioStep | None = None
        self._payload: dict[str, Any] | None = None
        self._frames: dict[tuple[StationId, CameraRole], CameraFrame] = {}
        self._frame_truth: dict[tuple[StationId, CameraRole], CameraFrameTruth] = {}
        self._states: dict[StationId, CalibrationState] = {}
        self._solutions: dict[CalibrationSolutionRef, CalibrationSolution] = {}
        self._captures: dict[StationId, CaptureTime] = {}
        self._update_id = 0
        self._log = _UiLogBuffer()

    def publish(
        self,
        step: ScenarioStep | None,
        result: CenterAlgorithmResult | None,
        *,
        edge_output: EdgeStepOutput | None = None,
    ) -> None:
        """Adapt only after the raw result is persisted, before any next source step."""
        clock = self.session.source.clock.read()
        self._scene = step.truth.logical_scene if step else self.session.source.logic.sample(clock)
        if step is not None:
            self._states.update(
                (state.station_id, state) for state in step.packets.calibration_states
            )
            self._solutions.update(
                (solution.solution_ref, solution) for solution in step.packets.calibration_solutions
            )
            if edge_output is None:
                for truth in step.truth.camera_frames:
                    if truth.camera_role is CameraRole.MAIN:
                        self._captures[truth.station_id] = truth.capture_time
            else:
                for completed in edge_output.frames:
                    self._captures[completed.detections.main_frame_ref.station_id] = (
                        completed.detections.capture_time
                    )
            if not self.session.video_enabled:
                self._frames.clear()
                self._frame_truth.clear()
            for frame in step.video.frames if self.session.video_enabled else ():
                key = (frame.station_id, frame.camera_role)
                self._frames[key] = frame
                self._frame_truth[key] = next(
                    truth for truth in step.truth.camera_frames if truth.frame_id == frame.frame_id
                )
            # Only the display retains latest per-camera pixels and their matching truth.
            self._display_step = replace(
                step,
                video=replace(step.video, frames=tuple(self._frames.values())),
                truth=replace(step.truth, camera_frames=tuple(self._frame_truth.values())),
            )
            self._payload = _preview_metadata(self._display_step, self._update_id + 1, 1)
            if edge_output is not None:
                self._payload["packets"] = {
                    "calibration_event_count": len(edge_output.calibrations),
                    "ray_count": sum(isinstance(event, RayEvent) for event in edge_output.events),
                    "diagnostic_count": sum(
                        isinstance(event, DiagnosticEvent) for event in edge_output.events
                    ),
                    "rays": [
                        asdict(event) for event in edge_output.events if isinstance(event, RayEvent)
                    ],
                }
        if edge_output is not None:
            for control_update in edge_output.calibrations:
                for diagnostic in control_update.diagnostics:
                    if diagnostic.capture_time is None:
                        observed = control_update.state_event.state.observed_at
                        logger.info(
                            "Control diagnostic station=%s observed_at=%s:%d (not an exposure) %s: %s",
                            diagnostic.envelope.station_id,
                            observed.clock_domain_id,
                            observed.time_ns,
                            diagnostic.category,
                            ", ".join(diagnostic.reason_codes),
                        )
        stations = []
        for config in self.session.spec.station_configs:
            state = self._states.get(config.station_id)
            capture = self._captures.get(config.station_id)
            calibration = None
            if self.session.edge is not None and capture is not None:
                calibration = self.session.edge.stations[config.station_id].history.snapshot_at(
                    capture
                )
            elif state is not None and capture is not None:
                calibration = CalibrationSnapshot(
                    main_capture_time=capture,
                    state=state,
                    solution=self._solutions[state.solution_ref] if state.solution_ref else None,
                    usage={
                        CalibrationHealth.VALID: CalibrationUsage.VALID,
                        CalibrationHealth.FROZEN: CalibrationUsage.FROZEN,
                        CalibrationHealth.STALE: CalibrationUsage.STALE,
                    }.get(state.health),
                    reason_codes=state.reason_codes,
                )
            stations.append(
                station_visualization_state(
                    config,
                    actual_camera=next(
                        (
                            camera
                            for camera in self._scene.cameras
                            if camera.station_id == config.station_id
                            and camera.camera_role is CameraRole.MAIN
                        ),
                        None,
                    ),
                    calibration=calibration,
                )
            )
        self._update_id += 1
        update = AlgorithmOutputUpdate(
            update_id=self._update_id,
            scenario_time_ns=clock.time_ns,
            step_no=clock.step_no,
            edge_events=tuple(
                edge_station_event(
                    station_id=station.config.station_id,
                    station_session_id=station.device.station_session_id,
                    events=tuple(
                        event
                        for event in edge_output.events
                        if event.envelope.station_id == station.config.station_id
                        and not (isinstance(event, DiagnosticEvent) and event.capture_time is None)
                    ),
                )
                for station in self.session.assembly.stations
            )
            if edge_output is not None
            else None,
            center_events=center_fusion_event(
                result,
                voxel_space=self.session.algorithm.voxel_diagnostics.snapshot
                if isinstance(self.session.algorithm, ReferenceFusionAlgorithm)
                else None,
            )
            if result is not None
            else None,
            station_states=tuple(stations),
        )
        snapshots = truth_snapshots(
            update,
            scene_logic=self.session.source.logic,
            clock_domain_id=clock.clock_domain_id,
        )
        self._push_freeview()
        self.tick(algorithm_output=update, snapshots=snapshots)

    def _push_freeview(self) -> None:
        if self._scene is not None:
            self._freeview = self.renderer.editor_update(
                self._scene, self._scene.cameras[0], enabled=self._freeview, realtime=False
            )

    def tick(
        self,
        *,
        algorithm_output: AlgorithmOutputUpdate | None = None,
        snapshots: tuple[TruthComparisonSnapshot, ...] | None = None,
    ) -> None:
        spec = self.session.spec
        last = self.session.last
        status = {
            "state": self.session.status,
            "error": self.session.error,
            "last": {
                "center_ms": last["center_ms"],
                "measurement_count": len(last["measurements"]),
                "track_event_count": len(last["tracks"]),
                "edge_ms": last["edge_ms"],
                "edge_frame_count": last["edge_frame_count"],
                "edge_diagnostic_count": last["edge_diagnostic_count"],
            }
            if last is not None
            else None,
            "stations": [str(config.station_id) for config in spec.station_configs],
            "time_s": (self._scene.sample_time.time_ns - spec.timeline.start_time_ns) / 1e9
            if self._scene is not None
            else None,
            "duration_s": (spec.timeline.end_time_ns - spec.timeline.start_time_ns) / 1e9,
            "logical_step_count": self.session.step_count,
            "profile": spec.scenario_id,
        }
        for command in self.channel.push(
            status=status,
            payload=self._payload,
            step=self._display_step,
            station_id=self._station,
            editor={"active": False, "freeview": self._freeview},
            log=self._log.snapshot(),
            execution_mode=self.session.mode,
            algorithm_output=algorithm_output,
            truth_snapshots=snapshots,
        ):
            self._dispatch(command)

    def _dispatch(self, command: dict[str, Any]) -> None:
        kind = command["type"]
        try:
            if kind == "quit":
                self.running = False
            elif kind == "select_station":
                station = str(command["station"])
                if station not in {
                    str(config.station_id) for config in self.session.spec.station_configs
                }:
                    raise ValueError(f"unknown station: {station}")
                self._station = station
            elif kind == "freeview":
                if not isinstance(command.get("enabled"), bool):
                    raise TypeError("freeview requires a boolean enabled field")
                self._freeview = command["enabled"]
                self._push_freeview()
            else:
                raise ValueError(f"command is unavailable in mode {self.session.mode}: {kind}")
        except (ValueError, TypeError, KeyError):
            logger.warning("UI command %s failed", kind, exc_info=True)

    def run(self, tick_hz: float) -> None:
        if not math.isfinite(tick_hz) or not 0 < tick_hz <= 240:
            raise ValueError("tick_hz must be in (0, 240]")
        package = logging.getLogger("chongming")
        previous_level = package.level
        package.addHandler(self._log)
        package.setLevel(logging.INFO)
        self.session.status = "running"
        try:
            self.tick()
            while self.running and not self.session.ended:
                began = time.monotonic()
                self.session.step()
                deadline = began + 1 / tick_hz
                while self.running and not self.session.ended:
                    delay = deadline - time.monotonic()
                    if delay <= 0:
                        break
                    time.sleep(min(0.05, delay))
                    self.tick()
        finally:
            package.removeHandler(self._log)
            package.setLevel(previous_level)
