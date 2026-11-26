"""Standalone macOS/OpenCV raw camera recorder with frame timestamps and timing audit."""

from __future__ import annotations

import argparse
import csv
import ipaddress
import itertools
import json
import math
import platform
import re
import signal
import socket
import statistics
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import cv2

REPORT_SCHEMA = "camera-recording-report-v1"
META_SCHEMA = "camera-recording-meta-v1"
TIMESTAMPS_COLUMNS = (
    "station_id",
    "output_frame_index",
    "capture_attempt_index",
    "timestamp_source",
    "clock_domain",
    "backend_pos_msec",
    "realtime_ns",
    "monotonic_ns",
    "realtime_minus_monotonic_ns",
    "monotonic_dt_ms",
    "gap_flag",
    "actual_width_px",
    "actual_height_px",
    "reported_fps",
)

_NTP_EPOCH_DELTA_S = 2208988800  # Seconds between 1900-01-01 and 1970-01-01.
_DEFAULT_NTP_SERVER = "time.apple.com"
_GO_THRESHOLD_MS = 33.0
_NEAR_BLACK_MAX = 16
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_fake_ip(ip_text: str | None) -> bool:
    if not ip_text:
        return False
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv4Address) and addr in _FAKE_IP_NET


class RecordingError(RuntimeError):
    """Actionable failure during camera recording or verification."""


class FrameCaptureSource(Protocol):
    """Minimal capture interface allowing unit testing without hardware camera."""

    def isOpened(self) -> bool: ...

    def set(self, prop_id: int, value: float) -> bool: ...

    def get(self, prop_id: int) -> float: ...

    def read(self) -> tuple[bool, Any]: ...

    def release(self) -> None: ...


@dataclass(frozen=True, kw_only=True)
class NtpSample:
    offset_ms: float
    rtt_ms: float
    stratum: int
    server_ip: str


@dataclass(frozen=True, kw_only=True)
class NtpCheckResult:
    available: bool
    server: str
    server_ip: str | None
    samples_requested: int
    samples_succeeded: int
    median_offset_ms: float | None
    min_rtt_ms: float | None
    half_rtt_uncertainty_ms: float | None
    offset_spread_ms: float | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "method": "read_only_udp_sntp_v4",
            "server": self.server,
            "server_ip": self.server_ip,
            "fake_ip_detected": _is_fake_ip(self.server_ip),
            "samples_requested": self.samples_requested,
            "samples_succeeded": self.samples_succeeded,
            "median_offset_ms": self.median_offset_ms,
            "min_rtt_ms": self.min_rtt_ms,
            "half_rtt_uncertainty_ms": self.half_rtt_uncertainty_ms,
            "offset_spread_ms": self.offset_spread_ms,
            "error": self.error,
        }


def _decode_fourcc(code: float) -> str:
    raw = round(code)
    if raw <= 0:
        return "unknown"
    chars = [chr((raw >> (8 * idx)) & 0xFF) for idx in range(4)]
    if all(32 <= ord(ch) < 127 for ch in chars):
        return "".join(chars)
    return f"0x{raw:08x}"


def _unix_ns_to_ntp_pair(unix_ns: int) -> tuple[int, int]:
    sec, rem_ns = divmod(unix_ns, 1_000_000_000)
    ntp_sec = sec + _NTP_EPOCH_DELTA_S
    ntp_frac = (rem_ns << 32) // 1_000_000_000
    return ntp_sec, ntp_frac


def _ntp_pair_to_unix_ns(ntp_sec: int, ntp_frac: int) -> int:
    unix_sec = ntp_sec - _NTP_EPOCH_DELTA_S
    rem_ns = (ntp_frac * 1_000_000_000) >> 32
    return unix_sec * 1_000_000_000 + rem_ns


def build_sntp_request(transmit_unix_ns: int) -> bytes:
    """Build a 48-byte NTP v4 client request packet (mode 3)."""
    sec, frac = _unix_ns_to_ntp_pair(transmit_unix_ns)
    packet = bytearray(48)
    packet[0] = (4 << 3) | 3  # LI=0, VN=4, Mode=3 (client)
    struct.pack_into("!II", packet, 40, sec & 0xFFFFFFFF, frac & 0xFFFFFFFF)
    return bytes(packet)


