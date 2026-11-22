"""Standalone Edge TCP worker process built on FrameSource and StationAssembly."""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from chongming.bus.client import OnlineBusClient
from chongming.bus.codec import (
    detection_batch_to_proto,
    progress_report_to_proto,
    station_config_from_proto,
    station_event_to_proto,
)
from chongming.bus.frame_source import DecodedFrame, FrameSource, VirtualBusFrameSource
from chongming.composition.graph import StationAssembly, declare_station
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.contracts.fusion import StationEventTimeProgress
from chongming.edge_runtime import validate_device_policy
from chongming.modules.device import WARMUP_HOLD_NS
from chongming.ports.lifecycle import ModuleBinding
from chongming.scenarios.json_content import parse_json
from chongming.scenarios.serialization import _decode, load_scenario
from chongming_proto.online import common_pb2, online_pb2


def load_device_policy_map(path: Path) -> dict[m.StationId, d.DevicePolicy]:
    """Load and validate a station-keyed DevicePolicy JSON file without scenario configs."""
    policies: dict[m.StationId, d.DevicePolicy] = _decode(
        parse_json(path.read_bytes()), dict[m.StationId, d.DevicePolicy]
    )
    if not policies:
        raise ValueError("device policy file must configure at least one station")
    for policy in policies.values():
        validate_device_policy(policy)
    return policies


