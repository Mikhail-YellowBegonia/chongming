"""End-to-end tests for one-way conversion from legacy JSONL recordings."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from chongming.contracts import scene as s
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import save_compiled_scenario
from chongming_scorer.cli import main as scorer_main
from chongming_scorer.storage import RunOutputPackage, TestScenePackage
from scripts.convert_legacy_recording import convert_run_output, convert_test_scene
from tests.test_scene_rendering import test_scene


class LegacyConverterTests(unittest.TestCase):
    def test_convert_legacy_recording_and_score_end_to_end(self) -> None:
        spec = test_scene(s.SphereGeometry(1), (0.0, 50.0, 10.0))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_rec = root / "legacy-rec"
            legacy_dev = root / "legacy-dev"
            legacy_ctr = root / "legacy-ctr"
            scene_dir = root / "test-scene"
            run_dir = root / "run-output"

            legacy_rec.mkdir()
            (legacy_rec / "video").mkdir()
            video_file = legacy_rec / "video" / "camera-000.mp4"
            video_file.write_bytes(b"mp4-bytes")
            save_compiled_scenario(spec, legacy_rec / "scenario.json")

            station_id = str(spec.station_configs[0].station_id)
            digest = str(spec.station_configs[0].config_digest)
            (legacy_rec / "manifest.json").write_text(
                json.dumps(
                    {
                        "stream_mapping": [
                            {
                                "station_id": station_id,
                                "camera_role": "main",
                                "path": "video/camera-000.mp4",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (legacy_rec / "video.json").write_text(
                json.dumps(
                    {
                        "streams": {
                            "camera-000": {
                                "path": "video/camera-000.mp4",
                                "frame_count": 2,
                                "sha256": hashlib.sha256(b"mp4-bytes").hexdigest(),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            times = (1_000_000, 34_333_333)
            online_rows = []
            truth_rows = []
            obs_rows = []
            eval_rows = []
            for idx, ns in enumerate(times):
                cap = {
                    "clock_domain_id": spec.timeline.clock_domain.clock_domain_id,
                    "exposure_start_ns": ns,
                    "exposure_end_ns": ns,
                    "clock_uncertainty_ns": 0,
                    "rolling_shutter_model": "none",
                }
                fid = f"{station_id}:main:{idx}"
                online_rows.append(
                    {
                        "sample_time": {"time_ns": ns, "step_no": idx + 1},
                        "frames": [
                            {
                                "station_id": station_id,
                                "session_id": "sess-1",
                                "camera_role": "main",
                                "frame_id": fid,
                                "frame_index": idx,
                                "capture_time": cap,
                                "camera_config_ref": digest,
                                "path": "video/camera-000.mp4",
                                "width_px": 1920,
                                "height_px": 1080,
                            }
                        ],
                        "packets": {"rays": []},
                    }
                )
                truth_rows.append(
                    {
                        "sample_time": {
                            "clock_domain_id": spec.timeline.clock_domain.clock_domain_id,
                            "time_ns": ns,
                            "step_no": idx + 1,
                        },
                        "logical_scene": {
                            "flying_objects": [
                                {
                                    "truth_object_id": str(
                                        spec.scene_definition.flying_objects[0].truth_object_id
                                    ),
                                    "model_name": "sphere",
                                    "state": {
                                        "position_enu_m": [0.0, 50.0, 10.0],
                                        "velocity_enu_mps": [0.0, 0.0, 0.0],
                                    },
                                }
                            ]
                        },
                        "camera_frames": [
                            {
                                "station_id": str(st.station_id),
                                "camera_role": "main",
                                "frame_id": (fid if idx_st == 0 else f"{st.station_id}:main:{idx}"),
                                "targets": [
                                    {
                                        "truth_object_id": str(
                                            spec.scene_definition.flying_objects[0].truth_object_id
                                        ),
                                        "projected_width_px": 12.0,
                                        "projected_height_px": 16.0,
                                        "projection": {
                                            "center_in_field_of_view": True,
                                            "centroid_px": [960.0, 540.0],
                                        },
                                        "visibility": {
                                            "occlusions": [],
                                            "sight_segment": {
                                                "optical_center_enu_m": list(
                                                    st.main_camera_position_enu_m
                                                ),
                                                "reference_point_enu_m": [0.0, 50.0, 10.0],
                                            },
                                        },
                                    }
                                ],
                            }
                            for idx_st, st in enumerate(spec.station_configs)
                        ],
                    }
                )
                obs_rows.append(
                    {
                        "frame_id": fid,
                        "observations": [
                            {
                                "main_frame_ref": {
                                    "station_id": station_id,
                                    "station_session_id": "sess-1",
                                    "camera_role": "main",
                                    "frame_id": fid,
                                },
                                "capture_time": cap,
                                "centroid_px": [960.0, 540.0],
                                "detection_score": 1.0,
                                "detection_score_definition": {
                                    "definition_id": "fg-v1",
                                    "kind": "ranking",
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                                "local_track_id": "1",
                            }
                        ],
                    }
                )
                eval_rows.append(
                    {
                        "frame_id": fid,
                        "timing_ms": {"decode_ms": 2.0, "pipeline_ms": 0.5},
                    }
                )

            (legacy_rec / "online.jsonl").write_text(
                "".join(_json(r) + "\n" for r in online_rows), encoding="utf-8"
            )
            (legacy_rec / "truth.jsonl").write_text(
                "".join(_json(r) + "\n" for r in truth_rows), encoding="utf-8"
            )
            (legacy_dev / "evaluation").mkdir(parents=True)
            (legacy_dev / "observations.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in obs_rows), encoding="utf-8"
            )
            (legacy_dev / "evaluation" / "frames.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in eval_rows), encoding="utf-8"
            )
            legacy_ctr.mkdir()
            (legacy_ctr / "center-registration.json").write_text(
                json.dumps({"center_session_id": "c-1"}), encoding="utf-8"
            )
            (legacy_ctr / "center-input.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "progress": [
                                {
                                    "completed_capture_time": {
                                        "exposure_start_ns": ns,
                                        "exposure_end_ns": ns,
                                    }
                                }
                            ]
                        }
                    )
                    + "\n"
                    for ns in times
                ),
                encoding="utf-8",
            )
            (legacy_ctr / "center-output.jsonl").write_text(
                json.dumps(
                    {
                        "batches": [
                            {
                                "window_update": {
                                    "sealed_windows": [
                                        {
                                            "key": {"start_ns": 0, "end_ns": 20_000_000},
                                            "reference_time_ns": 10_000_000,
                                        },
                                        {
                                            "key": {"start_ns": 20_000_000, "end_ns": 40_000_000},
                                            "reference_time_ns": 30_000_000,
                                        },
                                    ]
                                }
                            }
                        ],
                        "measurements": [
                            {
                                "center_session_id": "c-1",
                                "measurement_id": "m-1",
                                "clock_domain_id": spec.timeline.clock_domain.clock_domain_id,
                                "world_frame_id": "demo-enu",
                                "reference_time_ns": times[0],
                                "support_exposure_span_ns": 0,
                                "status": "located",
                                "position_enu_m": [0.0, 50.0, 10.0],
                            }
                        ],
                        "tracks": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            convert_test_scene(legacy_rec, scene_dir, link_video=True)
            scene = TestScenePackage.open(scene_dir, verify_hashes=True, verify_videos=True)
            self.assertEqual(len(list(scene.iter_frame_index())), 2)
            self.assertEqual(len(list(scene.iter_truth_frames())), 2)

            convert_run_output(
                scene_dir,
                run_dir,
                mode="integrated",
                device_dir=legacy_dev,
                center_dir=legacy_ctr,
                run_id="conv-1",
            )
            run = RunOutputPackage.open(run_dir, verify_hashes=True)
            run.verify_matches_scene(scene)

            scorer_main(
                [
                    "score",
                    "--scene",
                    str(scene_dir),
                    "--run",
                    str(run_dir),
                    "--mode",
                    "integrated",
                    "--warmup-s",
                    "0",
                    "--minimum-stations",
                    "2",
                ]
            )
            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["edge"]["aggregate"]["tp"], 2)
            self.assertEqual(report["edge"]["aggregate"]["fn"], 0)
            self.assertEqual(report["edge"]["timings"]["aggregate"]["decode_ms"]["mean"], 2.0)
            self.assertEqual(report["edge"]["timings"]["aggregate"]["cv_ms"]["mean"], 0.5)
            self.assertEqual(report["center"]["metrics"]["tp"], 1)
            self.assertEqual(report["center"]["metrics"]["fn"], 1)
