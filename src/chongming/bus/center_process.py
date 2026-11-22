"""Standalone online Center process communicating with V-Switch over TCP."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from chongming.bus.client import OnlineBusClient
from chongming.bus.codec import (
    calibration_state_event_from_proto,
    diagnostic_event_from_proto,
    fusion_measurement_to_proto,
    progress_report_from_proto,
    ray_event_from_proto,
    sparse_voxel_grid_to_proto,
    station_config_from_proto,
    track_event_to_proto,
)
from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.contracts.development import CenterInputStep, CenterRegistration
from chongming.reference import ReferenceFusionAlgorithm
from chongming.scenarios.json_content import parse_json
from chongming.scenarios.serialization import _decode
from chongming_proto.online import common_pb2, online_pb2


def load_fusion_policy(path: Path) -> f.FusionPolicy:
    """Load an explicit FusionPolicy JSON file."""
    data = parse_json(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"FusionPolicy JSON must be an object: {path}")
    policy = _decode(data, f.FusionPolicy)
    if not isinstance(policy, f.FusionPolicy):
        raise TypeError(f"decoded FusionPolicy has unexpected type: {type(policy).__name__}")
    return policy


def default_online_clock_mapping(policy: f.FusionPolicy) -> f.EventClockMappingSpec:
    """Build an identity clock mapping matching a FusionPolicy's capture and processing clocks."""
    return f.EventClockMappingSpec(
        capture_clock_domain_id=policy.windows.capture_clock_domain_id,
        processing_clock_domain_id=policy.windows.processing_clock_domain_id,
        offset_ns=0,
        scale_numerator=1,
        scale_denominator=1,
        mapping_uncertainty_ns=0,
    )


