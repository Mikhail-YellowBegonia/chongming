"""Tests for immutable Test Scene and independent Run Output package storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.storage import (
    RunOutputPackage,
    RunOutputWriter,
    TestScenePackage,
    TestSceneWriter,
)


def _sample_station() -> online_pb2.StationConfig:
    return online_pb2.StationConfig(
        station_id="s1",
        config_version="v1",
        config_digest="cfg-1",
        world_frame_id="enu",
        main_camera_position_enu_m=common_pb2.Vec3(x=0.0, y=0.0, z=10.0),
        baseline_orientation_enu_from_main=common_pb2.Mat3(
            values=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        ),
    )


class ScorerStorageTests(unittest.TestCase):
    def test_test_scene_immutability_tamper_detection_and_path_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_video = root / "cam.mp4"
            raw_video.write_bytes(b"mp4-fixture-bytes")
            scene_dir = root / "scene"

            with TestSceneWriter(
                scene_dir,
                scenario_id="sc-1",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=0,
                end_time_ns=1_000_000,
                stations=[_sample_station()],
                random_seed=42,
            ) as writer:
                writer.add_video_file(raw_video, "video/s1-main.mp4", {"fps": 30}, symlink=True)
                writer.add_frame_index(
                    truth_pb2.FrameIndexEntry(
                        station_id="s1",
                        station_session_id="sess-1",
                        camera_role=common_pb2.CAMERA_ROLE_MAIN,
                        frame_index=0,
                        frame_id="f-0",
                        capture_time=common_pb2.CaptureTime(
                            clock_domain_id="clk",
                            exposure_start_ns=0,
                            exposure_end_ns=0,
                        ),
                        station_config_digest="cfg-1",
                        video_relpath="video/s1-main.mp4",
                        width_px=64,
                        height_px=64,
                    )
                )
                writer.add_truth_frame(
                    truth_pb2.TruthFrame(
                        world_frame_id="enu",
                        experiment_time_ns=0,
                        step_index=0,
                    )
                )
                writer.add_virtual_ray(
                    online_pb2.RayEvent(
                        envelope=common_pb2.EventEnvelope(
                            event_type=common_pb2.EVENT_TYPE_RAY,
                            station_id="s1",
                            station_session_id="sess-1",
                            sequence_no=1,
                        ),
                        frame_id="f-0",
                    )
                )

            with self.assertRaises(FileExistsError):
                TestSceneWriter(
                    scene_dir,
                    scenario_id="sc-1",
                    world_frame_id="enu",
                    clock_domain_id="clk",
                    start_time_ns=0,
                    end_time_ns=1_000_000,
                    stations=[],
                )

            scene = TestScenePackage.open(scene_dir, verify_hashes=True, verify_videos=True)
            self.assertEqual(len(scene.station_configs()), 1)
            self.assertEqual(scene.station_configs()[0].station_id, "s1")
            self.assertEqual(len(list(scene.iter_frame_index())), 1)
            self.assertEqual(len(list(scene.iter_truth_frames())), 1)
            self.assertEqual(len(list(scene.iter_virtual_rays())), 1)

            # Tampering with a stream breaks hash verification.
            truth_bytes = (scene_dir / "truth.pb.zst").read_bytes()
            (scene_dir / "truth.pb.zst").write_bytes(truth_bytes + b"tamper")
            with self.assertRaisesRegex(ValueError, "sha256|byte count"):
                TestScenePackage.open(scene_dir, verify_hashes=True)
            (scene_dir / "truth.pb.zst").write_bytes(truth_bytes)

            # Path traversal inside manifest is rejected.
            manifest = json.loads((scene_dir / "manifest.json").read_text(encoding="utf-8"))
            manifest["files"]["../escape.pb.zst"] = {"sha256": "0" * 64, "bytes": 0}
            (scene_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "content_sha256"):
                TestScenePackage.open(scene_dir, verify_hashes=False)

    def test_run_output_scene_matching_voxels_and_score_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene_dir = root / "scene"
            run_dir = root / "run"
            alt_out = root / "alt-scores"

            with TestSceneWriter(
                scene_dir,
                scenario_id="sc-2",
                world_frame_id="enu",
                clock_domain_id="clk",
                start_time_ns=0,
                end_time_ns=1_000_000,
                stations=[],
            ):
                pass
            scene = TestScenePackage.open(scene_dir)

            with RunOutputWriter(
                run_dir,
                run_id="r-1",
                mode="center",
                test_scene_path=scene_dir,
                test_scene_sha256=scene.content_sha256,
            ) as rw:
                rw.write_center_message(
                    online_pb2.BusMessage(
                        bus_sequence_no=1,
                        progress_report=online_pb2.ProgressReport(
                            source_role=common_pb2.COMPONENT_ROLE_CENTER,
                            source_id="center",
                            session_id="c1",
                            completed_time_ns=0,
                            completed_sequence_no=1,
                            finite_end_reached=True,
                        ),
                    )
                )
                rw.write_voxel_grid(
                    online_pb2.SparseVoxelGrid(
                        center_session_id="c1",
                        world_frame_id="enu",
                        window_start_ns=0,
                        window_end_ns=1_000,
                        cells=[online_pb2.SparseVoxelCell(ix=1, iy=2, iz=3, station_votes=2)],
                    )
                )

            run = RunOutputPackage.open(run_dir)
            run.verify_matches_scene(scene)
            self.assertTrue(run.has_voxels)
            grids = list(run.iter_voxels())
            self.assertEqual(len(grids), 1)
            self.assertEqual(grids[0].cells[0].station_votes, 2)

            scores = [truth_pb2.ScoreEvent(run_id="r-1", experiment_time_ns=0, error_unit="m")]
            run.write_scores(scores, {"status": "complete"})
            self.assertTrue(run.has_scores)
            self.assertEqual(len(list(run.iter_scores())), 1)
            with self.assertRaises(FileExistsError):
                run.write_scores(scores, {"status": "complete"})

            run.write_scores(scores, {"status": "complete"}, output_dir=alt_out)
            self.assertTrue((alt_out / "scores.pb.zst").is_file())
            self.assertTrue((alt_out / "report.json").is_file())

            bad_run_dir = root / "bad-run"
            with RunOutputWriter(
                bad_run_dir,
                run_id="r-bad",
                mode="center",
                test_scene_path=scene_dir,
                test_scene_sha256="f" * 64,
            ):
                pass
            bad_run = RunOutputPackage.open(bad_run_dir)
            with self.assertRaisesRegex(ValueError, "test_scene_sha256"):
                bad_run.verify_matches_scene(scene)
