from __future__ import annotations

import csv
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Self

import cv2
import numpy as np
from numpy.typing import NDArray

from scripts.record_camera import (
    NtpCheckResult,
    _ntp_pair_to_unix_ns,
    _unix_ns_to_ntp_pair,
    analyze_recording,
    main,
    query_ntp_offset,
    record_session,
)


class _FakeCapture:
    def __init__(
        self,
        events: Sequence[tuple[bool, NDArray[np.uint8] | None, float]],
        *,
        width: int = 640,
        height: int = 480,
        fps: float = 30.0,
        delay_s: float = 0.01,
    ) -> None:
        self._events = events
        self._index = 0
        self._width = width
        self._height = height
        self._fps = fps
        self._delay_s = delay_s
        self._current_pos_ms = 0.0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def set(self, prop_id: int, value: float) -> bool:
        return True

    def get(self, prop_id: int) -> float:
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        if prop_id == cv2.CAP_PROP_FPS:
            return float(self._fps)
        if prop_id == cv2.CAP_PROP_POS_MSEC:
            return self._current_pos_ms
        if prop_id == cv2.CAP_PROP_FOURCC:
            return float(cv2.VideoWriter_fourcc(*"avc1"))  # type: ignore[attr-defined]
        return 0.0

    def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
        if self._delay_s > 0:
            time.sleep(self._delay_s)
        if self._index >= len(self._events):
            return False, None
        ok, frame, pos_ms = self._events[self._index]
        self._index += 1
        self._current_pos_ms = pos_ms
        return ok, frame

    def release(self) -> None:
        self.released = True


class _LoopbackNtpServer:
    def __init__(self, simulated_offset_ns: int) -> None:
        self.simulated_offset_ns = simulated_offset_ns
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = int(self.sock.getsockname()[1])
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop = True
        self.sock.close()
        self._thread.join(timeout=1.0)

    def _serve(self) -> None:
        self.sock.settimeout(0.1)
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(512)
            except TimeoutError:
                continue
            except OSError:
                break
            if len(data) < 48:
                continue
            now_ns = time.clock_gettime_ns(time.CLOCK_REALTIME) + self.simulated_offset_ns
            sec, frac = _unix_ns_to_ntp_pair(now_ns)
            resp = bytearray(48)
            resp[0] = (4 << 3) | 4  # VN=4, Mode=4 (server)
            resp[1] = 2  # stratum 2
            resp[24:32] = data[40:48]
            struct.pack_into("!IIII", resp, 32, sec, frac, sec, frac)
            self.sock.sendto(bytes(resp), addr)


