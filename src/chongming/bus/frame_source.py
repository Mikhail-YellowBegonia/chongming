"""FrameSource abstraction and virtual V-Switch video packet decoder."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from chongming.bus.client import OnlineBusClient
from chongming.bus.codec import camera_role_from_proto, capture_time_from_proto
from chongming.contracts import model as m
from chongming_proto.online import common_pb2, online_pb2

_RAW_PIXEL_FORMATS: dict[common_pb2.PixelFormat, tuple[m.PixelFormat, int]] = {
    common_pb2.PIXEL_FORMAT_GRAY8: (m.PixelFormat.GRAY8, 1),
    common_pb2.PIXEL_FORMAT_RGB8: (m.PixelFormat.RGB8, 3),
    common_pb2.PIXEL_FORMAT_BGR8: (m.PixelFormat.BGR8, 3),
}


@dataclass(frozen=True, kw_only=True)
class DecodedFrame:
    """One decoded camera frame paired with decode latency and decoded-pixel SHA-256."""

    frame: m.CameraFrame
    frame_index: int
    decode_duration_ns: int
    decoded_pixel_sha256: str


class FrameSource(Protocol):
    """Unified camera frame stream abstraction for virtual bus and hardware sources."""

    def next_frame(self) -> DecodedFrame | None:
        """Return the next decoded frame, or None when the stream ends."""
        ...

    def close(self) -> None:
        """Release underlying decoder or hardware resources."""
        ...


def _ffmpeg_executable() -> str:
    configured = os.environ.get("CHONGMING_FFMPEG")
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "FFmpeg executable not found; set CHONGMING_FFMPEG or enter the Nix environment"
    )


def split_annex_b_nals(payload: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into individual NAL units without start codes."""
    nals: list[bytes] = []
    length = len(payload)
    pos = 0
    current_start: int | None = None
    while pos < length:
        idx = payload.find(b"\x00\x00\x01", pos)
        if idx < 0:
            break
        code_start = idx - 1 if idx > 0 and payload[idx - 1] == 0 else idx
        if current_start is not None:
            nal = payload[current_start:code_start]
            if nal:
                nals.append(nal)
        current_start = idx + 3
        pos = current_start
    if current_start is not None:
        tail = payload[current_start:]
        if tail:
            nals.append(tail)
    return nals


