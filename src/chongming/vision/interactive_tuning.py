"""Interactive tuning workbench with cv2 trackbars."""

import argparse
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

import cv2
import numpy as np

from chongming.contracts import device as d
from chongming.vision.blob import BlobExtractionConfig, BlobExtractionStage
from chongming.vision.mog2 import MOG2Config, MOG2Stage
from chongming.vision.morphology import MorphologicalConfig, MorphologicalFilterStage
from chongming.vision.motion import FrameDiffConfig, FrameDifferenceStage
from chongming.vision.occupancy import OccupancyConfig, OccupancyFilterStage
from chongming.vision.pipeline import PipelineContext, Stage, process_stages
from chongming.vision.preprocess import PreprocessConfig, PreprocessStage
from chongming.vision.scoring import WorkbenchScoring
from chongming.vision.tracking import TrackingConfig, TrackingStage


class TuningParameters(TypedDict):
    mode: str
    threshold: float
    downsample: int
    morph_kernel: int
    apply_opening: bool
    apply_occupancy: bool
    mog2_history: int
    mog2_variance_threshold: float
    mog2_learning_rate: float
    mog2_detect_shadows: bool
    score_inc: float
    score_decay: float
    occ_threshold: float
    occ_dilate: int
    min_area: float
    blob_dilation: int
    confirm_frames: int
    max_lost: int
    assoc_gate: float
    show: int