class EdgeProcess:
    """Independent Edge process consuming FrameSource and emitting online BusMessages."""

    def __init__(
        self,
        client: OnlineBusClient,
        policies: Mapping[m.StationId, d.DevicePolicy],
        *,
        instance_id: str = "edge-1",
        session_id: str | None = None,
        station_ids: Sequence[str] | None = None,
        source: FrameSource | None = None,
        local_configs: Mapping[m.StationId, m.StationConfig] | None = None,
        warmup_hold_ns: int = WARMUP_HOLD_NS,
        ffmpeg_bin: str | None = None,
    ) -> None:
        if not policies:
            raise ValueError("EdgeProcess requires at least one station DevicePolicy")
        for policy in policies.values():
            validate_device_policy(policy)
        self.client = client
        self.policies = dict(policies)
        self.instance_id = instance_id
        self.session_id = session_id or f"edge-{uuid4().hex[:12]}"
        self.station_ids = (
            tuple(station_ids)
            if station_ids is not None
            else tuple(sorted(str(sid) for sid in self.policies))
        )
        self._local_configs = dict(local_configs or {})
        self._configs: dict[m.StationId, m.StationConfig] = {}
        self._stations: dict[m.StationId, StationAssembly] = {}
        self._bindings: list[ModuleBinding] = []
        self._warmup_hold_ns = warmup_hold_ns
        self._last_now_ns: dict[m.StationId, int] = {}
        self._last_clock: dict[m.StationId, str] = {}
        self._last_cal_seq: dict[m.StationId, int] = {}
        self._last_event_seq: dict[m.StationId, int] = {}
        self._last_capture: dict[m.StationId, m.CaptureTime] = {}
        self._source: FrameSource = (
            source
            if source is not None
            else VirtualBusFrameSource(
                client,
                on_station_config=self._register_station_config_proto,
                ffmpeg_bin=ffmpeg_bin,
            )
        )

    def _register_station_config_proto(self, pb_config: online_pb2.StationConfig) -> None:
        sid = m.StationId(pb_config.station_id)
        if sid not in self.policies:
            raise ValueError(f"received StationConfig for unconfigured station {sid!r}")
        local = self._local_configs.get(sid)
        anchor = (
            local.reference_anchor
            if local is not None and str(local.config_digest) == pb_config.config_digest
            else None
        )
        config = station_config_from_proto(pb_config, reference_anchor=anchor)
        existing = self._configs.get(sid)
        if existing is not None:
            if existing != config:
                raise ValueError(f"conflicting StationConfig received for station {sid!r}")
            return
        self._configs[sid] = config

    def _ensure_station(self, frame: m.CameraFrame, now: m.ProcessingTime) -> StationAssembly:
        sid = frame.station_id
        station = self._stations.get(sid)
        if station is not None:
            if station.device.station_session_id != frame.station_session_id:
                raise ValueError(
                    f"frame station_session_id {frame.station_session_id!r} changed mid-stream"
                )
            return station
        if sid not in self.policies:
            raise ValueError(f"received frame for unconfigured station {sid!r}")
        config = self._configs.get(sid) or self._local_configs.get(sid)
        if config is None:
            raise ValueError(f"missing StationConfig before frame for station {sid!r}")
        if config.config_digest != frame.camera_config_ref:
            raise ValueError(
                f"frame config digest {frame.camera_config_ref!r} != {config.config_digest!r}"
            )
        assembly, bindings = declare_station(
            config=config,
            policy=self.policies[sid],
            image_session_id=frame.station_session_id,
            warmup_hold_ns=self._warmup_hold_ns,
        )
        for binding in bindings:
            self._bindings.append(binding)
            binding.module.start_module(binding.node.module_key, now)
        self._stations[sid] = assembly
        return assembly

    def _send_station_event(self, event: m.StationEvent) -> None:
        sid = event.envelope.station_id
        seq = event.envelope.sequence_no
        if seq > self._last_event_seq.get(sid, 0):
            self._last_event_seq[sid] = seq
        self.client.send_payload(**station_event_to_proto(event))

    def _emit_calibration_update(self, update: m.CalibrationUpdate | None) -> None:
        if update is None:
            return
        envelope = update.state_event.envelope
        previous = self._last_cal_seq.get(envelope.station_id, 0)
        if envelope.sequence_no == previous:
            return
        if envelope.sequence_no < previous:
            raise ValueError("device calibration event sequence regressed")
        self._last_cal_seq[envelope.station_id] = envelope.sequence_no
        self._send_station_event(update.state_event)
        for diagnostic in update.diagnostics:
            self._send_station_event(diagnostic)

    def _process_decoded(self, decoded: DecodedFrame) -> None:
        frame = decoded.frame
        sid = frame.station_id
        midpoint = (frame.capture_time.exposure_start_ns + frame.capture_time.exposure_end_ns) // 2
        clock_id = frame.capture_time.clock_domain_id
        prev_clock = self._last_clock.get(sid)
        if prev_clock is not None and prev_clock != clock_id:
            raise ValueError(f"station {sid!r} capture clock domain changed")
        self._last_clock[sid] = clock_id
        now_ns = max(self._last_now_ns.get(sid, midpoint), midpoint)
        self._last_now_ns[sid] = now_ns
        now = m.ProcessingTime(clock_domain_id=clock_id, time_ns=now_ns)

        station = self._ensure_station(frame, now)
        self._emit_calibration_update(station.device.tick(now))

        if frame.camera_role is m.CameraRole.AUXILIARY:
            self._emit_calibration_update(station.device.process_auxiliary(frame, now))
            return

        t0 = time.perf_counter_ns()
        result = station.device.process_main(frame, now)
        cv_ns = max(0, time.perf_counter_ns() - t0)

        self.client.send_payload(
            detection_batch=detection_batch_to_proto(
                result.detections,
                decoded_pixel_sha256=decoded.decoded_pixel_sha256,
                decode_duration_ns=decoded.decode_duration_ns,
                cv_duration_ns=cv_ns,
            )
        )
        for event in result.events:
            self._send_station_event(event)
        self._last_capture[sid] = frame.capture_time
        self.client.send_payload(
            progress_report=progress_report_to_proto(
                result.progress,
                source_role=common_pb2.COMPONENT_ROLE_EDGE,
                completed_sequence_no=self._last_event_seq.get(sid, 0),
            )
        )

    def _emit_stream_end(self) -> None:
        for sid, station in sorted(self._stations.items()):
            clock_id = self._last_clock.get(sid, "capture")
            last_ns = self._last_now_ns.get(sid, 0)
            progress = StationEventTimeProgress(
                station_id=sid,
                station_session_id=station.device.station_session_id,
                station_config_digest=station.config.config_digest,
                completed_capture_time=self._last_capture.get(sid),
                received_at=m.ProcessingTime(clock_domain_id=clock_id, time_ns=last_ns),
                stream_ended=True,
            )
            self.client.send_payload(
                progress_report=progress_report_to_proto(
                    progress,
                    source_role=common_pb2.COMPONENT_ROLE_EDGE,
                    completed_sequence_no=self._last_event_seq.get(sid, 0),
                )
            )

    def run(self) -> online_pb2.SessionAck:
        """Complete handshake, process all frames from FrameSource, and emit final progress."""
        ack = self.client.handshake(
            role=common_pb2.COMPONENT_ROLE_EDGE,
            instance_id=self.instance_id,
            session_id=self.session_id,
            station_ids=self.station_ids,
        )
        try:
            while True:
                decoded = self._source.next_frame()
                if decoded is None:
                    break
                self._process_decoded(decoded)
            self._emit_stream_end()
        finally:
            self.close()
        return ack

    def close(self) -> None:
        """Stop started device modules and release FrameSource resources."""
        try:
            self._source.close()
        finally:
            for binding in reversed(self._bindings):
                sid = binding.node.station_id
                clock_id = self._last_clock.get(sid, "capture") if sid is not None else "capture"
                time_ns = self._last_now_ns.get(sid, 0) if sid is not None else 0
                binding.module.stop_module(
                    binding.node.module_key,
                    m.ProcessingTime(clock_domain_id=clock_id, time_ns=time_ns),
                )
            self._bindings.clear()


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point for standalone Edge TCP process."""
    parser = argparse.ArgumentParser(
        prog="chongming-edge",
        description="Run standalone Edge device pipeline connected to V-Switch over TCP.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--policy", required=True, type=Path, help="DevicePolicy JSON path")
    parser.add_argument("--instance-id", default="edge-1")
    parser.add_argument("--session-id")
    parser.add_argument("--station-id", dest="station_ids", action="append")
    parser.add_argument(
        "--scenario",
        type=Path,
        help="Optional scenario JSON supplying local StationConfig reference landmarks",
    )
    parser.add_argument("--warmup-hold-s", type=float, default=WARMUP_HOLD_NS / 1e9)
    parser.add_argument("--timeout-s", type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.warmup_hold_s < 0:
        raise ValueError("warmup-hold-s must be non-negative")
    policies = load_device_policy_map(args.policy)
    local_configs: dict[m.StationId, m.StationConfig] = {}
    if args.scenario is not None:
        spec = load_scenario(args.scenario)
        local_configs = {cfg.station_id: cfg for cfg in spec.station_configs}

    with OnlineBusClient(args.host, args.port, timeout_s=args.timeout_s) as client:
        process = EdgeProcess(
            client,
            policies,
            instance_id=args.instance_id,
            session_id=args.session_id,
            station_ids=args.station_ids,
            local_configs=local_configs,
            warmup_hold_ns=round(args.warmup_hold_s * 1e9),
        )
        process.run()


if __name__ == "__main__":
    main()
