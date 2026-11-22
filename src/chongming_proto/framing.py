"""Length-prefixed Protobuf wire framing and .pb.zst compressed stream I/O."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Self, cast

import zstandard
from google.protobuf.message import DecodeError, Message

MAX_FRAME_BYTES: int = 64 * 1024 * 1024
_LEN_STRUCT = struct.Struct(">I")


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    """Read exactly `size` bytes, looping over short reads from compressed streams."""
    if size < 0:
        raise ValueError(f"size must be non-negative, got {size}")
    if size == 0:
        return b""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_length_prefixed(message: Message, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    """Encode a Protobuf message with a 4-byte big-endian unsigned length prefix."""
    if max_bytes <= 0 or max_bytes > MAX_FRAME_BYTES:
        raise ValueError(f"max_bytes must be in (0, {MAX_FRAME_BYTES}], got {max_bytes}")
    payload = message.SerializeToString()
    if len(payload) > max_bytes:
        raise ValueError(f"encoded message size {len(payload)} exceeds max_bytes limit {max_bytes}")
    return _LEN_STRUCT.pack(len(payload)) + payload


def read_length_prefixed[T: Message](
    stream: IO[bytes],
    message_cls: type[T],
    *,
    max_bytes: int = MAX_FRAME_BYTES,
) -> T | None:
    """Read one length-prefixed Protobuf message; return None on clean EOF."""
    if max_bytes <= 0 or max_bytes > MAX_FRAME_BYTES:
        raise ValueError(f"max_bytes must be in (0, {MAX_FRAME_BYTES}], got {max_bytes}")
    header = _read_exact(stream, _LEN_STRUCT.size)
    if not header:
        return None
    if len(header) != _LEN_STRUCT.size:
        raise EOFError("truncated frame length header")
    (length,) = _LEN_STRUCT.unpack(header)
    if length > max_bytes:
        raise ValueError(f"frame payload size {length} exceeds max_bytes limit {max_bytes}")
    payload = _read_exact(stream, length)
    if len(payload) != length:
        raise EOFError(f"truncated frame payload: expected {length} bytes, got {len(payload)}")
    message = message_cls()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError(f"invalid {message_cls.__name__} protobuf payload: {exc}") from exc
    return message


class PbZstWriter:
    """Stream writer for concatenated length-prefixed Protobuf messages in .pb.zst frames."""

    def __init__(
        self,
        path: Path,
        *,
        level: int = 3,
        flush_every: int = 256,
        overwrite: bool = False,
        max_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        if flush_every < 1:
            raise ValueError("flush_every must be at least 1")
        if max_bytes <= 0 or max_bytes > MAX_FRAME_BYTES:
            raise ValueError(f"max_bytes must be in (0, {MAX_FRAME_BYTES}], got {max_bytes}")
        self.path = path
        self.level = level
        self.flush_every = flush_every
        self.overwrite = overwrite
        self.max_bytes = max_bytes
        self._file: IO[bytes] | None = None
        self._compressor = zstandard.ZstdCompressor(level=level)
        self._writer: zstandard.ZstdCompressionWriter | None = None
        self._pending_in_frame = 0
        self.count = 0

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb") if self.overwrite else self.path.open("xb")
        return self

    def _ensure_frame(self) -> zstandard.ZstdCompressionWriter:
        if self._file is None:
            raise RuntimeError("PbZstWriter must be entered as a context manager")
        if self._writer is None:
            self._writer = self._compressor.stream_writer(self._file, closefd=False)
            self._pending_in_frame = 0
        return self._writer

    def write(self, message: Message) -> None:
        frame = encode_length_prefixed(message, max_bytes=self.max_bytes)
        writer = self._ensure_frame()
        writer.write(frame)
        self.count += 1
        self._pending_in_frame += 1
        if self._pending_in_frame >= self.flush_every:
            self.flush_frame()

    def flush_frame(self) -> None:
        """Close the active zstd frame and flush bytes so earlier messages survive crashes."""
        if self._writer is not None:
            self._writer.flush(zstandard.FLUSH_FRAME)
            cast(BinaryIO, self._writer).close()
            self._writer = None
            self._pending_in_frame = 0
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        try:
            self.flush_frame()
        finally:
            if self._file is not None:
                self._file.close()
                self._file = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def iter_pb_zst[T: Message](
    path: Path,
    message_cls: type[T],
    *,
    max_bytes: int = MAX_FRAME_BYTES,
) -> Iterator[T]:
    """Stream-decode all length-prefixed Protobuf messages from a multi-frame .pb.zst file."""
    decompressor = zstandard.ZstdDecompressor()
    with (
        path.open("rb") as raw_handle,
        decompressor.stream_reader(raw_handle, read_across_frames=True) as stream,
    ):
        while True:
            message = read_length_prefixed(stream, message_cls, max_bytes=max_bytes)
            if message is None:
                return
            yield message