def parse_sntp_response(
    payload: bytes, t1_unix_ns: int, t4_unix_ns: int, server_ip: str
) -> NtpSample:
    if len(payload) < 48:
        raise ValueError(f"Short NTP response ({len(payload)} bytes)")
    first_byte, stratum = payload[0], payload[1]
    mode, version, leap = first_byte & 0x07, (first_byte >> 3) & 0x07, first_byte >> 6
    if mode != 4 or version not in {3, 4} or leap == 3 or not 1 <= stratum <= 15:
        raise ValueError("NTP reply is not from a synchronized server")
    expected_originate = build_sntp_request(t1_unix_ns)[40:48]
    if payload[24:32] != expected_originate:
        raise ValueError("NTP reply does not match the request timestamp")
    rx_sec, rx_frac, tx_sec, tx_frac = struct.unpack_from("!IIII", payload, 32)
    if rx_sec == 0 or tx_sec == 0:
        raise ValueError("NTP server returned zero timestamp")
    t2_ns = _ntp_pair_to_unix_ns(rx_sec, rx_frac)
    t3_ns = _ntp_pair_to_unix_ns(tx_sec, tx_frac)
    offset_ns = ((t2_ns - t1_unix_ns) + (t3_ns - t4_unix_ns)) / 2.0
    rtt_ns = (t4_unix_ns - t1_unix_ns) - (t3_ns - t2_ns)
    if t3_ns < t2_ns or rtt_ns < 0:
        raise ValueError("NTP reply has inconsistent timestamps")
    return NtpSample(
        offset_ms=offset_ns / 1_000_000.0,
        rtt_ms=rtt_ns / 1_000_000.0,
        stratum=stratum,
        server_ip=server_ip,
    )


def query_ntp_offset(
    server: str = _DEFAULT_NTP_SERVER,
    *,
    port: int = 123,
    samples: int = 5,
    timeout_s: float = 1.5,
) -> NtpCheckResult:
    """Query an NTP server read-only via UDP SNTP without changing the host clock."""
    if samples <= 0:
        raise ValueError("samples must be positive")
    try:
        addr_info = socket.getaddrinfo(server, port, type=socket.SOCK_DGRAM)
    except OSError as exc:
        return NtpCheckResult(
            available=False,
            server=server,
            server_ip=None,
            samples_requested=samples,
            samples_succeeded=0,
            median_offset_ms=None,
            min_rtt_ms=None,
            half_rtt_uncertainty_ms=None,
            offset_spread_ms=None,
            error=f"DNS resolution failed: {exc}",
        )

    family, socktype, proto, _, sockaddr = addr_info[0]
    server_ip = str(sockaddr[0])
    collected: list[NtpSample] = []
    last_error: str | None = None

    for _ in range(samples):
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout_s)
                sock.connect(sockaddr)
                t1_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                mono_start = time.monotonic_ns()
                sock.send(build_sntp_request(t1_ns))
                data = sock.recv(512)
                mono_end = time.monotonic_ns()
                t4_ns = t1_ns + (mono_end - mono_start)
            collected.append(parse_sntp_response(data, t1_ns, t4_ns, server_ip))
        except (OSError, ValueError) as exc:
            last_error = str(exc)

    if not collected:
        return NtpCheckResult(
            available=False,
            server=server,
            server_ip=server_ip,
            samples_requested=samples,
            samples_succeeded=0,
            median_offset_ms=None,
            min_rtt_ms=None,
            half_rtt_uncertainty_ms=None,
            offset_spread_ms=None,
            error=last_error or "No NTP responses received",
        )

    offsets = [s.offset_ms for s in collected]
    rtts = [s.rtt_ms for s in collected]
    min_rtt = min(rtts)
    return NtpCheckResult(
        available=True,
        server=server,
        server_ip=collected[0].server_ip,
        samples_requested=samples,
        samples_succeeded=len(collected),
        median_offset_ms=statistics.median(offsets),
        min_rtt_ms=min_rtt,
        half_rtt_uncertainty_ms=max(rtts) * 0.5,
        offset_spread_ms=max(offsets) - min(offsets),
    )