class CenterProcess:
    """Drive ReferenceFusionAlgorithm over a single V-Switch TCP BusMessage stream."""

    def __init__(
        self,
        client: OnlineBusClient,
        policy: f.FusionPolicy,
        *,
        clock_mapping: f.EventClockMappingSpec | None = None,
        instance_id: str = "center-1",
        session_id: str = "center-session-1",
        station_ids: Sequence[str] = (),
        postintersection: bool = False,
        voxel_diagnostics_enabled: bool = False,
        started_at_ns: int = 0,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if started_at_ns < 0:
            raise ValueError("started_at_ns must be non-negative")
        self.client = client
        self.policy = policy
        self.clock_mapping = (
            default_online_clock_mapping(policy) if clock_mapping is None else clock_mapping
        )
        self.instance_id = instance_id
        self.session_id = session_id
        self.station_ids = tuple(station_ids)
        self.voxel_diagnostics_enabled = voxel_diagnostics_enabled
        self._wall_clock_ns = wall_clock_ns
        self._capture_is_processing_clock = (
            self.clock_mapping.capture_clock_domain_id
            == self.clock_mapping.processing_clock_domain_id
        )
        self._processing_clock_id = self.policy.windows.processing_clock_domain_id
        self._last_now_ns = (
            started_at_ns
            if self._capture_is_processing_clock
            else max(started_at_ns, int(self._wall_clock_ns()))
        )
        self._step_no = 0
        self._last_completed_seq = 0
        self._last_sealed_ref_ns: int | None = None
        self._station_configs: dict[m.StationId, m.StationConfig] = {}
        self._stations_with_frames: set[m.StationId] = set()
        self._algorithm = ReferenceFusionAlgorithm(postintersection=postintersection)
        self._algorithm.voxel_diagnostics.enabled = voxel_diagnostics_enabled
        self._started = False

    def _next_now(self, event_time_ns: int | None = None) -> m.ProcessingTime:
        if self._capture_is_processing_clock:
            if event_time_ns is not None and event_time_ns > self._last_now_ns:
                self._last_now_ns = event_time_ns
        else:
            self._last_now_ns = max(self._last_now_ns, int(self._wall_clock_ns()))
        return m.ProcessingTime(
            clock_domain_id=self._processing_clock_id,
            time_ns=self._last_now_ns,
        )

    def _register_station(self, config: m.StationConfig) -> None:
        if self._algorithm.center is None:
            raise RuntimeError("CenterProcess algorithm is not started")
        result = self._algorithm.center.fusion.register_station(config)
        if result.status not in {
            f.RegistryWriteStatus.REGISTERED,
            f.RegistryWriteStatus.ALREADY_REGISTERED,
        }:
            raise ValueError(f"station registration failed: {result.reason_codes}")
        self._station_configs[config.station_id] = config

    def _emit_step(
        self,
        *,
        now: m.ProcessingTime,
        solutions: tuple[m.CalibrationSolution, ...] = (),
        events: tuple[m.ReceivedEvent, ...] = (),
        progress: tuple[f.StationEventTimeProgress, ...] = (),
        stream_ended: bool = False,
    ) -> None:
        step = CenterInputStep(
            step_no=self._step_no,
            time=now,
            solutions=solutions,
            events=events,
            progress=progress,
            stream_ended=stream_ended,
        )
        self._step_no += 1
        result = self._algorithm.process(step)

        for measurement in result.measurements:
            self.client.send_payload(fusion_measurement=fusion_measurement_to_proto(measurement))
        for track in result.tracks:
            self.client.send_payload(track_event=track_event_to_proto(track))

        diag_snapshot = self._algorithm.voxel_diagnostics.snapshot
        diag_window = self._algorithm.voxel_diagnostics.window
        if self.voxel_diagnostics_enabled and diag_snapshot is not None and diag_window is not None:
            self.client.send_payload(
                sparse_voxel_grid=sparse_voxel_grid_to_proto(
                    center_session_id=m.SessionId(self.session_id),
                    world_frame_id=self.policy.admission.world_frame_id,
                    window_start_ns=diag_window.start_ns,
                    window_end_ns=diag_window.end_ns,
                    snapshot=diag_snapshot,
                )
            )

        for batch in result.batches:
            for window in batch.window_update.sealed_windows:
                self._last_sealed_ref_ns = window.reference_time_ns
                self.client.send_payload(
                    progress_report=online_pb2.ProgressReport(
                        source_role=common_pb2.COMPONENT_ROLE_CENTER,
                        source_id=self.instance_id,
                        session_id=self.session_id,
                        completed_time_ns=int(window.reference_time_ns),
                        completed_sequence_no=int(self._last_completed_seq),
                        finite_end_reached=False,
                    )
                )

    def _handle_message(self, msg: online_pb2.BusMessage) -> None:
        self._last_completed_seq = max(self._last_completed_seq, int(msg.bus_sequence_no))
        kind = msg.WhichOneof("payload")
        if kind == "station_config":
            self._register_station(station_config_from_proto(msg.station_config))
            return

        if kind == "calibration_state":
            cal_event = calibration_state_event_from_proto(msg.calibration_state)
            event_time_ns = (
                cal_event.state.observed_at.time_ns
                if cal_event.state.observed_at.clock_domain_id == self._processing_clock_id
                else None
            )
            now = self._next_now(event_time_ns)
            solutions = () if cal_event.solution is None else (cal_event.solution,)
            self._emit_step(
                now=now,
                solutions=solutions,
                events=(m.ReceivedEvent(event=cal_event, arrival_time=now),),
            )
            return

        if kind == "ray_event":
            ray_event = ray_event_from_proto(msg.ray_event)
            self._stations_with_frames.add(ray_event.envelope.station_id)
            midpoint_ns = (
                ray_event.capture_time.exposure_start_ns + ray_event.capture_time.exposure_end_ns
            ) // 2
            now = self._next_now(midpoint_ns)
            self._emit_step(
                now=now,
                events=(m.ReceivedEvent(event=ray_event, arrival_time=now),),
            )
            return

        if kind == "diagnostic_event":
            diag_event = diagnostic_event_from_proto(msg.diagnostic_event)
            event_time_ns = (
                (
                    diag_event.capture_time.exposure_start_ns
                    + diag_event.capture_time.exposure_end_ns
                )
                // 2
                if diag_event.capture_time is not None
                else None
            )
            now = self._next_now(event_time_ns)
            self._emit_step(
                now=now,
                events=(m.ReceivedEvent(event=diag_event, arrival_time=now),),
            )
            return

        if kind == "progress_report":
            report = msg.progress_report
            station_id = m.StationId(report.source_id)
            cfg = self._station_configs.get(station_id)
            if cfg is None:
                raise ValueError(
                    f"ProgressReport received before StationConfig for station {report.source_id!r}"
                )
            has_completed_frame = (station_id in self._stations_with_frames) or (
                report.completed_time_ns != 0
            )
            if not report.finite_end_reached:
                self._stations_with_frames.add(station_id)
            now = self._next_now(int(report.completed_time_ns) if has_completed_frame else None)
            progress = progress_report_from_proto(
                report,
                station_config_digest=cfg.config_digest,
                capture_clock_domain_id=self.policy.windows.capture_clock_domain_id,
                received_at=now,
                has_completed_frame=has_completed_frame,
            )
            self._emit_step(
                now=now,
                progress=(progress,),
                stream_ended=bool(report.finite_end_reached),
            )
            return

        raise ValueError(f"unexpected BusMessage payload for CenterProcess: {kind!r}")

    def _emit_stream_end(self) -> None:
        self.client.send_payload(
            progress_report=online_pb2.ProgressReport(
                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                source_id=self.instance_id,
                session_id=self.session_id,
                completed_time_ns=0
                if self._last_sealed_ref_ns is None
                else int(self._last_sealed_ref_ns),
                completed_sequence_no=int(self._last_completed_seq),
                finite_end_reached=True,
            )
        )

    def run(self) -> online_pb2.SessionAck:
        """Complete handshake, consume incoming bus messages until EOF, and emit outputs."""
        ack = self.client.handshake(
            role=common_pb2.COMPONENT_ROLE_CENTER,
            instance_id=self.instance_id,
            session_id=self.session_id,
            station_ids=self.station_ids,
        )
        self._algorithm.start(
            CenterRegistration(
                stations=(),
                center_session_id=m.SessionId(self.session_id),
                policy=self.policy,
                clock_mapping=self.clock_mapping,
                started_at=m.ProcessingTime(
                    clock_domain_id=self._processing_clock_id,
                    time_ns=self._last_now_ns,
                ),
            )
        )
        self._started = True
        try:
            while True:
                msg = self.client.receive()
                if msg is None:
                    break
                self._handle_message(msg)
            self._emit_stream_end()
        finally:
            self.close()
        return ack

    def close(self) -> None:
        """Stop the underlying ReferenceFusionAlgorithm if it was started."""
        if not self._started:
            return
        self._started = False
        self._algorithm.close()


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point for standalone Center TCP process."""
    parser = argparse.ArgumentParser(
        prog="chongming-center-bus",
        description="Run standalone Center fusion algorithm connected to V-Switch over TCP.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--policy", required=True, type=Path, help="FusionPolicy JSON path")
    parser.add_argument("--instance-id", default="center-1")
    parser.add_argument("--session-id", default="center-session-1")
    parser.add_argument("--station-id", dest="station_ids", action="append", default=[])
    parser.add_argument(
        "--timeout-basis",
        choices=[basis.value for basis in f.StreamTimeoutBasis],
        default=None,
        help="Optional override for EventWindowPolicy.timeout_basis",
    )
    parser.add_argument("--postintersection", action="store_true")
    parser.add_argument("--voxel-diagnostics", action="store_true")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    args = parser.parse_args(argv)

    policy = load_fusion_policy(args.policy)
    if args.timeout_basis is not None:
        policy = replace(
            policy,
            windows=replace(policy.windows, timeout_basis=f.StreamTimeoutBasis(args.timeout_basis)),
        )

    with OnlineBusClient(args.host, args.port, timeout_s=args.timeout_s) as client:
        process = CenterProcess(
            client,
            policy,
            instance_id=args.instance_id,
            session_id=args.session_id,
            station_ids=args.station_ids,
            postintersection=args.postintersection,
            voxel_diagnostics_enabled=args.voxel_diagnostics,
        )
        process.run()


if __name__ == "__main__":
    main()
