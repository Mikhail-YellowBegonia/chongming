"""Length-prefixed TCP bus client for online Edge and Center processes."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import BinaryIO, Self

from chongming_proto.online import (
    MAX_FRAME_BYTES,
    common_pb2,
    encode_length_prefixed,
    online_pb2,
    read_length_prefixed,
)

PROTOCOL_VERSION = "chongming.v1"


class BusHandshakeError(RuntimeError):
    """Raised when V-Switch rejects or fails to complete SessionHello/SessionAck."""


class OnlineBusClient:
    """Synchronous TCP client speaking 4-byte BE length-prefixed BusMessage."""

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

    def set_timeout(self, timeout_s: float | None) -> None:
        """Update socket timeout for subsequent blocking I/O operations."""
        if self._sock is None:
            raise RuntimeError("OnlineBusClient is closed")
        self._sock.settimeout(timeout_s)

    def send_payload(self, **payload: object) -> online_pb2.BusMessage:
        """Wrap one BusMessage payload field with sequence/timestamp and send it."""
        if len(payload) != 1:
            raise ValueError(f"expected exactly one BusMessage payload field, got {list(payload)}")
        msg = online_pb2.BusMessage(
            bus_sequence_no=self._next_seq,
            sent_wall_time_ns=int(self._wall_clock_ns()),
            **payload,  # type: ignore[arg-type]
        )
        self._next_seq += 1
        self.send_raw(msg)
        return msg

    def send_raw(self, message: online_pb2.BusMessage) -> None:
        """Send an already-constructed BusMessage over TCP."""
        if self._sock is None:
            raise RuntimeError("OnlineBusClient is closed")
        frame = encode_length_prefixed(message, max_bytes=self.max_bytes)
        self._sock.sendall(frame)

    def receive(self) -> online_pb2.BusMessage | None:
        """Read the next BusMessage from the bus; return None on clean EOF."""
        if self._reader is None:
            raise RuntimeError("OnlineBusClient is closed")
        return read_length_prefixed(self._reader, online_pb2.BusMessage, max_bytes=self.max_bytes)

    def handshake(
        self,
        *,
        role: common_pb2.ComponentRole,
        instance_id: str,
        session_id: str,
        station_ids: Sequence[str] = (),
        protocol_version: str = PROTOCOL_VERSION,
    ) -> online_pb2.SessionAck:
        """Send SessionHello and verify SessionAck acceptance."""
        if role not in (
            common_pb2.COMPONENT_ROLE_EDGE,
            common_pb2.COMPONENT_ROLE_CENTER,
            common_pb2.COMPONENT_ROLE_PRODUCER,
            common_pb2.COMPONENT_ROLE_VIEWER,
        ):
            raise ValueError(f"invalid online component role for bus handshake: {role}")
        self.send_payload(
            session_hello=online_pb2.SessionHello(
                protocol_version=protocol_version,
                role=role,
                instance_id=instance_id,
                session_id=session_id,
                station_ids=list(station_ids),
            )
        )
        reply = self.receive()
        if reply is None:
            raise BusHandshakeError("connection closed before receiving SessionAck")
        if reply.WhichOneof("payload") != "session_ack":
            raise BusHandshakeError(
                f"expected SessionAck after SessionHello, got {reply.WhichOneof('payload')!r}"
            )
        ack = reply.session_ack
        if not ack.accepted:
            raise BusHandshakeError(f"SessionHello rejected: {ack.reason}")
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