def _open_camera_capture(
    camera_index: int,
    *,
    width: int,
    height: int,
    fps: float,
    backend: str,
) -> cv2.VideoCapture:
    if backend == "avfoundation":
        api_pref = cv2.CAP_AVFOUNDATION
    elif backend == "any":
        api_pref = cv2.CAP_ANY
    else:
        raise ValueError(f"Unsupported backend: {backend!r}")

    cap = cv2.VideoCapture(camera_index, api_pref)
    if not cap.isOpened():
        cap.release()
        raise RecordingError(
            f"Failed to open camera index {camera_index} (backend={backend}). "
            "On macOS, check System Settings -> Privacy & Security -> Camera permission "
            "for your terminal/IDE, or try a different --camera index."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    cap.set(cv2.CAP_PROP_FPS, float(fps))
    return cap


def _open_video_writer(
    video_path: Path,
    *,
    preferred_codec: str,
    fps: float,
    width: int,
    height: int,
) -> tuple[cv2.VideoWriter, str]:
    candidates = [preferred_codec]
    for fallback in ("avc1", "mp4v", "MJPG"):
        if fallback not in candidates:
            candidates.append(fallback)
    for codec in candidates:
        fourcc = cv2.VideoWriter_fourcc(*codec)  # type: ignore[attr-defined]
        writer = cv2.VideoWriter(str(video_path), fourcc, float(fps), (width, height))
        if writer.isOpened():
            return writer, codec
        writer.release()
    raise RecordingError(f"Failed to open VideoWriter for {video_path} with codecs {candidates}.")


def _has_advancing_backend_timestamps(values_ms: list[float | None]) -> bool:
    finite = [v for v in values_ms if v is not None and math.isfinite(v) and v > 0.0]
    if len(finite) < max(2, int(0.9 * len(values_ms))):
        return False
    advancing = sum(1 for a, b in itertools.pairwise(finite) if b > a)
    return advancing >= max(1, int(0.85 * (len(finite) - 1)))


def _is_near_black(frame: Any) -> bool:
    return cv2.norm(frame, cv2.NORM_INF) <= _NEAR_BLACK_MAX


def _probe_video(video_path: Path) -> tuple[bool, int, tuple[int, int] | None, int, float | None]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        return False, 0, None, 0, None
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    count = 0
    near_black = 0
    size: tuple[int, int] | None = None
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if size is None:
            size = (int(frame.shape[1]), int(frame.shape[0]))
        near_black += int(_is_near_black(frame))
        count += 1
    cap.release()
    return True, count, size, near_black, fps if math.isfinite(fps) and fps > 0 else None


def analyze_recording(
    *,
    video_path: Path,
    timestamps_path: Path,
    meta: dict[str, Any] | None = None,
    ntp_result: NtpCheckResult | None = None,
    expected_fps: float = 30.0,
) -> dict[str, Any]:
    """Verify reopened video frames against timestamp rows and evaluate the 33 ms budget."""
    if not video_path.is_file():
        raise RecordingError(f"Video file not found: {video_path}")
    if not timestamps_path.is_file():
        raise RecordingError(f"Timestamps CSV not found: {timestamps_path}")

    with timestamps_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or any(
            col not in reader.fieldnames for col in TIMESTAMPS_COLUMNS
        ):
            raise RecordingError(f"Timestamps CSV missing expected columns: {timestamps_path}")
        rows = list(reader)

    playable, video_frame_count, video_size, decoded_near_black, playback_fps = _probe_video(
        video_path
    )
    row_count = len(rows)
    indices = [int(r["output_frame_index"]) for r in rows]
    contiguous_indices = indices == list(range(row_count))

    mono_ns = [int(r["monotonic_ns"]) for r in rows]
    real_ns = [int(r["realtime_ns"]) for r in rows]
    offsets_ns = [int(r["realtime_minus_monotonic_ns"]) for r in rows]
    backend_ms: list[float | None] = [
        float(r["backend_pos_msec"]) if r["backend_pos_msec"].strip() else None for r in rows
    ]
    sources = sorted({r["timestamp_source"] for r in rows})
    source_timestamps_available = _has_advancing_backend_timestamps(backend_ms)

    dt_ms_list = [(b - a) / 1_000_000.0 for a, b in itertools.pairwise(mono_ns)]
    expected_dt_ms = 1000.0 / expected_fps if expected_fps > 0 else 33.333
    gap_threshold_ms = 1.5 * expected_dt_ms
    gap_events = [
        {"frame_index": idx + 1, "dt_ms": dt}
        for idx, dt in enumerate(dt_ms_list)
        if dt > gap_threshold_ms
    ]

    clock_step_events: list[dict[str, Any]] = []
    if offsets_ns:
        base_offset = offsets_ns[0]
        for idx, off in enumerate(offsets_ns):
            drift_ms = (off - base_offset) / 1_000_000.0
            if abs(drift_ms) > 5.0:
                clock_step_events.append({"frame_index": idx, "offset_shift_ms": drift_ms})
        max_clock_shift_ms = max(abs(off - base_offset) for off in offsets_ns) / 1_000_000.0
    else:
        max_clock_shift_ms = 0.0

    if len(dt_ms_list) >= 1 and (mono_ns[-1] - mono_ns[0]) > 0:
        duration_s = (mono_ns[-1] - mono_ns[0]) / 1_000_000_000.0
        measured_fps = (row_count - 1) / duration_s
        median_dt_ms = statistics.median(dt_ms_list)
        max_dt_ms = max(dt_ms_list)
        jitter_std_ms = statistics.pstdev(dt_ms_list) if len(dt_ms_list) > 1 else 0.0
    else:
        duration_s = 0.0
        measured_fps = 0.0
        median_dt_ms = 0.0
        max_dt_ms = 0.0
        jitter_std_ms = 0.0

    # Budget evaluation against 33 ms at 30 fps.
    ntp_bound_ms: float | None = None
    if ntp_result is not None and ntp_result.available and not _is_fake_ip(ntp_result.server_ip):
        assert ntp_result.median_offset_ms is not None
        assert ntp_result.half_rtt_uncertainty_ms is not None
        ntp_bound_ms = abs(ntp_result.median_offset_ms) + ntp_result.half_rtt_uncertainty_ms

    read_failures = int(meta.get("read_failures", 0)) if meta else 0
    write_failures = int(meta.get("write_failures", 0)) if meta else 0
    input_near_black = meta.get("input_near_black_frames") if meta else None

    hard_failures: list[str] = []
    if not playable or video_frame_count == 0:
        hard_failures.append("Recorded video is not playable or contains 0 frames.")
    elif decoded_near_black == video_frame_count:
        hard_failures.append(
            "All decoded frames are nearly black. Check the selected camera and lens before recording again."
        )
    if video_frame_count != row_count:
        hard_failures.append(
            f"Video frame count ({video_frame_count}) != timestamp row count ({row_count})."
        )
    if not contiguous_indices:
        hard_failures.append("Timestamp output_frame_index sequence is not contiguous 0..N-1.")
    if ntp_bound_ms is not None and ntp_bound_ms > _GO_THRESHOLD_MS:
        hard_failures.append(
            f"Single-host NTP offset+uncertainty ({ntp_bound_ms:.2f} ms) exceeds {_GO_THRESHOLD_MS:.0f} ms budget."
        )
    if max_clock_shift_ms > _GO_THRESHOLD_MS:
        hard_failures.append(
            f"Host wall-clock jumped by {max_clock_shift_ms:.2f} ms during capture."
        )

    warnings: list[str] = []
    if (
        playback_fps is not None
        and duration_s > 0
        and abs(playback_fps - measured_fps) > (0.05 * measured_fps)
    ):
        warnings.append(
            f"MP4 playback rate ({playback_fps:.2f} fps) differs from measured capture cadence "
            f"({measured_fps:.2f} fps). Use the original timestamp CSV for replay timing."
        )
    if 0 < decoded_near_black < video_frame_count:
        warnings.append(f"Reopened video contains {decoded_near_black} nearly black frames.")
    if not source_timestamps_available:
        warnings.append(
            "Capture backend did not expose advancing hardware/source frame timestamps; "
            "timestamps are host receipt time after cv2.VideoCapture.read(). Capture/USB/ISP "
            "latency remains unresolved by host clocks alone."
        )
    if ntp_result is None or not ntp_result.available:
        warnings.append(
            "Network time offset check was not available; cross-host clock offset is unverified."
        )
    elif _is_fake_ip(ntp_result.server_ip):
        warnings.append(
            f"NTP server {ntp_result.server!r} resolved to {ntp_result.server_ip} "
            "(RFC 2544 / proxy Fake-IP). Measured offset/RTT reflects proxy forwarding, not real "
            "clock error; bypass TUN/Fake-IP or use a direct server such as --ntp-server ntp.aliyun.com."
        )
    if gap_events:
        warnings.append(
            f"Detected {len(gap_events)} capture interval gap(s) > {gap_threshold_ms:.1f} ms "
            f"(max dt {max_dt_ms:.2f} ms)."
        )
    if read_failures or write_failures:
        warnings.append(
            f"Encountered {read_failures} read failure(s) and {write_failures} write failure(s)."
        )

    if hard_failures:
        timing_status = "no_go"
    else:
        timing_status = "provisional"

    if timing_status == "provisional":
        warnings.append(
            "This single-camera audit cannot establish cross-camera timing. Compare both "
            "recordings using a shared physical event before applying the 33 ms limit."
        )

    return {
        "schema_version": REPORT_SCHEMA,
        "timing_status": timing_status,
        "go_threshold_ms": _GO_THRESHOLD_MS,
        "video": {
            "path": str(video_path),
            "playable": playable,
            "decoded_frame_count": video_frame_count,
            "decoded_size_px": list(video_size) if video_size else None,
            "input_near_black_frames": input_near_black,
            "decoded_near_black_frames": decoded_near_black,
            "playback_fps": playback_fps,
        },
        "timestamps": {
            "path": str(timestamps_path),
            "row_count": row_count,
            "contiguous_indices": contiguous_indices,
            "frame_count_matches_video": video_frame_count == row_count,
            "timestamp_sources": sources,
            "source_timestamps_available": source_timestamps_available,
            "realtime_clock_resolution_ms": time.clock_getres(time.CLOCK_REALTIME) * 1000.0,
            "unresolved_capture_latency": True,
            "wall_clock_span_s": ((real_ns[-1] - real_ns[0]) / 1e9) if len(real_ns) > 1 else 0.0,
        },
        "cadence": {
            "duration_monotonic_s": duration_s,
            "measured_fps": measured_fps,
            "median_dt_ms": median_dt_ms,
            "max_dt_ms": max_dt_ms,
            "jitter_std_ms": jitter_std_ms,
            "gap_threshold_ms": gap_threshold_ms,
            "gap_count": len(gap_events),
            "gaps": gap_events[:20],
            "unsynced_half_frame_quantization_ms": (median_dt_ms * 0.5)
            if median_dt_ms > 0
            else None,
        },
        "clock_stability": {
            "max_realtime_monotonic_shift_ms": max_clock_shift_ms,
            "clock_step_count": len(clock_step_events),
            "clock_steps": clock_step_events[:10],
        },
        "ntp_check": ntp_result.to_dict() if ntp_result is not None else None,
        "single_host_ntp_bound_ms": ntp_bound_ms,
        "read_failures": read_failures,
        "write_failures": write_failures,
        "hard_failures": hard_failures,
        "warnings": warnings,
    }


def format_report_summary(report: dict[str, Any]) -> str:
    vid = report["video"]
    ts = report["timestamps"]
    cad = report["cadence"]
    ntp = report["ntp_check"]
    lines = [
        f"Timing Verdict: {report['timing_status'].upper()} (threshold {report['go_threshold_ms']:.0f} ms @ 30 fps)",
        f"Video: {vid['path']} (playable={vid['playable']}, frames={vid['decoded_frame_count']}, size={vid['decoded_size_px']})",
        f"MP4 playback rate: {vid['playback_fps']} fps",
        f"Nearly black frames: input={vid['input_near_black_frames']}, decoded={vid['decoded_near_black_frames']}",
        (
            f"Timestamps: rows={ts['row_count']}, match_video={ts['frame_count_matches_video']}, "
            f"sources={ts['timestamp_sources']}, source_ts_available={ts['source_timestamps_available']}"
        ),
        (
            f"Cadence: {cad['measured_fps']:.2f} fps over {cad['duration_monotonic_s']:.2f} s, "
            f"median dt={cad['median_dt_ms']:.2f} ms, max dt={cad['max_dt_ms']:.2f} ms, gaps={cad['gap_count']}"
        ),
        f"Clock Stability: max |realtime-monotonic| shift = {report['clock_stability']['max_realtime_monotonic_shift_ms']:.3f} ms",
    ]
    if ntp is not None:
        if ntp["available"]:
            lines.append(
                f"NTP ({ntp['server']} @ {ntp['server_ip']}): median_offset={ntp['median_offset_ms']:+.2f} ms, "
                f"min_rtt={ntp['min_rtt_ms']:.2f} ms, half_rtt_uncert=±{ntp['half_rtt_uncertainty_ms']:.2f} ms"
            )
        else:
            lines.append(f"NTP ({ntp['server']}): UNAVAILABLE ({ntp['error']})")
    for err in report["hard_failures"]:
        lines.append(f"FAIL: {err}")
    for warn in report["warnings"]:
        lines.append(f"NOTE: {warn}")
    return "\n".join(lines)


def record_session(
    *,
    capture: FrameCaptureSource,
    station_id: str,
    output_dir: Path,
    requested_width: int,
    requested_height: int,
    requested_fps: float,
    codec: str = "avc1",
    duration_s: float | None = None,
    max_frames: int | None = None,
    preview: bool = False,
    ntp_server: str | None = _DEFAULT_NTP_SERVER,
) -> tuple[Path, Path, Path, Path]:
    """Capture raw frames from `capture`, pair per-frame timestamps, finalize, and verify."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if re.fullmatch(r"[A-Za-z0-9_-]+", station_id) is None:
        raise RecordingError("Station ID must use letters, digits, underscores or hyphens.")
    utc_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    session_stem = f"{station_id}_{utc_stamp}"
    video_path = output_dir / f"{session_stem}.mp4"
    timestamps_path = output_dir / f"{session_stem}_timestamps.csv"
    meta_path = output_dir / f"{session_stem}_meta.json"
    report_path = output_dir / f"{session_stem}_report.json"

    negotiated_width = float(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    negotiated_height = float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    negotiated_fps = float(capture.get(cv2.CAP_PROP_FPS))
    negotiated_fourcc = _decode_fourcc(float(capture.get(cv2.CAP_PROP_FOURCC)))

    print(
        f"Opened camera for station={station_id}: requested {requested_width}x{requested_height}@{requested_fps:.1f}, "
        f"reported {negotiated_width:.0f}x{negotiated_height:.0f}@{negotiated_fps:.2f} (fourcc={negotiated_fourcc})"
    )

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    writer: cv2.VideoWriter | None = None
    actual_codec = codec
    actual_w = 0
    actual_h = 0
    written_frames = 0
    attempt_idx = 0
    read_failures = 0
    write_failures = 0
    input_near_black = 0
    prev_written_mono_ns: int | None = None
    expected_dt_ms = 1000.0 / requested_fps if requested_fps > 0 else 33.333
    gap_threshold_ms = 1.5 * expected_dt_ms
    start_mono_ns = time.monotonic_ns()
    prev_pos_msec: float | None = None
    seen_advancing_pos = False

    try:
        with timestamps_path.open("x", encoding="utf-8", newline="") as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(TIMESTAMPS_COLUMNS)

            while not stop_requested:
                if max_frames is not None and written_frames >= max_frames:
                    break
                if (
                    duration_s is not None
                    and (time.monotonic_ns() - start_mono_ns) / 1_000_000_000.0 >= duration_s
                ):
                    break

                ok, frame = capture.read()
                realtime_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                monotonic_ns = time.monotonic_ns()
                pos_msec = float(capture.get(cv2.CAP_PROP_POS_MSEC))
                attempt_idx += 1

                if not ok or frame is None or frame.size == 0:
                    read_failures += 1
                    if read_failures >= 30 and written_frames == 0:
                        raise RecordingError(
                            "Camera failed to deliver any valid frame after 30 attempts."
                        )
                    continue

                frame_h, frame_w = int(frame.shape[0]), int(frame.shape[1])
                if writer is None:
                    actual_w, actual_h = frame_w, frame_h
                    writer, actual_codec = _open_video_writer(
                        video_path,
                        preferred_codec=codec,
                        fps=requested_fps,
                        width=actual_w,
                        height=actual_h,
                    )
                    print(
                        f"Recording actual frame size {actual_w}x{actual_h} -> {video_path} "
                        f"(codec={actual_codec}, playback_fps={requested_fps:g})"
                    )
                elif (frame_w, frame_h) != (actual_w, actual_h):
                    write_failures += 1
                    continue

                # Write raw frame unmodified (no overlays).
                if _is_near_black(frame):
                    input_near_black += 1
                    if written_frames == 14 and input_near_black == 15:
                        print(
                            "NOTE: First 15 camera frames are nearly black. Check the camera preview.",
                            file=sys.stderr,
                            flush=True,
                        )
                writer.write(frame)

                if math.isfinite(pos_msec) and pos_msec > 0.0:
                    seen_advancing_pos = prev_pos_msec is not None and pos_msec > prev_pos_msec
                    prev_pos_msec = pos_msec
                else:
                    seen_advancing_pos = False
                ts_source = "backend_source_pos_msec" if seen_advancing_pos else "host_receipt_time"
                clock_domain = (
                    "backend_pos_msec+host_realtime_monotonic"
                    if seen_advancing_pos
                    else "host_realtime_and_monotonic"
                )

                dt_ms_str = ""
                gap_flag = ""
                if prev_written_mono_ns is not None:
                    dt_ms = (monotonic_ns - prev_written_mono_ns) / 1_000_000.0
                    dt_ms_str = f"{dt_ms:.6f}"
                    if dt_ms > gap_threshold_ms:
                        gap_flag = "gap"
                prev_written_mono_ns = monotonic_ns

                csv_writer.writerow(
                    [
                        station_id,
                        written_frames,
                        attempt_idx - 1,
                        ts_source,
                        clock_domain,
                        f"{pos_msec:.6f}" if math.isfinite(pos_msec) and pos_msec > 0.0 else "",
                        realtime_ns,
                        monotonic_ns,
                        realtime_ns - monotonic_ns,
                        dt_ms_str,
                        gap_flag,
                        actual_w,
                        actual_h,
                        f"{negotiated_fps:.4f}",
                    ]
                )
                written_frames += 1

                if preview:
                    cv2.imshow(f"Chongming Recorder ({station_id}) - raw preview", frame)
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        stop_requested = True

            csv_file.flush()
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
        if writer is not None:
            writer.release()
        capture.release()
        if preview:
            cv2.destroyAllWindows()

    if written_frames == 0:
        raise RecordingError("No frames were written before stop.")

    ntp_result = query_ntp_offset(ntp_server) if ntp_server else None
    meta = {
        "schema_version": META_SCHEMA,
        "station_id": station_id,
        "host": {
            "node": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "requested_format": {
            "width_px": requested_width,
            "height_px": requested_height,
            "fps": requested_fps,
        },
        "capture_reported_format": {
            "width_px": negotiated_width,
            "height_px": negotiated_height,
            "fps": negotiated_fps,
            "fourcc": negotiated_fourcc,
        },
        "actual_written_format": {
            "width_px": actual_w,
            "height_px": actual_h,
            "fps": requested_fps,
            "codec": actual_codec,
            "written_frames": written_frames,
        },
        "read_failures": read_failures,
        "write_failures": write_failures,
        "input_near_black_frames": input_near_black,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    report = analyze_recording(
        video_path=video_path,
        timestamps_path=timestamps_path,
        meta=meta,
        ntp_result=ntp_result,
        expected_fps=requested_fps,
    )
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(format_report_summary(report))
    return video_path, timestamps_path, meta_path, report_path


def _macos_system_cameras() -> list[dict[str, str]]:
    if platform.system() != "Darwin":
        return []
    try:
        proc = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            return []
        payload = json.loads(proc.stdout)
        entries = payload.get("SPCameraDataType", [])
        if not isinstance(entries, list):
            return []
        return [
            {
                "name": str(item.get("_name", "")),
                "model_id": str(item.get("spcamera_model-id", "")),
                "unique_id": str(item.get("spcamera_unique-id", "")),
            }
            for item in entries
            if isinstance(item, dict)
        ]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def _cmd_list_cameras(max_index: int) -> int:
    sys_cams = _macos_system_cameras()
    if sys_cams:
        print("macOS system_profiler cameras:")
        for item in sys_cams:
            print(f"  - {item['name']} (model={item['model_id']}, unique_id={item['unique_id']})")
        if len(sys_cams) > 1:
            print(
                "Note: OpenCV AVFoundation index order can differ from system_profiler (for example, "
                "a nearby iPhone Continuity Camera may appear at camera[0] and the built-in MacBook "
                "camera at camera[1])."
            )

    get_log_level = getattr(cv2, "getLogLevel", None)
    set_log_level = getattr(cv2, "setLogLevel", None)
    prev_log_level: int | None = None
    if callable(get_log_level) and callable(set_log_level):
        prev_log_level = int(get_log_level())
        set_log_level(0)

    found = 0
    try:
        for idx in range(max_index):
            cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
            if not cap.isOpened():
                cap.release()
                if found > 0:
                    break
                continue
            w0 = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            h0 = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            fps0 = cap.get(cv2.CAP_PROP_FPS)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920.0)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080.0)
            cap.set(cv2.CAP_PROP_FPS, 30.0)
            w1 = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            h1 = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            fps1 = cap.get(cv2.CAP_PROP_FPS)
            fourcc = _decode_fourcc(cap.get(cv2.CAP_PROP_FOURCC))
            print(
                f"OpenCV camera[{idx}]: default {w0:.0f}x{h0:.0f}@{fps0:.2f}fps -> "
                f"after 1080p@30 request: {w1:.0f}x{h1:.0f}@{fps1:.2f}fps (fourcc={fourcc})"
            )
            found += 1
            cap.release()
    finally:
        if prev_log_level is not None and callable(set_log_level):
            set_log_level(prev_log_level)

    if found == 0:
        print(
            "No OpenCV cameras opened. On macOS, ensure Camera permission is granted in "
            "System Settings -> Privacy & Security -> Camera."
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Record raw camera video and per-frame timestamps")
    rec.add_argument("--station-id", required=True, help="Station identifier (e.g. station-a)")
    rec.add_argument("--camera", type=int, default=0, help="Camera index (default: 0)")
    rec.add_argument(
        "--backend",
        choices=("avfoundation", "any"),
        default="avfoundation",
        help="OpenCV capture backend",
    )
    rec.add_argument("--width", type=int, default=1920, help="Requested width (default: 1920)")
    rec.add_argument("--height", type=int, default=1080, help="Requested height (default: 1080)")
    rec.add_argument("--fps", type=float, default=30.0, help="Requested frame rate (default: 30)")
    rec.add_argument("--codec", default="avc1", help="FourCC codec (default: avc1)")
    rec.add_argument("--duration", type=float, help="Optional recording duration in seconds")
    rec.add_argument("--max-frames", type=int, help="Optional maximum frame count")
    rec.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/recordings"),
        help="Output directory (keep outside git; default: results/recordings)",
    )
    rec.add_argument("--preview", action="store_true", help="Show raw preview window (q to stop)")
    rec.add_argument(
        "--ntp-server",
        default=_DEFAULT_NTP_SERVER,
        help=f"Read-only NTP reference server (default: {_DEFAULT_NTP_SERVER})",
    )
    rec.add_argument("--skip-ntp", action="store_true", help="Skip NTP offset check")

    ver = sub.add_parser("verify", help="Reopen a recorded clip and audit timestamps/timing")
    ver.add_argument("--video", type=Path, required=True, help="Recorded video path")
    ver.add_argument("--timestamps", type=Path, required=True, help="Timestamps CSV path")
    ver.add_argument("--meta", type=Path, help="Optional session metadata JSON path")
    ver.add_argument("--expected-fps", type=float, default=30.0, help="Expected FPS (default: 30)")
    ver.add_argument(
        "--ntp-server",
        default=_DEFAULT_NTP_SERVER,
        help=f"Read-only NTP reference server (default: {_DEFAULT_NTP_SERVER})",
    )
    ver.add_argument("--skip-ntp", action="store_true", help="Skip NTP offset check")
    ver.add_argument("--output", type=Path, help="Optional JSON report output path")

    ntp = sub.add_parser("ntp-check", help="Run a read-only NTP offset & uncertainty check")
    ntp.add_argument("--server", default=_DEFAULT_NTP_SERVER, help="NTP server hostname")
    ntp.add_argument("--samples", type=int, default=5, help="Number of UDP samples (default: 5)")

    lst = sub.add_parser("list-cameras", help="Probe available camera indices")
    lst.add_argument("--max-index", type=int, default=5, help="Indices 0..N-1 to probe")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-cameras":
        return _cmd_list_cameras(args.max_index)

    if args.command == "ntp-check":
        res = query_ntp_offset(args.server, samples=args.samples)
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.available else 1

    if args.command == "verify":
        meta = json.loads(args.meta.read_text(encoding="utf-8")) if args.meta else None
        ntp_res = None if args.skip_ntp else query_ntp_offset(args.ntp_server)
        report = analyze_recording(
            video_path=args.video,
            timestamps_path=args.timestamps,
            meta=meta,
            ntp_result=ntp_res,
            expected_fps=args.expected_fps,
        )
        print(format_report_summary(report))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 0 if report["timing_status"] != "no_go" else 2

    if args.command == "record":
        cap = _open_camera_capture(
            args.camera,
            width=args.width,
            height=args.height,
            fps=args.fps,
            backend=args.backend,
        )
        _, _, _, report_path = record_session(
            capture=cap,
            station_id=args.station_id,
            output_dir=args.output_dir,
            requested_width=args.width,
            requested_height=args.height,
            requested_fps=args.fps,
            codec=args.codec,
            duration_s=args.duration,
            max_frames=args.max_frames,
            preview=args.preview,
            ntp_server=None if args.skip_ntp else args.ntp_server,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return 0 if report["timing_status"] != "no_go" else 2

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecordingError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