class RecordCameraTests(unittest.TestCase):
    def test_record_host_receipt_with_read_failure_pairs_frames_and_marks_provisional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            frames = [np.full((240, 320, 3), 20 + idx * 15, dtype=np.uint8) for idx in range(6)]
            events: list[tuple[bool, NDArray[np.uint8] | None, float]] = [
                (True, frames[0], 0.0),
                (True, frames[1], 0.0),
                (False, None, 0.0),  # simulated read failure mid-stream
                (True, frames[2], 0.0),
                (True, np.zeros((100, 100, 3), dtype=np.uint8), 0.0),  # size mismatch -> write skip
                (True, frames[3], 0.0),
                (True, frames[4], 0.0),
                (True, frames[5], 0.0),
            ]
            cap = _FakeCapture(events, width=320, height=240, fps=30.0, delay_s=0.005)
            video_path, ts_path, meta_path, report_path = record_session(
                capture=cap,
                station_id="station-mac-a",
                output_dir=out_dir,
                requested_width=320,
                requested_height=240,
                requested_fps=30.0,
                max_frames=6,
                ntp_server=None,
            )

            self.assertTrue(cap.released)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["read_failures"], 1)
            self.assertEqual(meta["write_failures"], 1)
            self.assertEqual(meta["actual_written_format"]["written_frames"], 6)

            with ts_path.open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 6)
            self.assertEqual([int(r["output_frame_index"]) for r in rows], list(range(6)))
            self.assertTrue(all(r["timestamp_source"] == "host_receipt_time" for r in rows))

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["timing_status"], "provisional")
            self.assertTrue(report["timestamps"]["frame_count_matches_video"])
            self.assertFalse(report["timestamps"]["source_timestamps_available"])
            self.assertEqual(report["video"]["decoded_frame_count"], 6)

            # Verify CLI verify subcommand agrees on reopened clip.
            verify_out = out_dir / "verify.json"
            exit_code = main(
                [
                    "verify",
                    "--video",
                    str(video_path),
                    "--timestamps",
                    str(ts_path),
                    "--meta",
                    str(meta_path),
                    "--skip-ntp",
                    "--output",
                    str(verify_out),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(verify_out.read_text())["timing_status"], "provisional")

    def test_advancing_backend_timestamps_and_loopback_ntp_are_provisional(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            _LoopbackNtpServer(simulated_offset_ns=4_000_000) as srv,
        ):
            ntp_res = query_ntp_offset("127.0.0.1", port=srv.port, samples=4, timeout_s=1.0)
            self.assertTrue(ntp_res.available)
            assert ntp_res.median_offset_ms is not None
            self.assertAlmostEqual(ntp_res.median_offset_ms, 4.0, delta=2.0)

            out_dir = Path(tmp)
            events = [
                (True, np.full((240, 320, 3), 50 + idx * 10, dtype=np.uint8), 1000.0 + idx * 33.3)
                for idx in range(6)
            ]
            cap = _FakeCapture(events, width=320, height=240, fps=30.0, delay_s=0.005)
            video_path, ts_path, meta_path, _ = record_session(
                capture=cap,
                station_id="station-mac-b",
                output_dir=out_dir,
                requested_width=320,
                requested_height=240,
                requested_fps=30.0,
                max_frames=6,
                ntp_server=None,
            )
            report = analyze_recording(
                video_path=video_path,
                timestamps_path=ts_path,
                meta=json.loads(meta_path.read_text(encoding="utf-8")),
                ntp_result=ntp_res,
                expected_fps=30.0,
            )
            self.assertEqual(report["timing_status"], "provisional")
            self.assertTrue(report["timestamps"]["source_timestamps_available"])
            self.assertTrue(report["timestamps"]["unresolved_capture_latency"])

    def test_frame_mismatch_and_excess_ntp_offset_trigger_no_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            events = [(True, np.full((240, 320, 3), 40, dtype=np.uint8), 0.0) for _ in range(5)]
            cap = _FakeCapture(events, width=320, height=240, fps=30.0, delay_s=0.002)
            video_path, ts_path, _, _ = record_session(
                capture=cap,
                station_id="station-bad",
                output_dir=out_dir,
                requested_width=320,
                requested_height=240,
                requested_fps=30.0,
                max_frames=5,
                ntp_server=None,
            )

            # A proxied NTP result is not a usable clock offset measurement.
            bad_ntp = NtpCheckResult(
                available=True,
                server="time.apple.com",
                server_ip="198.18.0.69",
                samples_requested=5,
                samples_succeeded=5,
                median_offset_ms=48.0,
                min_rtt_ms=4.0,
                half_rtt_uncertainty_ms=2.0,
                offset_spread_ms=0.5,
            )
            rep_ntp = analyze_recording(
                video_path=video_path, timestamps_path=ts_path, ntp_result=bad_ntp
            )
            self.assertEqual(rep_ntp["timing_status"], "provisional")
            self.assertIsNone(rep_ntp["single_host_ntp_bound_ms"])
            self.assertTrue(any("Fake-IP" in w for w in rep_ntp["warnings"]))

            # Case 2: Drop last CSV row so video frames != timestamp rows.
            lines = ts_path.read_text(encoding="utf-8").splitlines()
            ts_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            rep_mismatch = analyze_recording(video_path=video_path, timestamps_path=ts_path)
            self.assertEqual(rep_mismatch["timing_status"], "no_go")
            self.assertFalse(rep_mismatch["timestamps"]["frame_count_matches_video"])

    def test_ntp_pair_roundtrip(self) -> None:
        now_ns = 1_764_077_000_123_456_789
        sec, frac = _unix_ns_to_ntp_pair(now_ns)
        recovered = _ntp_pair_to_unix_ns(sec, frac)
        self.assertLess(abs(recovered - now_ns), 2)


if __name__ == "__main__":
    unittest.main()