class H264StreamDecoder:
    """GOP-aware Annex-B H.264 decoder yielding RGB8 frames without lock-step lookahead."""

    def __init__(self, *, width_px: int, height_px: int, ffmpeg_bin: str | None = None) -> None:
        if width_px <= 0 or height_px <= 0:
            raise ValueError("H.264 stream dimensions must be positive")
        self.width_px = width_px
        self.height_px = height_px
        self._frame_bytes = width_px * height_px * 3
        self._ffmpeg = ffmpeg_bin
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._gop_buf = bytearray()
        self._gop_frame_count = 0

    def decode(self, payload: bytes, *, is_keyframe: bool) -> bytes:
        """Decode one Annex-B access unit into tightly packed RGB8 bytes."""
        nals = split_annex_b_nals(payload)
        if not nals:
            raise ValueError("H.264 FramedVideoPacket contains no Annex-B NAL units")
        saw_idr = False
        saw_param_update = False
        picture_nals: list[bytes] = []
        for nal in nals:
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                self._sps = b"\x00\x00\x00\x01" + nal
                saw_param_update = True
            elif nal_type == 8:
                self._pps = b"\x00\x00\x00\x01" + nal
                saw_param_update = True
            elif nal_type == 9:
                continue
            else:
                if nal_type == 5:
                    saw_idr = True
                picture_nals.append(b"\x00\x00\x00\x01" + nal)
        if not picture_nals:
            raise ValueError("H.264 FramedVideoPacket contains no video slice NAL units")
        if self._sps is None or self._pps is None:
            raise ValueError("H.264 stream missing SPS/PPS before coded picture")
        if saw_idr or is_keyframe or not self._gop_buf:
            self._gop_buf = bytearray(self._sps + self._pps)
            self._gop_frame_count = 0
        elif saw_param_update:
            # Refresh stream parameter header while keeping earlier GOP slices intact.
            self._gop_buf.extend(self._sps)
            self._gop_buf.extend(self._pps)
        for nal_with_start_code in picture_nals:
            self._gop_buf.extend(nal_with_start_code)

        target_index = self._gop_frame_count
        self._gop_frame_count += 1

        ffmpeg = self._ffmpeg or _ffmpeg_executable()
        self._ffmpeg = ffmpeg
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-analyzeduration",
            "0",
            "-framerate",
            "30",
            "-f",
            "h264",
            "-i",
            "pipe:0",
        ]
        if target_index > 0:
            cmd.extend(["-vf", f"select=eq(n\\,{target_index})", "-fps_mode", "passthrough"])
        cmd.extend(["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])

        result = subprocess.run(
            cmd,
            input=bytes(self._gop_buf),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg H.264 decode failed: {detail}")
        decoded = result.stdout
        if len(decoded) != self._frame_bytes:
            raise RuntimeError(f"FFmpeg decoded {len(decoded)} bytes, expected {self._frame_bytes}")
        return decoded


def decode_video_packet(
    packet: online_pb2.FramedVideoPacket,
    h264_decoders: dict[tuple[str, m.CameraRole], H264StreamDecoder],
    *,
    ffmpeg_bin: str | None = None,
) -> DecodedFrame:
    """Decode one FramedVideoPacket (RAW or Annex-B H264) into a DecodedFrame."""
    if not packet.station_id or not packet.station_session_id or not packet.frame_id:
        raise ValueError("FramedVideoPacket requires station_id, station_session_id, and frame_id")
    if not packet.station_config_digest:
        raise ValueError("FramedVideoPacket requires station_config_digest")
    if packet.width_px <= 0 or packet.height_px <= 0:
        raise ValueError("FramedVideoPacket dimensions must be positive")
    role = camera_role_from_proto(packet.camera_role)
    capture_time = capture_time_from_proto(packet.capture_time)
    width = int(packet.width_px)
    height = int(packet.height_px)

    if packet.codec == common_pb2.VIDEO_CODEC_RAW:
        fmt_entry = _RAW_PIXEL_FORMATS.get(packet.pixel_format)
        if fmt_entry is None:
            raise ValueError(f"unsupported RAW pixel format: {packet.pixel_format}")
        pixel_format, channels = fmt_entry
        t0 = time.perf_counter_ns()
        stride = int(packet.row_stride_bytes)
        min_stride = width * channels
        if stride < min_stride:
            raise ValueError(f"RAW row_stride_bytes {stride} smaller than {min_stride}")
        expected_bytes = stride * height
        if len(packet.payload) != expected_bytes:
            raise ValueError(f"RAW payload size {len(packet.payload)} != expected {expected_bytes}")
        pixels = bytes(packet.payload)
        decode_ns = time.perf_counter_ns() - t0
    elif packet.codec == common_pb2.VIDEO_CODEC_H264:
        key = (packet.station_id, role)
        decoder = h264_decoders.get(key)
        if decoder is None:
            decoder = H264StreamDecoder(width_px=width, height_px=height, ffmpeg_bin=ffmpeg_bin)
            h264_decoders[key] = decoder
        elif (decoder.width_px, decoder.height_px) != (width, height):
            raise ValueError("H.264 stream dimensions changed mid-session")
        t0 = time.perf_counter_ns()
        pixels = decoder.decode(packet.payload, is_keyframe=bool(packet.is_keyframe))
        decode_ns = time.perf_counter_ns() - t0
        pixel_format = m.PixelFormat.RGB8
        stride = width * 3
    else:
        raise ValueError(f"unsupported FramedVideoPacket codec: {packet.codec}")

    digest = hashlib.sha256(pixels).hexdigest()
    frame = m.CameraFrame(
        station_id=m.StationId(packet.station_id),
        station_session_id=m.SessionId(packet.station_session_id),
        camera_role=role,
        frame_id=m.FrameId(packet.frame_id),
        capture_time=capture_time,
        image=m.ImagePlane(
            width_px=width,
            height_px=height,
            pixel_format=pixel_format,
            row_stride_bytes=stride,
            pixels=pixels,
        ),
        camera_config_ref=m.ConfigDigest(packet.station_config_digest),
    )
    return DecodedFrame(
        frame=frame,
        frame_index=int(packet.frame_index),
        decode_duration_ns=max(0, int(decode_ns)),
        decoded_pixel_sha256=digest,
    )


class VirtualBusFrameSource:
    """FrameSource implementation consuming FramedVideoPacket from an OnlineBusClient."""

    def __init__(
        self,
        client: OnlineBusClient,
        *,
        on_station_config: Callable[[online_pb2.StationConfig], None] | None = None,
        ffmpeg_bin: str | None = None,
    ) -> None:
        self._client = client
        self._on_station_config = on_station_config
        self._ffmpeg_bin = ffmpeg_bin
        self._h264_decoders: dict[tuple[str, m.CameraRole], H264StreamDecoder] = {}
        self.station_configs: dict[str, online_pb2.StationConfig] = {}

    def next_frame(self) -> DecodedFrame | None:
        while True:
            msg = self._client.receive()
            if msg is None:
                return None
            kind = msg.WhichOneof("payload")
            if kind == "station_config":
                cfg = msg.station_config
                self.station_configs[cfg.station_id] = cfg
                if self._on_station_config is not None:
                    self._on_station_config(cfg)
                continue
            if kind == "video_packet":
                return decode_video_packet(
                    msg.video_packet,
                    self._h264_decoders,
                    ffmpeg_bin=self._ffmpeg_bin,
                )
            if kind == "progress_report" and msg.progress_report.finite_end_reached:
                return None
            raise ValueError(f"unexpected BusMessage payload on Edge input stream: {kind!r}")

    def close(self) -> None:
        self._h264_decoders.clear()
