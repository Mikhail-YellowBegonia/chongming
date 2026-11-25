"""Tests for online V-Switch TCP scoring and offline parity."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path

from chongming_proto.framing import encode_length_prefixed, iter_pb_zst, read_length_prefixed
from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.cli import main
from chongming_scorer.online import OnlineScorerClient, OnlineScorerSubscriber, ScorerHandshakeError
from chongming_scorer.scorer import CenterIncrementalScorer
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)


def _start_tcp_server(
    handler: Callable[[socket.socket], None],
) -> tuple[int, threading.Thread, list[BaseException]]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                handler(conn)
        except (OSError, AssertionError, ValueError, EOFError) as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread, errors


def _build_fixture(root: Path) -> tuple[Path, Path, list[truth_pb2.TruthFrame]]:
    scene_dir = root / "scene"
    run_dir = root / "run"
    truth_frames: list[truth_pb2.TruthFrame] = []

    with TestSceneWriter(
        scene_dir,
        scenario_id="online-pipe-1",
        world_frame_id="enu",
        clock_domain_id="clk",
        start_time_ns=0,
        end_time_ns=20_000_000_000,
        stations=[],
    ) as sw:
        for idx, ns in enumerate((0, 16_000_000_000, 17_000_000_000)):
            cap = common_pb2.CaptureTime(
                clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
            )
            for st in ("s1", "s2"):
                sw.add_frame_index(
                    truth_pb2.FrameIndexEntry(
                        station_id=st,
                        station_session_id=f"sess-{st}",
                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                        frame_index=idx,
                        frame_id=f"{st}-{idx}",
                        capture_time=cap,
                        station_config_digest="cfg",
                        video_relpath=f"video/{st}-main.mp4",
                        width_px=64,
                        height_px=64,
                    )
                )
            tf = truth_pb2.TruthFrame(
                world_frame_id="enu",
                experiment_time_ns=ns,
                step_index=idx,
                targets=[
                    truth_pb2.TargetTruth(
                        target_id="target-1",
                        target_class="sphere",
                        position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                        velocity_enu_mps=common_pb2.Vec3(),
                        bounding_radius_m=1.0,
                        body_aabb_diagonal_m=2.0,
                    )
                ],
                projections=[
                    truth_pb2.StationTargetProjection(
                        station_id=st,
                        target_id="target-1",
                        occlusion_status=truth_pb2.OCCLUSION_STATUS_VISIBLE,
                        projected_centroid_px=common_pb2.Vec2(x=20.0, y=20.0),
                        projected_diagonal_px=4.0,
                        range_m=50.0,
                        frame_id=f"{st}-{idx}",
                    )
                    for st in ("s1", "s2")
                ],
            )
            sw.add_truth_frame(tf)
            truth_frames.append(tf)

    scene = TestScenePackage.open(scene_dir)
    with RunOutputWriter(
        run_dir,
        run_id="run-online-1",
        mode="integrated",
        test_scene_path=scene_dir,
        test_scene_sha256=scene.content_sha256,
    ) as rw:
        seq = 0
        for idx, ns in enumerate((0, 16_000_000_000, 17_000_000_000)):
            cap = common_pb2.CaptureTime(
                clock_domain_id="clk", exposure_start_ns=ns, exposure_end_ns=ns
            )
            for st in ("s1", "s2"):
                seq += 1
                obs = (
                    [
                        online_pb2.Observation2D(
                            centroid_px=common_pb2.Vec2(x=20.0, y=20.0),
                            local_track_id="t1",
                        )
                    ]
                    if idx == 1
                    else []
                )
                rw.write_edge_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=seq,
                        detection_batch=online_pb2.DetectionBatch(
                            main_frame_ref=common_pb2.FrameRef(
                                station_id=st,
                                station_session_id=f"sess-{st}",
                                camera_role=common_pb2.CAMERA_ROLE_MAIN,
                                frame_id=f"{st}-{idx}",
                            ),
                            capture_time=cap,
                            processing_status=common_pb2.PROCESSING_STATUS_PROCESSED,
                            observations=obs,
                            decode_duration_ns=5_000_000,
                            cv_duration_ns=1_000_000,
                        ),
                    )
                )
                if idx == 1:
                    seq += 1
                    rw.write_edge_message(
                        online_pb2.BusMessage(
                            bus_sequence_no=seq,
                            ray_event=online_pb2.RayEvent(
                                envelope=common_pb2.EventEnvelope(
                                    event_type=common_pb2.EVENT_TYPE_RAY,
                                    station_id=st,
                                    station_session_id=f"sess-{st}",
                                    sequence_no=idx + 1,
                                ),
                                frame_id=f"{st}-{idx}",
                                local_track_id="t1",
                                capture_time=cap,
                                ray_origin_enu_m=common_pb2.Vec3(
                                    x=-10.0 if st == "s1" else 10.0, y=0.0, z=10.0
                                ),
                                ray_direction_enu_unit=common_pb2.Vec3(
                                    x=0.2 if st == "s1" else -0.2, y=1.0, z=0.0
                                ),
                            ),
                        )
                    )

        rw.write_center_message(
            online_pb2.BusMessage(
                bus_sequence_no=1,
                fusion_measurement=online_pb2.FusionMeasurement(
                    center_session_id="c1",
                    measurement_id="m1",
                    clock_domain_id="clk",
                    world_frame_id="enu",
                    reference_time_ns=16_000_000_000,
                    status=common_pb2.FUSION_STATUS_LOCATED,
                    position_enu_m=common_pb2.Vec3(x=0.0, y=50.0, z=10.0),
                ),
            )
        )
        for c_seq, ns in enumerate((0, 16_000_000_000, 17_000_000_000), start=2):
            rw.write_center_message(
                online_pb2.BusMessage(
                    bus_sequence_no=c_seq,
                    progress_report=online_pb2.ProgressReport(
                        source_role=common_pb2.COMPONENT_ROLE_CENTER,
                        source_id="center",
                        session_id="c1",
                        completed_time_ns=ns,
                        completed_sequence_no=c_seq - 1,
                        finite_end_reached=(c_seq == 4),
                    ),
                )
            )
    return scene_dir, run_dir, truth_frames


class OnlineScorerTests(unittest.TestCase):
    def test_online_subscribe_matches_offline_edge_center_and_integrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir, run_dir, truth_frames = _build_fixture(root)
            run = RunOutputPackage.open(run_dir)
            edge_msgs = list(run.iter_edge_events())
            center_msgs = list(run.iter_center_output())

            mode_cases: list[tuple[str, Sequence[online_pb2.BusMessage], str]] = [
                ("edge", edge_msgs, "15.0"),
                ("center", center_msgs, "0.0"),
                ("integrated", [*edge_msgs, *center_msgs], "15.0"),
            ]

            for mode, bus_stream, warmup_arg in mode_cases:
                with self.subTest(mode=mode):
                    offline_out = root / f"offline-{mode}"
                    online_out = root / f"online-{mode}"
                    with contextlib.redirect_stdout(io.StringIO()):
                        main(
                            [
                                "score",
                                "--scene",
                                str(scene_dir),
                                "--run",
                                str(run_dir),
                                "--output",
                                str(offline_out),
                                "--mode",
                                mode,
                                "--size-k-m",
                                "0.7",
                                "--warmup-s",
                                warmup_arg,
                            ]
                        )

                    received_scores: list[truth_pb2.ScoreEvent] = []

                    def server_handler(
                        conn: socket.socket,
                        stream: Sequence[online_pb2.BusMessage] = bus_stream,
                        out_scores: list[truth_pb2.ScoreEvent] = received_scores,
                    ) -> None:
                        reader = conn.makefile("rb")
                        try:
                            hello = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                            assert hello is not None
                            self.assertEqual(hello.WhichOneof("payload"), "bus_message")
                            self.assertEqual(
                                hello.bus_message.WhichOneof("payload"), "session_hello"
                            )
                            self.assertEqual(
                                hello.bus_message.session_hello.role,
                                common_pb2.COMPONENT_ROLE_SCORER,
                            )
                            conn.sendall(
                                encode_length_prefixed(
                                    truth_pb2.TestFrameworkMessage(
                                        sequence_no=1,
                                        bus_message=online_pb2.BusMessage(
                                            bus_sequence_no=1,
                                            session_ack=online_pb2.SessionAck(
                                                accepted=True,
                                                run_id="run-online-1",
                                            ),
                                        ),
                                    )
                                )
                            )

                            def drain_scores() -> None:
                                while True:
                                    msg = read_length_prefixed(
                                        reader, truth_pb2.TestFrameworkMessage
                                    )
                                    if msg is None:
                                        break
                                    self.assertEqual(msg.WhichOneof("payload"), "score_event")
                                    out_scores.append(msg.score_event)

                            drain_thread = threading.Thread(target=drain_scores, daemon=True)
                            drain_thread.start()

                            tf_msg = truth_pb2.TestFrameworkMessage(
                                sequence_no=2, truth_frame=truth_frames[0]
                            )
                            conn.sendall(encode_length_prefixed(tf_msg))
                            for tf_seq, bmsg in enumerate(stream, start=3):
                                conn.sendall(
                                    encode_length_prefixed(
                                        truth_pb2.TestFrameworkMessage(
                                            sequence_no=tf_seq,
                                            bus_message=bmsg,
                                        )
                                    )
                                )
                            conn.shutdown(socket.SHUT_WR)
                            drain_thread.join(timeout=5.0)
                        finally:
                            reader.close()

                    port, thread, errors = _start_tcp_server(server_handler)
                    with contextlib.redirect_stdout(io.StringIO()):
                        main(
                            [
                                "subscribe",
                                "--host",
                                "127.0.0.1",
                                "--port",
                                str(port),
                                "--scene",
                                str(scene_dir),
                                "--output",
                                str(online_out),
                                "--mode",
                                mode,
                                "--size-k-m",
                                "0.7",
                                "--warmup-s",
                                warmup_arg,
                            ]
                        )
                    thread.join(timeout=5.0)
                    self.assertEqual(errors, [])

                    # Verify scene integrity remains untouched.
                    TestScenePackage.open(scene_dir, verify_hashes=True)

                    offline_report = json.loads(
                        (offline_out / "report.json").read_text(encoding="utf-8")
                    )
                    online_report = json.loads(
                        (online_out / "report.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(online_report, offline_report)

                    offline_scores = [
                        e.SerializeToString()
                        for e in iter_pb_zst(offline_out / "scores.pb.zst", truth_pb2.ScoreEvent)
                    ]
                    persisted_online_scores = [
                        e.SerializeToString()
                        for e in iter_pb_zst(online_out / "scores.pb.zst", truth_pb2.ScoreEvent)
                    ]
                    streamed_online_scores = [
                        e.SerializeToString()
                        for e in sorted(
                            received_scores,
                            key=lambda item: (item.experiment_time_ns, item.error_unit),
                        )
                    ]
                    self.assertEqual(persisted_online_scores, offline_scores)
                    self.assertEqual(streamed_online_scores, offline_scores)

    def test_online_scorer_boundary_and_error_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir, _, truth_frames = _build_fixture(root)
            scene = TestScenePackage.open(scene_dir)

            # 1. Output dir must never point into read-only Test Scene.
            def accept_only(conn: socket.socket) -> None:
                reader = conn.makefile("rb")
                try:
                    _ = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                finally:
                    reader.close()

            port0, t0, _ = _start_tcp_server(accept_only)
            with (
                OnlineScorerClient("127.0.0.1", port0, timeout_s=5.0) as client,
                self.assertRaisesRegex(ValueError, "read-only Test Scene"),
            ):
                OnlineScorerSubscriber(client, scene, output_dir=scene_dir)
            t0.join(timeout=5.0)

            # 2. Rejected handshake raises ScorerHandshakeError.
            def reject_handshake(conn: socket.socket) -> None:
                reader = conn.makefile("rb")
                try:
                    _ = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=1,
                                bus_message=online_pb2.BusMessage(
                                    bus_sequence_no=1,
                                    session_ack=online_pb2.SessionAck(
                                        accepted=False, reason="denied"
                                    ),
                                ),
                            )
                        )
                    )
                finally:
                    reader.close()

            port1, t1, _ = _start_tcp_server(reject_handshake)
            with (
                OnlineScorerClient("127.0.0.1", port1, timeout_s=5.0) as client,
                self.assertRaisesRegex(ScorerHandshakeError, "denied"),
            ):
                OnlineScorerSubscriber(client, scene, mode="edge").run()
            t1.join(timeout=5.0)

            # 3. Conflicting TruthFrame over TCP is rejected.
            bad_tf = truth_pb2.TruthFrame()
            bad_tf.CopyFrom(truth_frames[0])
            bad_tf.targets[0].position_enu_m.x = 999.0

            def send_conflicting_truth(conn: socket.socket) -> None:
                reader = conn.makefile("rb")
                try:
                    _ = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=1,
                                bus_message=online_pb2.BusMessage(
                                    bus_sequence_no=1,
                                    session_ack=online_pb2.SessionAck(accepted=True, run_id="r1"),
                                ),
                            )
                        )
                    )
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(sequence_no=2, truth_frame=bad_tf)
                        )
                    )
                    conn.shutdown(socket.SHUT_WR)
                finally:
                    reader.close()

            port2, t2, _ = _start_tcp_server(send_conflicting_truth)
            with (
                OnlineScorerClient("127.0.0.1", port2, timeout_s=5.0) as client,
                self.assertRaisesRegex(ValueError, "conflicting TruthFrame"),
            ):
                OnlineScorerSubscriber(client, scene, mode="center").run()
            t2.join(timeout=5.0)

            # 4. Inbound ScoreEvent received by subscriber is rejected.
            def send_inbound_score(conn: socket.socket) -> None:
                reader = conn.makefile("rb")
                try:
                    _ = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=1,
                                bus_message=online_pb2.BusMessage(
                                    bus_sequence_no=1,
                                    session_ack=online_pb2.SessionAck(accepted=True, run_id="r1"),
                                ),
                            )
                        )
                    )
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=2,
                                score_event=truth_pb2.ScoreEvent(run_id="r1", error_unit="m"),
                            )
                        )
                    )
                    conn.shutdown(socket.SHUT_WR)
                finally:
                    reader.close()

            port3, t3, _ = _start_tcp_server(send_inbound_score)
            with (
                OnlineScorerClient("127.0.0.1", port3, timeout_s=5.0) as client,
                self.assertRaisesRegex(ValueError, "inbound ScoreEvent"),
            ):
                OnlineScorerSubscriber(client, scene, mode="center").run()
            t3.join(timeout=5.0)

    def test_center_scorer_ignores_zero_time_finite_end_after_positive_scene_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scene_dir = Path(directory) / "scene"
            with TestSceneWriter(
                scene_dir,
                scenario_id="empty-end-1",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=1_000_000_000,
                end_time_ns=2_000_000_000,
                stations=[],
            ):
                pass
            scene = TestScenePackage.open(scene_dir)
            scorer = CenterIncrementalScorer(scene, run_id="empty-run", warmup_s=0.0)
            events = scorer.feed_bus_message(
                online_pb2.BusMessage(
                    bus_sequence_no=1,
                    progress_report=online_pb2.ProgressReport(
                        source_role=common_pb2.COMPONENT_ROLE_CENTER,
                        source_id="center-1",
                        session_id="sess-1",
                        completed_time_ns=0,
                        finite_end_reached=True,
                    ),
                )
            )
            self.assertEqual(events, [])
            remaining_events, _report, errors = scorer.finalize()
            self.assertEqual(remaining_events, [])
            self.assertEqual(errors, [])

    def test_online_subscribe_unlabelled_scene_reports_unavailable_preserving_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir, run_dir, _truth_frames = _build_fixture(root)
            # Rebuild an unlabelled copy of the scene with zero-record truth.pb.zst.
            unlabelled_scene = root / "unlabelled_scene"
            with TestSceneWriter(
                unlabelled_scene,
                scenario_id="online-unlabelled",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=0,
                end_time_ns=20_000_000_000,
                stations=[],
            ) as sw:
                for entry in TestScenePackage.open(scene_dir).iter_frame_index():
                    sw.add_frame_index(entry)

            run = RunOutputPackage.open(run_dir)
            edge_msgs = list(run.iter_edge_events())
            received_scores: list[truth_pb2.ScoreEvent] = []

            def server_handler(conn: socket.socket) -> None:
                reader = conn.makefile("rb")
                try:
                    _ = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                    conn.sendall(
                        encode_length_prefixed(
                            truth_pb2.TestFrameworkMessage(
                                sequence_no=1,
                                bus_message=online_pb2.BusMessage(
                                    bus_sequence_no=1,
                                    session_ack=online_pb2.SessionAck(
                                        accepted=True, run_id="run-unlabelled"
                                    ),
                                ),
                            )
                        )
                    )

                    def drain_scores() -> None:
                        while True:
                            msg = read_length_prefixed(reader, truth_pb2.TestFrameworkMessage)
                            if msg is None:
                                break
                            received_scores.append(msg.score_event)

                    t = threading.Thread(target=drain_scores, daemon=True)
                    t.start()
                    for seq, bmsg in enumerate(edge_msgs, start=2):
                        conn.sendall(
                            encode_length_prefixed(
                                truth_pb2.TestFrameworkMessage(sequence_no=seq, bus_message=bmsg)
                            )
                        )
                    conn.shutdown(socket.SHUT_WR)
                    t.join(timeout=5.0)
                finally:
                    reader.close()

            port, thread, errors = _start_tcp_server(server_handler)
            out_dir = root / "online_unlabelled_out"
            with contextlib.redirect_stdout(io.StringIO()):
                main(
                    [
                        "subscribe",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--scene",
                        str(unlabelled_scene),
                        "--output",
                        str(out_dir),
                        "--mode",
                        "edge",
                        "--warmup-s",
                        "0",
                    ]
                )
            thread.join(timeout=5.0)
            self.assertEqual(errors, [])
            self.assertEqual(received_scores, [])
            report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "unavailable")
            self.assertIn("truth_unavailable", report["reason_codes"])
            self.assertEqual(report["available_outputs"]["edge"]["detection_batches"], 6)
