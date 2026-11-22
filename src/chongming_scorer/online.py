"""Online V-Switch TCP subscriber for chongming_scorer over TestFrameworkMessage."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from chongming_proto.framing import MAX_FRAME_BYTES, encode_length_prefixed, read_length_prefixed
from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.scorer import (
    CenterIncrementalScorer,
    EdgeIncrementalScorer,
    IntegratedIncrementalScorer,
    SceneTruthIndex,
)
from chongming_scorer.storage import TestScenePackage, persist_scores_output

PROTOCOL_VERSION = "chongming.v1"


class ScorerHandshakeError(RuntimeError):
    """Raised when V-Switch rejects or fails to complete Scorer SessionHello/SessionAck."""


class OnlineScorerClient:
    """Synchronous TCP client speaking 4-byte BE length-prefixed TestFrameworkMessage."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float | None = 30.0,
        max_bytes: int = MAX_FRAME_BYTES,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not host:
            raise ValueError("host must be non-empty")
        if port <= 0 or port > 65535:
            raise ValueError(f"port must be in [1, 65535], got {port}")
        if max_bytes <= 0 or max_bytes > MAX_FRAME_BYTES:
            raise ValueError(f"max_bytes must be in (0, {MAX_FRAME_BYTES}], got {max_bytes}")
        self.host = host
        self.port = port
        self.max_bytes = max_bytes
        self._wall_clock_ns = wall_clock_ns
        self._sock: socket.socket | None = socket.create_connection((host, port), timeout=timeout_s)
        self._reader: BinaryIO | None = self._sock.makefile("rb")
        self._next_seq = 1
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def send_frame(self, message: truth_pb2.TestFrameworkMessage) -> None:
        """Send one TestFrameworkMessage over TCP."""
        if self._sock is None:
            raise RuntimeError("OnlineScorerClient is closed")
        self._sock.sendall(encode_length_prefixed(message, max_bytes=self.max_bytes))

    def receive(self) -> truth_pb2.TestFrameworkMessage | None:
        """Read the next TestFrameworkMessage from V-Switch; return None on clean EOF."""
        if self._reader is None:
            raise RuntimeError("OnlineScorerClient is closed")
        return read_length_prefixed(
            self._reader,
            truth_pb2.TestFrameworkMessage,
            max_bytes=self.max_bytes,
        )

    def send_score_event(self, event: truth_pb2.ScoreEvent) -> truth_pb2.TestFrameworkMessage:
        """Wrap and transmit one ScoreEvent back to V-Switch."""
        msg = truth_pb2.TestFrameworkMessage(
            sequence_no=self._next_seq,
            score_event=event,
        )
        self._next_seq += 1
        self.send_frame(msg)
        return msg

    def handshake(
        self,
        *,
        instance_id: str,
        session_id: str,
        station_ids: Sequence[str] = (),
        protocol_version: str = PROTOCOL_VERSION,
    ) -> online_pb2.SessionAck:
        """Send COMPONENT_ROLE_SCORER SessionHello wrapped in TestFrameworkMessage."""
        hello = truth_pb2.TestFrameworkMessage(
            sequence_no=self._next_seq,
            bus_message=online_pb2.BusMessage(
                bus_sequence_no=self._next_seq,
                sent_wall_time_ns=int(self._wall_clock_ns()),
                session_hello=online_pb2.SessionHello(
                    protocol_version=protocol_version,
                    role=common_pb2.COMPONENT_ROLE_SCORER,
                    instance_id=instance_id,
                    session_id=session_id,
                    station_ids=list(station_ids),
                ),
            ),
        )
        self._next_seq += 1
        self.send_frame(hello)

        reply = self.receive()
        if reply is None:
            raise ScorerHandshakeError("connection closed before receiving SessionAck")
        if reply.WhichOneof("payload") != "bus_message":
            raise ScorerHandshakeError(
                f"expected bus_message(session_ack), got {reply.WhichOneof('payload')!r}"
            )
        bus_msg = reply.bus_message
        if bus_msg.WhichOneof("payload") != "session_ack":
            raise ScorerHandshakeError(
                f"expected SessionAck after SessionHello, got {bus_msg.WhichOneof('payload')!r}"
            )
        ack = bus_msg.session_ack
        if not ack.accepted:
            raise ScorerHandshakeError(f"SessionHello rejected: {ack.reason}")
        return ack

    def close(self) -> None:
        """Close the buffered reader and underlying TCP socket."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._reader is not None:
                self._reader.close()
        finally:
            self._reader = None
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None


class OnlineScorerSubscriber:
    """Stream ScoreEvents over TCP while reading static truth from an immutable Test Scene."""

    def __init__(
        self,
        client: OnlineScorerClient,
        scene: TestScenePackage,
        *,
        mode: str = "integrated",
        output_dir: Path | None = None,
        instance_id: str = "scorer-1",
        session_id: str = "scorer-session-1",
        station_ids: Sequence[str] = (),
        gate_px: float = 10.0,
        size_k_px: float | None = 0.5,
        gate_m: float = 10.0,
        size_k_m: float | None = None,
        miss_k: float = 3.0,
        warmup_s: float | None = None,
        warmup_origin_ns: int | None = None,
        minimum_stations: int = 2,
    ) -> None:
        if mode not in {"edge", "center", "integrated"}:
            raise ValueError(f"unsupported score mode: {mode!r}")
        if output_dir is not None and output_dir.resolve() == scene.root.resolve():
            raise ValueError(
                "online scorer --output must never point into the read-only Test Scene"
            )
        self.client = client
        self.scene = scene
        self.mode = mode
        self.output_dir = output_dir
        self.instance_id = instance_id
        self.session_id = session_id
        self.station_ids = tuple(station_ids)
        self.gate_px = gate_px
        self.size_k_px = size_k_px
        self.gate_m = gate_m
        self.size_k_m = size_k_m
        self.miss_k = miss_k
        self.warmup_s = warmup_s
        self.warmup_origin_ns = warmup_origin_ns
        self.minimum_stations = minimum_stations
        self._scene_index = SceneTruthIndex.from_scene(scene)

    def _build_scorer(
        self, run_id: str
    ) -> EdgeIncrementalScorer | CenterIncrementalScorer | IntegratedIncrementalScorer:
        if self.mode == "edge":
            edge_warmup = 15.0 if self.warmup_s is None else self.warmup_s
            return EdgeIncrementalScorer(
                self.scene,
                run_id=run_id,
                gate_px=self.gate_px,
                size_k=self.size_k_px,
                warmup_s=edge_warmup,
                scene_index=self._scene_index,
            )
        if self.mode == "center":
            center_warmup = 0.0 if self.warmup_s is None else self.warmup_s
            return CenterIncrementalScorer(
                self.scene,
                run_id=run_id,
                gate_m=self.gate_m,
                size_k=self.size_k_m,
                miss_k=self.miss_k,
                warmup_s=center_warmup,
                warmup_origin_ns=self.warmup_origin_ns,
                minimum_stations=self.minimum_stations,
                scene_index=self._scene_index,
                defer_schedule_evaluation=False,
            )
        integrated_warmup = 15.0 if self.warmup_s is None else self.warmup_s
        return IntegratedIncrementalScorer(
            self.scene,
            run_id=run_id,
            gate_px=self.gate_px,
            size_k_px=self.size_k_px,
            gate_m=self.gate_m,
            size_k_m=self.size_k_m,
            miss_k=self.miss_k,
            warmup_s=integrated_warmup,
            warmup_origin_ns=self.warmup_origin_ns,
            minimum_stations=self.minimum_stations,
            scene_index=self._scene_index,
            defer_center_schedule_evaluation=False,
        )

    def run(self) -> dict[str, Any]:
        """Handshake, score incoming TestFrameworkMessages until EOF, and optionally persist."""
        ack = self.client.handshake(
            instance_id=self.instance_id,
            session_id=self.session_id,
            station_ids=self.station_ids,
        )
        run_id = ack.run_id or self.session_id
        if not run_id:
            raise ValueError(
                "online scorer requires non-empty run_id from SessionAck or session_id"
            )
        scorer = self._build_scorer(run_id)

        while True:
            msg = self.client.receive()
            if msg is None:
                break
            kind = msg.WhichOneof("payload")
            if kind == "truth_frame":
                self._scene_index.register_truth_frame(msg.truth_frame)
            elif kind == "bus_message":
                for event in scorer.feed_bus_message(msg.bus_message):
                    self.client.send_score_event(event)
            elif kind == "score_event":
                raise ValueError("Scorer subscriber must not receive inbound ScoreEvent")
            else:
                raise ValueError(f"unexpected TestFrameworkMessage payload: {kind!r}")

        for event in scorer.flush_remaining():
            self.client.send_score_event(event)

        if isinstance(scorer, IntegratedIncrementalScorer):
            all_scores, report = scorer.finalize()
        else:
            all_scores, report, _ = scorer.finalize()

        if self.output_dir is not None:
            persist_scores_output(self.output_dir, all_scores, report)
        return report


def subscribe_and_score(
    *,
    host: str,
    port: int,
    scene_dir: Path,
    output_dir: Path | None = None,
    mode: str = "integrated",
    instance_id: str = "scorer-1",
    session_id: str = "scorer-session-1",
    station_ids: Sequence[str] = (),
    gate_px: float = 10.0,
    size_k_px: float | None = 0.5,
    gate_m: float = 10.0,
    size_k_m: float | None = None,
    miss_k: float = 3.0,
    warmup_s: float | None = None,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
    verify_hashes: bool = True,
    timeout_s: float | None = 30.0,
) -> dict[str, Any]:
    """Connect to V-Switch over TCP, stream live ScoreEvents, and write final report/scores."""
    scene = TestScenePackage.open(scene_dir, verify_hashes=verify_hashes)
    with OnlineScorerClient(host, port, timeout_s=timeout_s) as client:
        return OnlineScorerSubscriber(
            client,
            scene,
            mode=mode,
            output_dir=output_dir,
            instance_id=instance_id,
            session_id=session_id,
            station_ids=station_ids,
            gate_px=gate_px,
            size_k_px=size_k_px,
            gate_m=gate_m,
            size_k_m=size_k_m,
            miss_k=miss_k,
            warmup_s=warmup_s,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
        ).run()