class InteractivePipeline:
    """Interactive parameter tuning workbench."""

    def __init__(self, video_path: Path) -> None:
        self.cap = cv2.VideoCapture(str(video_path))
        if not self.cap.isOpened():
            raise ValueError("cannot open video")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(self.fps) or self.fps <= 0:
            self.cap.release()
            raise ValueError("video FPS must be finite and positive")
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # Load all frames into memory for smooth playback
        print(f"Loading {self.total_frames} frames into memory...")
        self.frames = []
        while True:
            ret, frame = self.cap.read()
            if not ret:
                break
            self.frames.append(frame)
        self.cap.release()
        print(f"Loaded {len(self.frames)} frames")
        if not self.frames:
            raise ValueError("video contains no decodable frames")

        self.export_template: Path | None = None
        self.export_station: str | None = None
        self.export_directory: Path | None = None
        self.export_index = 0
        self.zoom_point: tuple[int, int] | None = None
        self.scoring: WorkbenchScoring | None = None
        self.tracking_config = TrackingConfig()
        self.maximum_area: float | None = None
        self.apply_closing = False
        self.current_frame_idx = 0
        self.paused = False

        # Create window and trackbars
        cv2.namedWindow("Pipeline Tuning")
        cv2.namedWindow("Controls")
        cv2.namedWindow("MOG2")

        # Frame control
        cv2.createTrackbar("Frame", "Controls", 0, len(self.frames) - 1, self._on_frame_change)
        cv2.createTrackbar("Paused", "Controls", 0, 1, self._on_pause_change)

        # Motion detection
        cv2.createTrackbar(
            "Mode", "Controls", 1, 3, lambda x: None
        )  # 0=two, 1=three, 2=five, 3=MOG2
        cv2.createTrackbar("Threshold", "Controls", 20, 100, lambda x: None)
        cv2.createTrackbar("Downsample", "Controls", 1, 4, lambda x: None)  # 1-4x

        cv2.createTrackbar("MOG History", "MOG2", 500, 2000, lambda x: None)
        cv2.createTrackbar("MOG Variance", "MOG2", 16, 100, lambda x: None)
        cv2.createTrackbar(
            "MOG LR x10000", "MOG2", 0, 10001, lambda x: None
        )  # 0=auto, n=(n-1)/10000
        cv2.createTrackbar("MOG Shadows", "MOG2", 0, 1, lambda x: None)
        cv2.createTrackbar("Use Occupancy", "Controls", 1, 1, lambda x: None)

        # Morphology
        cv2.createTrackbar("Morph Kernel", "Controls", 3, 15, lambda x: None)
        cv2.createTrackbar("Apply Opening", "Controls", 1, 1, lambda x: None)

        # Occupancy
        cv2.createTrackbar("Score Inc", "Controls", 10, 50, lambda x: None)  # /10
        cv2.createTrackbar("Score Decay", "Controls", 10, 50, lambda x: None)  # /10
        cv2.createTrackbar("Occ Threshold", "Controls", 50, 200, lambda x: None)
        cv2.createTrackbar("Occ Dilate", "Controls", 5, 20, lambda x: None)  # Dilation kernel size

        # Blob extraction
        cv2.createTrackbar("Min Area", "Controls", 10, 200, lambda x: None)
        cv2.createTrackbar("Blob Dilation", "Controls", 5, 20, lambda x: None)

        # Tracking
        cv2.createTrackbar("Confirm Frames", "Controls", 3, 10, lambda x: None)
        cv2.createTrackbar("Max Lost", "Controls", 10, 50, lambda x: None)
        cv2.createTrackbar("Assoc Gate", "Controls", 50, 200, lambda x: None)

        # Visualization
        cv2.createTrackbar(
            "Show", "Controls", 0, 3, lambda x: None
        )  # 0=result, 1=motion, 2=filtered, 3=occupancy

        # Initialize stages (will be recreated on param change)
        self.stages: list[Stage] = []
        self.last_params: dict[str, Any] | None = None
        self.last_processed = -1
        self.context: PipelineContext | None = None

    def _on_frame_change(self, value: int) -> None:
        self.current_frame_idx = value

    def _on_pause_change(self, value: int) -> None:
        self.paused = bool(value)

    def _get_params(self) -> TuningParameters:
        """Read current parameters from trackbars."""
        mode_idx = cv2.getTrackbarPos("Mode", "Controls")
        mode = ["two_frame", "three_frame", "five_frame", "mog2"][mode_idx]

        raw_downsample = cv2.getTrackbarPos("Downsample", "Controls")
        downsample = 1 if raw_downsample <= 1 else 2 if raw_downsample == 2 else 4
        if raw_downsample != downsample:
            cv2.setTrackbarPos("Downsample", "Controls", downsample)
        return {
            "mode": mode,
            "apply_occupancy": bool(cv2.getTrackbarPos("Use Occupancy", "Controls")),
            "mog2_history": max(1, cv2.getTrackbarPos("MOG History", "MOG2")),
            "mog2_variance_threshold": float(max(1, cv2.getTrackbarPos("MOG Variance", "MOG2"))),
            "mog2_learning_rate": (cv2.getTrackbarPos("MOG LR x10000", "MOG2") - 1) / 10000
            if cv2.getTrackbarPos("MOG LR x10000", "MOG2")
            else -1.0,
            "mog2_detect_shadows": bool(cv2.getTrackbarPos("MOG Shadows", "MOG2")),
            "threshold": float(cv2.getTrackbarPos("Threshold", "Controls")),
            "downsample": downsample,
            "morph_kernel": cv2.getTrackbarPos("Morph Kernel", "Controls") | 1,  # Force odd
            "apply_opening": bool(cv2.getTrackbarPos("Apply Opening", "Controls")),
            "score_inc": cv2.getTrackbarPos("Score Inc", "Controls") / 10.0,
            "score_decay": cv2.getTrackbarPos("Score Decay", "Controls") / 10.0,
            "occ_threshold": float(cv2.getTrackbarPos("Occ Threshold", "Controls")),
            "occ_dilate": cv2.getTrackbarPos("Occ Dilate", "Controls"),
            "min_area": float(cv2.getTrackbarPos("Min Area", "Controls")),
            "blob_dilation": cv2.getTrackbarPos("Blob Dilation", "Controls"),
            "confirm_frames": cv2.getTrackbarPos("Confirm Frames", "Controls"),
            "max_lost": cv2.getTrackbarPos("Max Lost", "Controls"),
            "assoc_gate": float(cv2.getTrackbarPos("Assoc Gate", "Controls")),
            "show": cv2.getTrackbarPos("Show", "Controls"),
        }

    def _rebuild_pipeline(self, params: Mapping[str, Any]) -> None:
        """Rebuild pipeline stages with new parameters."""
        self.stages = [
            PreprocessStage(PreprocessConfig(downsample_factor=params["downsample"])),
            MOG2Stage(
                MOG2Config(
                    params["mog2_history"],
                    params["mog2_variance_threshold"],
                    params["mog2_learning_rate"],
                    params["mog2_detect_shadows"],
                )
            )
            if params["mode"] == "mog2"
            else FrameDifferenceStage(
                FrameDiffConfig(
                    mode=params["mode"], threshold=params["threshold"], use_binarize=True
                )
            ),
            MorphologicalFilterStage(
                MorphologicalConfig(
                    kernel_size=params["morph_kernel"],
                    apply_opening=params["apply_opening"],
                    apply_closing=self.apply_closing,
                )
            ),
            OccupancyFilterStage(
                OccupancyConfig(
                    enabled=params["apply_occupancy"],
                    score_increment=params["score_inc"],
                    score_decay=params["score_decay"],
                    mask_threshold=params["occ_threshold"],
                    dilate_background_mask=params["occ_dilate"] > 0,
                    dilation_kernel_size=params["occ_dilate"],
                )
            ),
            BlobExtractionStage(
                BlobExtractionConfig(
                    min_area_px2=params["min_area"],
                    max_area_px2=self.maximum_area,
                    apply_dilation=params["blob_dilation"] > 0,
                    dilation_kernel_size=params["blob_dilation"],
                )
            ),
            TrackingStage(
                replace(
                    self.tracking_config,
                    confirm_frames=params["confirm_frames"],
                    max_lost_frames=params["max_lost"],
                    association_gate_px=params["assoc_gate"],
                )
            ),
        ]

    def configure(
        self, detection: d.FrameDifferencePolicy, tracking: d.KalmanTrackingPolicy
    ) -> None:
        """Apply an explicit offline/device profile to the same interactive stages."""
        self.maximum_area = detection.maximum_component_area_px2
        self.apply_closing = detection.apply_closing
        self.tracking_config = TrackingConfig(
            covariance_floor=tracking.covariance_floor_px,
            process_noise_pos=tracking.process_noise_position,
            process_noise_vel=tracking.process_noise_velocity,
            measurement_noise=tracking.measurement_noise_px,
        )
        controls = {
            "Mode": (["two_frame", "three_frame", "five_frame", "mog2"].index(detection.mode), 3),
            "Use Occupancy": (int(detection.apply_occupancy), 1),
            "Threshold": (detection.threshold, 100),
            "Downsample": (detection.downsample_factor, 4),
            "Morph Kernel": (detection.morphology_kernel_size, 15),
            "Apply Opening": (int(detection.apply_opening), 1),
            "Score Inc": (detection.score_increment * 10, 50),
            "Score Decay": (detection.score_decay * 10, 50),
            "Occ Threshold": (detection.mask_threshold, 200),
            "Occ Dilate": (detection.occupancy_dilation_size, 20),
            "Min Area": (detection.minimum_component_area_px2, 200),
            "Blob Dilation": (detection.blob_dilation_size, 20),
            "Confirm Frames": (tracking.confirmation_frame_count, 10),
            "Max Lost": (tracking.maximum_lost_frame_count, 50),
            "Assoc Gate": (tracking.association_gate_px, 200),
        }
        controls.update(
            {
                "MOG History": (
                    500 if detection.mog2_history is None else detection.mog2_history,
                    2000,
                ),
                "MOG Variance": (
                    16
                    if detection.mog2_variance_threshold is None
                    else detection.mog2_variance_threshold,
                    100,
                ),
                "MOG LR x10000": (
                    0
                    if detection.mog2_learning_rate in (None, -1)
                    else 1 + detection.mog2_learning_rate * 10000,
                    10001,
                ),
                "MOG Shadows": (int(bool(detection.mog2_detect_shadows)), 1),
            }
        )
        for name, (value, maximum) in controls.items():
            if not 0 <= value <= maximum or abs(value - round(value)) > 1e-8:
                raise ValueError(f"{name} cannot be represented by the workbench slider")
            cv2.setTrackbarPos(
                name, "MOG2" if name.startswith("MOG ") else "Controls", round(value)
            )

    def process_until(self, index: int) -> PipelineContext:
        """Train once per frame, replaying history after a backward seek."""
        if not 0 <= index < len(self.frames):
            raise ValueError("frame index outside video")
        if index < self.last_processed:
            for stage in self.stages:
                stage.reset()
            self.last_processed = -1
            if self.scoring is not None:
                self.scoring.reset()
        for current in range(self.last_processed + 1, index + 1):
            self.context = PipelineContext(
                frame=self.frames[current],
                timestamp=(
                    self.scoring.times[current] if self.scoring is not None else current / self.fps
                ),
            )
            process_stages(self.stages, self.context)
            if self.scoring is not None:
                scale = self.last_params["downsample"] if self.last_params is not None else 1
                self.scoring.observe(
                    current,
                    [
                        tuple(v * scale for v in detection.measurement_px)
                        for detection in self.context.detections
                    ],
                )
            self.last_processed = current
        assert self.context is not None
        return self.context

    def export_parameters(self) -> Path:
        """Write a new station-policy snapshot; never overwrite an experiment."""
        if (
            self.export_template is None
            or self.export_directory is None
            or self.export_station is None
        ):
            raise ValueError("export requires --policy, --station and --export-directory")
        document = json.loads(self.export_template.read_text())
        profile = document[self.export_station]
        params = dict(self._get_params())
        detection = profile["detection"]
        for field, control in {
            "apply_occupancy": "apply_occupancy",
            "mode": "mode",
            "threshold": "threshold",
            "downsample_factor": "downsample",
            "morphology_kernel_size": "morph_kernel",
            "apply_opening": "apply_opening",
            "score_increment": "score_inc",
            "score_decay": "score_decay",
            "mask_threshold": "occ_threshold",
            "occupancy_dilation_size": "occ_dilate",
            "minimum_component_area_px2": "min_area",
            "blob_dilation_size": "blob_dilation",
        }.items():
            detection[field] = params[control]
        detection["algorithm_id"] = (
            "mog2-workbench-v1" if params["mode"] == "mog2" else "frame-difference-occupancy-v1"
        )
        for field in (
            "mog2_history",
            "mog2_variance_threshold",
            "mog2_learning_rate",
            "mog2_detect_shadows",
        ):
            detection[field] = params[field] if params["mode"] == "mog2" else None
        for field, control in {
            "confirmation_frame_count": "confirm_frames",
            "maximum_lost_frame_count": "max_lost",
            "association_gate_px": "assoc_gate",
        }.items():
            profile["tracking"][field] = params[control]
        self.export_directory.mkdir(parents=True, exist_ok=True)
        while True:
            destination = self.export_directory / f"workbench-policy-{self.export_index:03d}.json"
            self.export_index += 1
            if not destination.exists():
                break
        with destination.open("x") as handle:
            handle.write(json.dumps(document, indent=2, allow_nan=False) + "\n")
        return destination

    def run(self) -> None:
        """Main loop."""
        print("\nControls:")
        print("  Space: Pause/Resume")
        print("  Q/Esc: Quit; S: Export parameter snapshot (with --export-directory)")
        print("  Mode: 0=two-frame, 1=three-frame, 2=five-frame, 3=MOG2")
        print("  MOG2 controls are in the MOG2 window; Use Occupancy is optional")
        print("  Use trackbars to adjust parameters in real-time")

        while True:
            params = self._get_params()
            algorithm_params = {key: value for key, value in params.items() if key != "show"}

            # Rebuild pipeline if params changed
            if algorithm_params != self.last_params:
                self._rebuild_pipeline(params)
                self.last_params = algorithm_params
                self.last_processed = -1
                self.context = None
                if self.scoring is not None:
                    self.scoring.reset()
                # Replay the new algorithm up to the selected frame; keep the view stable.
                print(
                    f"\nParams updated: mode={params['mode']}, threshold={params['threshold']}, "
                    f"downsample={params['downsample']}x"
                )

            # Get current frame
            if self.current_frame_idx >= len(self.frames):
                self.current_frame_idx = 0

            frame = self.frames[self.current_frame_idx]

            try:
                context = self.process_until(self.current_frame_idx)
            except Exception as error:
                logging.getLogger(__name__).exception("Pipeline error")
                vis = frame.copy()
                cv2.putText(
                    vis,
                    f"Pipeline Error: {str(error)[:60]}",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                )
                cv2.imshow("Pipeline Tuning", vis)
                cv2.destroyAllWindows()
                raise

            # Select visualization
            show_mode = params["show"]
            if show_mode == 1 and context.motion_mask is not None:
                vis = cv2.cvtColor(context.motion_mask, cv2.COLOR_GRAY2BGR)
            elif show_mode == 2 and context.filtered_mask is not None:
                vis = cv2.cvtColor(context.filtered_mask, cv2.COLOR_GRAY2BGR)
            elif show_mode == 3:
                # Visualize occupancy map (score heatmap)
                occ_stage = self.stages[3]  # OccupancyFilterStage
                if hasattr(occ_stage, "score_map") and occ_stage.score_map is not None:
                    # Normalize to 0-255 for visualization
                    occ_vis = occ_stage.score_map.copy()
                    # Clip to threshold range for better contrast
                    occ_threshold = params["occ_threshold"]
                    # Clamp score map values to prevent overflow (max 1000)
                    occ_vis = np.clip(occ_vis, 0, 1000)
                    occ_vis = np.clip(occ_vis * (255.0 / max(occ_threshold, 1.0)), 0, 255).astype(
                        np.uint8
                    )
                    vis = cv2.applyColorMap(occ_vis, cv2.COLORMAP_JET)

                    # Add threshold line indicator and stats
                    max_score = float(np.clip(occ_stage.score_map.max(), 0, 10000))
                    mean_score = float(np.clip(occ_stage.score_map.mean(), 0, 10000))
                    cv2.putText(
                        vis,
                        f"Occupancy: max={max_score:.1f} mean={mean_score:.2f} thr={occ_threshold:.0f}",
                        (10, 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (255, 255, 255),
                        2,
                    )
                else:
                    vis = frame.copy()
                    cv2.putText(
                        vis,
                        "Occupancy disabled"
                        if not params["apply_occupancy"]
                        else "Occupancy stage not initialized",
                        (10, 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),
                        2,
                    )
            else:
                # Result view
                vis = frame.copy()

                # Get downsample factor to scale coordinates back to original resolution
                downsample = params["downsample"]

                # Draw blobs with AABB
                for blob in context.blobs:
                    # Draw bounding box (x, y, w, h format)
                    x, y, w, h = blob.bounding_box
                    x1, y1 = int(x * downsample), int(y * downsample)
                    x2, y2 = int((x + w) * downsample), int((y + h) * downsample)
                    cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 165, 0), 2)

                    # Draw centroid
                    cx, cy = (
                        int(blob.centroid_px[0] * downsample),
                        int(blob.centroid_px[1] * downsample),
                    )
                    cv2.circle(vis, (cx, cy), 5, (255, 165, 0), -1)

                    # Draw area label
                    cv2.putText(
                        vis,
                        f"{int(blob.area_px2)}",
                        (x1, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (255, 165, 0),
                        1,
                    )

                # Draw tracked detections with AABB and lifetime
                for det in context.detections:
                    # Get tracker stage to access lifetime info
                    tracker_stage = self.stages[-1]  # TrackingStage is last
                    lifetime = 0
                    if hasattr(tracker_stage, "tracks"):
                        for track in tracker_stage.tracks:
                            if track.track_id == det.track_id:
                                lifetime = track.lifetime_frames
                                break

                    # Draw bounding box (fixed size around position)
                    cx, cy = (
                        int(det.measurement_px[0] * downsample),
                        int(det.measurement_px[1] * downsample),
                    )
                    box_size = 30
                    cv2.rectangle(
                        vis,
                        (cx - box_size, cy - box_size),
                        (cx + box_size, cy + box_size),
                        (0, 255, 0),
                        2,
                    )

                    # Draw center point
                    cv2.circle(vis, (cx, cy), 5, (0, 255, 0), -1)

                    # Draw ID and lifetime
                    cv2.putText(
                        vis,
                        f"ID{det.track_id} [{lifetime}f]",
                        (cx - box_size, cy - box_size - 5),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 0),
                        2,
                    )

            if self.scoring is not None and show_mode == 0:
                self.scoring.draw(
                    vis,
                    self.current_frame_idx,
                    max(1, cv2.getTrackbarPos("Score Floor px", "Controls")),
                    max(1, cv2.getTrackbarPos("Score k x100", "Controls")) / 100,
                )

            stage_ms = context.debug_data["stage_timings_ms"]
            cv2.putText(
                vis,
                f"Algorithm {sum(stage_ms.values()):.2f} ms (no decode / scoring / UI)",
                (10, 85),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (255, 255, 255),
                1,
            )

            # Overlay info
            cv2.putText(
                vis,
                f"Frame {self.current_frame_idx}/{len(self.frames)}  "
                f"Blobs:{len(context.blobs)} Tracks:{len(context.detections)}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 255),
                2,
            )
            cv2.putText(
                vis,
                f"{params['mode']} thr={params['threshold']:.0f} ds={params['downsample']}x",
                (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2,
            )

            cv2.imshow("Pipeline Tuning", vis)
            if self.scoring is not None:
                cv2.imshow(
                    "Scoring",
                    self.scoring.panel(
                        frame,
                        self.current_frame_idx,
                        max(1, cv2.getTrackbarPos("Score Floor px", "Controls")),
                        max(1, cv2.getTrackbarPos("Score k x100", "Controls")) / 100,
                        [
                            (
                                b.centroid_px[0] * params["downsample"],
                                b.centroid_px[1] * params["downsample"],
                            )
                            for b in context.blobs
                        ],
                        self.zoom_point,
                        context.debug_data["stage_timings_ms"],
                    ),
                )

            # Update trackbar position if not paused
            if not self.paused:
                if self.current_frame_idx == len(self.frames) - 1:
                    self.paused = True
                    cv2.setTrackbarPos("Paused", "Controls", 1)
                else:
                    self.current_frame_idx += 1
                cv2.setTrackbarPos("Frame", "Controls", self.current_frame_idx)

            # Handle keyboard
            key = cv2.waitKey(33) & 0xFF  # ~30fps
            if key == ord("q") or key == 27:  # Q or Esc
                break
            elif key in (ord("a"), ord("d")):
                self.paused = True
                self.current_frame_idx = max(
                    0,
                    min(len(self.frames) - 1, self.last_processed + (1 if key == ord("d") else -1)),
                )
                cv2.setTrackbarPos("Paused", "Controls", 1)
                cv2.setTrackbarPos("Frame", "Controls", self.current_frame_idx)
            elif key == ord("s") and self.export_directory is not None:
                print(f"Saved parameters: {self.export_parameters()}")
            elif key == ord(" "):  # Space
                self.paused = not self.paused
                cv2.setTrackbarPos("Paused", "Controls", int(self.paused))

        cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path, help="Path to video file")
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--export-directory", type=Path, help="New snapshots written on S")
    parser.add_argument("--recording", type=Path, help="Recorded truth for scoring overlay")
    parser.add_argument("--size-k", type=float, default=0.5)
    parser.add_argument("--gate-px", type=float, default=10.0)
    parser.add_argument("--policy", type=Path, help="Explicit station-to-image-policy JSON")
    parser.add_argument("--station", help="Station key in the policy JSON")
    args = parser.parse_args()
    if bool(args.policy) != bool(args.station):
        parser.error("--policy and --station must be supplied together")
    parameters: tuple[d.FrameDifferencePolicy, d.KalmanTrackingPolicy] | None = None
    if args.policy:
        from chongming.evaluate_recording import ImagePolicy
        from chongming.modules.workbench_device import (
            validate_workbench_detection,
            validate_workbench_tracking,
        )
        from chongming.scenarios.json_content import parse_json
        from chongming.scenarios.serialization import _decode

        profiles: dict[str, ImagePolicy] = _decode(
            parse_json(args.policy.read_bytes()), dict[str, ImagePolicy]
        )
        profile = profiles[args.station]
        if not isinstance(profile.detection, d.FrameDifferencePolicy) or not isinstance(
            profile.tracking, d.KalmanTrackingPolicy
        ):
            parser.error("the interactive workbench requires frame-difference/Kalman policies")
        validate_workbench_detection(profile.detection)
        validate_workbench_tracking(profile.tracking)
        parameters = (profile.detection, profile.tracking)
    if args.export_directory and not args.policy:
        parser.error("--export-directory requires --policy and --station")
    if args.recording and not args.station:
        parser.error("--recording requires --station and --policy")
    if not 0 < args.size_k <= 2 or abs(args.size_k * 100 - round(args.size_k * 100)) > 1e-8:
        parser.error("--size-k must be 0.01..2 in 0.01 steps")
    if not 1 <= args.gate_px <= 100 or args.gate_px != round(args.gate_px):
        parser.error("--gate-px must be an integer in 1..100")
    workbench = InteractivePipeline(args.video)
    try:
        workbench.export_template = args.policy
        workbench.export_station = args.station
        workbench.export_directory = args.export_directory
        if parameters is not None:
            workbench.configure(*parameters)
        if args.recording:
            workbench.scoring = WorkbenchScoring(args.recording, args.station, args.video)
            if len(workbench.scoring.frames) != len(workbench.frames):
                raise ValueError("video and scoring frame counts differ")
            cv2.createTrackbar(
                "Score k x100", "Controls", round(args.size_k * 100), 200, lambda x: None
            )
            cv2.createTrackbar(
                "Score Floor px", "Controls", round(args.gate_px), 100, lambda x: None
            )
        if not 0 <= args.start_frame < len(workbench.frames):
            parser.error("--start-frame outside video")
        workbench.current_frame_idx = args.start_frame
        workbench.paused = args.paused
        cv2.setTrackbarPos("Frame", "Controls", args.start_frame)
        cv2.setTrackbarPos("Paused", "Controls", int(args.paused))

        def inspect_pixels(event: int, x: int, y: int, flags: int, data: Any) -> None:
            if event == cv2.EVENT_LBUTTONDOWN:
                factor = (
                    workbench.last_params["downsample"]
                    if workbench.last_params and cv2.getTrackbarPos("Show", "Controls") != 0
                    else 1
                )
                workbench.zoom_point = (x * factor, y * factor)

        cv2.setMouseCallback("Pipeline Tuning", inspect_pixels)
        workbench.run()
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
