"""Small online-only replay verifies calibration sessions and warmup delivery."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from chongming.contracts import model as m
from chongming.modules.detection import FILL_RATIO_SCORE
from chongming.replay_integrated import replay
from chongming.scenarios.demo import demo_source
from chongming.scenarios.run import _json
from chongming.scenarios.serialization import load_scenario, save_compiled_scenario

ROOT = Path(__file__).resolve().parents[1]


class IntegratedReplayTests(unittest.TestCase):
    def test_calibration_rebinding_and_warmup_keep_image_evidence_separate_from_truth(self) -> None:
        spec = load_scenario(ROOT / "scenarios/center_clean.json")
        spec = replace(spec, timeline=replace(spec.timeline, end_time_ns=100_000_000))
        source = demo_source(spec, video_enabled=False)
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory)
            save_compiled_scenario(spec, recording / "scenario.json")
            (recording / "manifest.json").write_text(
                _json({"station_sessions": source.station_sessions})
            )
            online, observations = [], []
            for step in source.generate(spec):
                frames = []
                for truth in step.truth.camera_frames:
                    if truth.camera_role is not m.CameraRole.MAIN:
                        continue
                    frames.append(
                        {
                            "station_id": truth.station_id,
                            "session_id": truth.station_session_id,
                            "frame_id": truth.frame_id,
                            "camera_role": "main",
                            "capture_time": truth.capture_time,
                        }
                    )
                    values = tuple(
                        m.Observation2D(
                            main_frame_ref=m.FrameRef(
                                station_id=truth.station_id,
                                station_session_id=truth.station_session_id,
                                camera_role=truth.camera_role,
                                frame_id=truth.frame_id,
                            ),
                            capture_time=truth.capture_time,
                            centroid_px=target.projection.centroid_px,
                            centroid_covariance_px2=None,
                            detection_score=1,
                            detection_score_definition=FILL_RATIO_SCORE,
                            local_track_id=None,
                            track_quality=None,
                            track_quality_definition=None,
                        )
                        for target in truth.targets
                        if target.projection.center_in_field_of_view
                        and target.projection.centroid_px is not None
                    )
                    observations.append({"frame_id": truth.frame_id, "observations": values})
                online.append({"packets": step.packets, "frames": frames})
            (recording / "online.jsonl").write_text("".join(_json(row) + "\n" for row in online))
            detected = recording / "observations.jsonl"
            detected.write_text("".join(_json(row) + "\n" for row in observations))
            output = recording / "output"
            report = replay(
                recording,
                detected,
                output,
                asset_root=ROOT / "scenarios",
                pixel_sigma_px=5,
                warmup_s=0.02,
            )
            self.assertEqual(report["status"], "complete")
            with (output / "edge-rays.jsonl").open() as handle:
                rays = [json.loads(line) for line in handle]
            self.assertTrue(rays)
            self.assertTrue(all(row["time_ns"] >= 20_000_000 for row in rays))
            self.assertTrue(all(row["event"]["envelope"]["event_type"] == "ray" for row in rays))
            sessions = {
                str(binding.station_id): str(binding.image_session_id)
                for binding in source.station_sessions
            }
            self.assertTrue(
                all(
                    row["event"]["envelope"]["station_session_id"] == sessions[row["station_id"]]
                    for row in rays
                )
            )
            with (output / "center-output.jsonl").open() as handle:
                outputs = [json.loads(line) for line in handle]
            self.assertTrue(
                any(m["status"] == "located" for row in outputs for m in row["measurements"])
            )
            self.assertTrue(report["stage_ms_scored"]["geometry_ms"]["samples"])
