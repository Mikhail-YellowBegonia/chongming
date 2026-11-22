"""Tests for length-prefixed Protobuf framing and multi-frame .pb.zst streams."""

from __future__ import annotations

import io
import struct
import tempfile
import unittest
from pathlib import Path

from chongming_proto.framing import MAX_FRAME_BYTES
from chongming_proto.online import (
    PbZstWriter,
    common_pb2,
    encode_length_prefixed,
    iter_pb_zst,
    online_pb2,
    read_length_prefixed,
)
from chongming_proto.truth import truth_pb2


class ProtoFramingTests(unittest.TestCase):
    def test_length_prefixed_roundtrip_and_limits(self) -> None:
        msg = online_pb2.BusMessage(
            bus_sequence_no=42,
            sent_wall_time_ns=1_763_784_000_123_456_789,
            progress_report=online_pb2.ProgressReport(
                source_role=common_pb2.COMPONENT_ROLE_CENTER,
                source_id="center",
                session_id="s-1",
                completed_time_ns=9_007_199_254_740_993,
                completed_sequence_no=7,
                finite_end_reached=True,
            ),
        )
        encoded = encode_length_prefixed(msg)
        stream = io.BytesIO(encoded)
        decoded = read_length_prefixed(stream, online_pb2.BusMessage)
        assert decoded is not None
        self.assertEqual(decoded.bus_sequence_no, 42)
        self.assertEqual(decoded.sent_wall_time_ns, 1_763_784_000_123_456_789)
        self.assertEqual(decoded.progress_report.completed_time_ns, 9_007_199_254_740_993)
        self.assertIsNone(read_length_prefixed(stream, online_pb2.BusMessage))

        with self.assertRaisesRegex(ValueError, "exceeds max_bytes"):
            encode_length_prefixed(msg, max_bytes=4)
        with self.assertRaisesRegex(ValueError, "exceeds max_bytes"):
            read_length_prefixed(io.BytesIO(encoded), online_pb2.BusMessage, max_bytes=4)
        with self.assertRaisesRegex(ValueError, "max_bytes"):
            encode_length_prefixed(msg, max_bytes=MAX_FRAME_BYTES + 1)

    def test_truncated_and_corrupt_frames_are_rejected(self) -> None:
        with self.assertRaises(EOFError):
            read_length_prefixed(io.BytesIO(b"\x00\x00"), online_pb2.BusMessage)
        with self.assertRaises(EOFError):
            read_length_prefixed(
                io.BytesIO(struct.pack(">I", 10) + b"abc"),
                online_pb2.BusMessage,
            )
        bad_proto = struct.pack(">I", 2) + b"\xff\xff"
        with self.assertRaisesRegex(ValueError, "invalid BusMessage"):
            read_length_prefixed(io.BytesIO(bad_proto), online_pb2.BusMessage)

    def test_multiframe_pb_zst_roundtrip_overwrite_refusal_and_crash_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.pb.zst"
            with PbZstWriter(path, flush_every=2) as writer:
                for idx in range(5):
                    writer.write(
                        online_pb2.BusMessage(
                            bus_sequence_no=idx + 1,
                            sent_wall_time_ns=1_000_000_000_000_000 + idx,
                        )
                    )
            loaded = list(iter_pb_zst(path, online_pb2.BusMessage))
            self.assertEqual([m.bus_sequence_no for m in loaded], [1, 2, 3, 4, 5])
            self.assertEqual(
                [m.sent_wall_time_ns for m in loaded],
                [1_000_000_000_000_000 + idx for idx in range(5)],
            )

            with self.assertRaises(FileExistsError), PbZstWriter(path, overwrite=False):
                pass

            partial_path = Path(directory) / "partial.pb.zst"
            with PbZstWriter(partial_path, flush_every=10) as writer:
                writer.write(online_pb2.BusMessage(bus_sequence_no=10))
                writer.write(online_pb2.BusMessage(bus_sequence_no=20))
                writer.flush_frame()
                # Read already-flushed zstd frame while writer stays open.
                flushed = list(iter_pb_zst(partial_path, online_pb2.BusMessage))
                self.assertEqual([m.bus_sequence_no for m in flushed], [10, 20])
                writer.write(online_pb2.BusMessage(bus_sequence_no=30))
            self.assertEqual(
                [m.bus_sequence_no for m in iter_pb_zst(partial_path, online_pb2.BusMessage)],
                [10, 20, 30],
            )

    def test_truth_64bit_scalar_precision(self) -> None:
        precise_x = 1234567.8901234567
        precise_ns = 9_007_199_254_740_997
        frame = truth_pb2.TruthFrame(
            world_frame_id="enu",
            experiment_time_ns=precise_ns,
            step_index=3,
            targets=[
                truth_pb2.TargetTruth(
                    target_id="t1",
                    target_class="f86",
                    position_enu_m=common_pb2.Vec3(x=precise_x, y=-2.5, z=100.125),
                    velocity_enu_mps=common_pb2.Vec3(x=10.25, y=-5.5, z=0.125),
                    bounding_radius_m=6.5,
                    body_aabb_diagonal_m=13.0,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truth.pb.zst"
            with PbZstWriter(path) as writer:
                writer.write(frame)
            restored = next(iter(iter_pb_zst(path, truth_pb2.TruthFrame)))
        self.assertEqual(restored.experiment_time_ns, precise_ns)
        self.assertEqual(restored.targets[0].position_enu_m.x, precise_x)
        self.assertEqual(restored.targets[0].body_aabb_diagonal_m, 13.0)

    def test_test_framework_message_embeds_bus_message_roundtrip(self) -> None:
        wrapped = truth_pb2.TestFrameworkMessage(
            sequence_no=9,
            bus_message=online_pb2.BusMessage(
                bus_sequence_no=3,
                sent_wall_time_ns=1_763_784_000_000_000_001,
                session_hello=online_pb2.SessionHello(
                    protocol_version="chongming.v1",
                    role=common_pb2.COMPONENT_ROLE_SCORER,
                    instance_id="scorer-1",
                    session_id="run-1",
                ),
            ),
        )
        encoded = encode_length_prefixed(wrapped)
        decoded = read_length_prefixed(io.BytesIO(encoded), truth_pb2.TestFrameworkMessage)
        assert decoded is not None
        self.assertEqual(decoded.sequence_no, 9)
        self.assertEqual(decoded.WhichOneof("payload"), "bus_message")
        self.assertEqual(decoded.bus_message.WhichOneof("payload"), "session_hello")
        self.assertEqual(
            decoded.bus_message.session_hello.role,
            common_pb2.COMPONENT_ROLE_SCORER,
        )
        self.assertEqual(
            decoded.bus_message.sent_wall_time_ns,
            1_763_784_000_000_000_001,
        )
