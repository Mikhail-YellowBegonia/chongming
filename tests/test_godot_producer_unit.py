"""Unit tests for Godot Producer core math, physics visibility, and Protobuf3 wire interop."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from chongming.bus.codec import station_config_from_proto
from chongming.contracts import model as m
from chongming_proto.framing import encode_length_prefixed, read_length_prefixed
from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2

ROOT = Path(__file__).resolve().parents[1]
PRODUCER_DIR = ROOT / "godot_producer"
GODOT = os.environ.get("CHONGMING_GODOT") or shutil.which("godot4") or shutil.which("godot")


def _run_godot_headless(script_relpath: str, *user_args: str, timeout_s: float = 30.0) -> str:
    assert GODOT is not None
    cmd = [
        GODOT,
        "--headless",
        "--path",
        str(PRODUCER_DIR),
        "--script",
        script_relpath,
        "--",
        *user_args,
    ]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Godot failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout


@unittest.skipUnless(GODOT, "Godot 4 executable is required")
class GodotProducerUnitTests(unittest.TestCase):
    def test_math_physics_occlusion_and_protobuf_wire_interop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stdout = _run_godot_headless("tests/unit_tests.gd", str(out_dir))
            marker = "GODOT_UNIT_TESTS "
            report_line = next(
                (line for line in stdout.splitlines() if line.startswith(marker)),
                None,
            )
            self.assertIsNotNone(report_line, f"Missing marker in Godot output:\n{stdout}")
            assert report_line is not None
            report = json.loads(report_line.removeprefix(marker))
            self.assertTrue(report["ok"], f"Godot unit test errors: {report['errors']}")
            self.assertEqual(report["occ_blocked"], truth_pb2.OCCLUSION_STATUS_OCCLUDED)
            self.assertEqual(report["occ_clear"], truth_pb2.OCCLUSION_STATUS_VISIBLE)

            hello_msg = online_pb2.BusMessage.FromString(
                (out_dir / "session_hello.bin").read_bytes()
            )
            self.assertEqual(hello_msg.WhichOneof("payload"), "session_hello")
            self.assertEqual(hello_msg.session_hello.protocol_version, "chongming.v1")
            self.assertEqual(hello_msg.session_hello.role, common_pb2.COMPONENT_ROLE_PRODUCER)
            self.assertEqual(list(hello_msg.session_hello.station_ids), ["s1"])
            self.assertEqual(hello_msg.sent_wall_time_ns, 2**53 + 100)

            cfg_msg = online_pb2.BusMessage.FromString(
                (out_dir / "station_config.bin").read_bytes()
            )
            self.assertEqual(cfg_msg.WhichOneof("payload"), "station_config")
            decoded_cfg = station_config_from_proto(cfg_msg.station_config)
            self.assertEqual(decoded_cfg.station_id, m.StationId("s1"))
            self.assertEqual(decoded_cfg.main_camera_position_enu_m, (0.0, 0.0, 0.0))
            self.assertIsInstance(decoded_cfg.reference_anchor, m.SurveyedReference)
            assert isinstance(decoded_cfg.reference_anchor, m.SurveyedReference)
            self.assertEqual(decoded_cfg.reference_anchor.reference_id, "board-1")
            self.assertEqual(len(decoded_cfg.reference_anchor.landmarks), 1)
            self.assertEqual(
                decoded_cfg.reference_anchor.landmarks[0].position_enu_m,
                (1.0, 2.0, 0.5),
            )

            pkt_msg = online_pb2.BusMessage.FromString((out_dir / "video_packet.bin").read_bytes())
            self.assertEqual(pkt_msg.WhichOneof("payload"), "video_packet")
            pkt = pkt_msg.video_packet
            self.assertEqual(pkt.camera_role, common_pb2.CAMERA_ROLE_MAIN)
            self.assertEqual(pkt.codec, common_pb2.VIDEO_CODEC_RAW)
            self.assertEqual(pkt.pixel_format, common_pb2.PIXEL_FORMAT_RGB8)
            self.assertEqual(pkt.capture_time.exposure_start_ns, 2**53 + 100)
            self.assertEqual(
                pkt.payload, bytes([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
            )

            ray_msg = online_pb2.BusMessage.FromString((out_dir / "ray_event.bin").read_bytes())
            self.assertEqual(ray_msg.WhichOneof("payload"), "ray_event")
            self.assertEqual(ray_msg.ray_event.frame_id, "s1-main-0000")
            self.assertEqual(ray_msg.ray_event.ray_direction_enu_unit.y, 1.0)
            self.assertEqual(
                ray_msg.ray_event.calibration_usage,
                common_pb2.CALIBRATION_USAGE_VALID,
            )

            pr_msg = online_pb2.BusMessage.FromString(
                (out_dir / "progress_report.bin").read_bytes()
            )
            self.assertEqual(pr_msg.WhichOneof("payload"), "progress_report")
            self.assertTrue(pr_msg.progress_report.finite_end_reached)
            self.assertEqual(pr_msg.progress_report.completed_time_ns, 2**53 + 100)

            tfm = truth_pb2.TestFrameworkMessage.FromString(
                (out_dir / "truth_frame.bin").read_bytes()
            )
            self.assertEqual(tfm.WhichOneof("payload"), "truth_frame")
            self.assertEqual(tfm.truth_frame.experiment_time_ns, 2**53 + 100)
            self.assertEqual(len(tfm.truth_frame.targets), 1)
            self.assertEqual(tfm.truth_frame.targets[0].body_aabb_diagonal_m, 10.0)
            self.assertEqual(len(tfm.truth_frame.projections), 2)
            self.assertEqual(
                tfm.truth_frame.projections[0].occlusion_status,
                truth_pb2.OCCLUSION_STATUS_VISIBLE,
            )
            self.assertEqual(
                tfm.truth_frame.projections[1].occlusion_status,
                truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV,
            )
            self.assertFalse(tfm.truth_frame.projections[1].HasField("projected_centroid_px"))
            self.assertTrue(tfm.truth_frame.projections[1].HasField("range_m"))
            self.assertEqual(tfm.truth_frame.projections[1].range_m, 0.0)

            tfm_neg = truth_pb2.TestFrameworkMessage.FromString(
                (out_dir / "truth_frame_neg.bin").read_bytes()
            )
            self.assertEqual(tfm_neg.truth_frame.experiment_time_ns, -1234567890123456789)

    def test_producer_bus_client_tcp_handshake_and_progress(self) -> None:
        bus_msgs: list[online_pb2.BusMessage] = []
        truth_msgs: list[truth_pb2.TestFrameworkMessage] = []
        errors: list[BaseException] = []

        bus_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bus_srv.bind(("127.0.0.1", 0))
        bus_srv.listen(1)
        bus_port = int(bus_srv.getsockname()[1])

        truth_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        truth_srv.bind(("127.0.0.1", 0))
        truth_srv.listen(1)
        truth_port = int(truth_srv.getsockname()[1])

        def serve_bus() -> None:
            try:
                conn, _ = bus_srv.accept()
                with conn, conn.makefile("rb") as reader:
                    hello = read_length_prefixed(reader, online_pb2.BusMessage)
                    assert hello is not None
                    bus_msgs.append(hello)
                    conn.sendall(
                        encode_length_prefixed(
                            online_pb2.BusMessage(
                                bus_sequence_no=1,
                                session_ack=online_pb2.SessionAck(
                                    accepted=True, reason="ok", run_id="mock-run"
                                ),
                            )
                        )
                    )
                    while True:
                        msg = read_length_prefixed(reader, online_pb2.BusMessage)
                        if msg is None:
                            break
                        bus_msgs.append(msg)
            except (OSError, AssertionError, ValueError, EOFError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                bus_srv.close()

        def serve_truth() -> None:
            try:
                conn, _ = truth_srv.accept()
                with conn, conn.makefile("rb") as reader:
                    hello = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                    assert hello is not None
                    truth_msgs.append(hello)
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=1,
                                bus_message=online_pb2.BusMessage(
                                    bus_sequence_no=1,
                                    session_ack=online_pb2.SessionAck(
                                        accepted=True, reason="ok", run_id="mock-run"
                                    ),
                                ),
                            )
                        )
                    )
                    while True:
                        msg = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                        if msg is None:
                            break
                        truth_msgs.append(msg)
            except (OSError, AssertionError, ValueError, EOFError) as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                truth_srv.close()

        t1 = threading.Thread(target=serve_bus, daemon=True)
        t2 = threading.Thread(target=serve_truth, daemon=True)
        t1.start()
        t2.start()

        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "tcp_client_test.gd"
            driver.write_text(
                "\n".join(
                    [
                        "extends SceneTree",
                        'const ProtoWire = preload("res://core/proto_wire.gd")',
                        'const BusClient = preload("res://core/producer_bus_client.gd")',
                        "func _init() -> void:",
                        "\tvar client := BusClient.new()",
                        f'\tvar ok := client.connect_and_handshake("127.0.0.1", {bus_port}, "127.0.0.1", {truth_port}, "gd-p", "gd-s", ["s1"], 5000)',
                        "\tif not ok:",
                        '\t\tpush_error("Handshake failed: " + client.last_error())',
                        "\t\tquit(1)",
                        "\t\treturn",
                        "\tclient.send_truth_frame({",
                        '\t\t"world_frame_id": "enu",',
                        '\t\t"experiment_time_ns": 12345,',
                        '\t\t"step_index": 0,',
                        '\t\t"targets": [],',
                        '\t\t"projections": [],',
                        "\t})",
                        "\tclient.send_progress_report({",
                        '\t\t"source_role": ProtoWire.COMPONENT_ROLE_PRODUCER,',
                        '\t\t"source_id": "gd-p",',
                        '\t\t"session_id": "gd-s",',
                        '\t\t"completed_time_ns": 12345,',
                        '\t\t"completed_sequence_no": 1,',
                        '\t\t"finite_end_reached": true,',
                        "\t})",
                        "\tclient.close()",
                        "\tquit(0)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            _run_godot_headless(str(driver))

        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(
            [m.WhichOneof("payload") for m in bus_msgs], ["session_hello", "progress_report"]
        )
        self.assertTrue(bus_msgs[1].progress_report.finite_end_reached)
        self.assertEqual(
            [m.WhichOneof("payload") for m in truth_msgs], ["bus_message", "truth_frame"]
        )
        self.assertEqual(truth_msgs[1].truth_frame.experiment_time_ns, 12345)
