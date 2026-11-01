"""Interactive tuning workbench with cv2 trackbars."""

import argparse
import logging
from pathlib import Path

import cv2
import numpy as np

from chongming.vision.blob import BlobExtractionConfig, BlobExtractionStage
from chongming.vision.morphology import MorphologicalConfig, MorphologicalFilterStage
from chongming.vision.motion import FrameDiffConfig, FrameDifferenceStage
from chongming.vision.occupancy import OccupancyConfig, OccupancyFilterStage
from chongming.vision.pipeline import PipelineContext
from chongming.vision.preprocess import PreprocessConfig, PreprocessStage
from chongming.vision.tracking import TrackingConfig, TrackingStage

logger = logging.getLogger(__name__)


class InteractivePipeline:
    """Interactive parameter tuning workbench."""

    def __init__(self, video_path: Path):
        self.cap = cv2.VideoCapture(str(video_path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
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

        self.current_frame_idx = 0
        self.paused = False

        # Create window and trackbars
        cv2.namedWindow("Pipeline Tuning")
        cv2.namedWindow("Controls")

        # Frame control
        cv2.createTrackbar("Frame", "Controls", 0, len(self.frames) - 1, self._on_frame_change)
        cv2.createTrackbar("Paused", "Controls", 0, 1, self._on_pause_change)

        # Motion detection
        cv2.createTrackbar("Mode", "Controls", 1, 2, lambda x: None)  # 0=two, 1=three, 2=five
        cv2.createTrackbar("Threshold", "Controls", 20, 100, lambda x: None)
        cv2.createTrackbar("Downsample", "Controls", 1, 4, lambda x: None)  # 1-4x

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
        self.stages = []
        self.last_params = None

    def _on_frame_change(self, value: int) -> None:
        self.current_frame_idx = value

    def _on_pause_change(self, value: int) -> None:
        self.paused = bool(value)

    def _get_params(self) -> dict:
        """Read current parameters from trackbars."""
        mode_idx = cv2.getTrackbarPos("Mode", "Controls")
        mode = ["two_frame", "three_frame", "five_frame"][mode_idx]

        return {
            "mode": mode,
            "threshold": float(cv2.getTrackbarPos("Threshold", "Controls")),
            "downsample": cv2.getTrackbarPos("Downsample", "Controls"),
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

    def _rebuild_pipeline(self, params: dict) -> None:
        """Rebuild pipeline stages with new parameters."""
        self.stages = [
            PreprocessStage(PreprocessConfig(downsample_factor=params["downsample"])),
            FrameDifferenceStage(
                FrameDiffConfig(
                    mode=params["mode"], threshold=params["threshold"], use_binarize=True
                )
            ),
            MorphologicalFilterStage(
                MorphologicalConfig(
                    kernel_size=params["morph_kernel"],
                    apply_opening=params["apply_opening"],
                    apply_closing=False,
                )
            ),
            OccupancyFilterStage(
                OccupancyConfig(
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
                    apply_dilation=params["blob_dilation"] > 0,
                    dilation_kernel_size=params["blob_dilation"],
                )
            ),
            TrackingStage(
                TrackingConfig(
                    confirm_frames=params["confirm_frames"],
                    max_lost_frames=params["max_lost"],
                    association_gate_px=params["assoc_gate"],
                )
            ),
        ]

    def run(self) -> None:
        """Main loop."""
        print("\nControls:")
        print("  Space: Pause/Resume")
        print("  Q/Esc: Quit")
        print("  Use trackbars to adjust parameters in real-time")

        while True:
            params = self._get_params()

            # Rebuild pipeline if params changed
            if params != self.last_params:
                self._rebuild_pipeline(params)
                self.last_params = params
                print(
                    f"\nParams updated: mode={params['mode']}, threshold={params['threshold']}, "
                    f"downsample={params['downsample']}x"
                )

            # Get current frame
            if self.current_frame_idx >= len(self.frames):
                self.current_frame_idx = 0

            frame = self.frames[self.current_frame_idx]
            timestamp = self.current_frame_idx / self.fps

            # Process through pipeline
            context = PipelineContext(frame=frame, timestamp=timestamp)
            try:
                for stage in self.stages:
                    stage.process(context)

                    # Flow protection: skip tracking if too many blobs
                    # This prevents workbench freeze when occupancy hasn't learned yet
                    if hasattr(context, "blobs") and len(context.blobs) > 50:
                        # Clear blobs to prevent tracking stage from processing
                        print(
                            f"WARNING: {len(context.blobs)} blobs detected, skipping tracking (occupancy learning)"
                        )
                        context.blobs = []
                        context.detections = []
                        break

            except Exception as e:
                logger.exception("Pipeline error")
                # Show error on screen
                vis = frame.copy()
                cv2.putText(
                    vis,
                    f"Pipeline Error: {str(e)[:60]}",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                )
                cv2.imshow("Pipeline Tuning", vis)
                key = cv2.waitKey(33) & 0xFF
                if key == ord("q") or key == 27:
                    break
                continue

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
                        "Occupancy stage not initialized (wait for first frame)",
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
                        int(det.position_px[0] * downsample),
                        int(det.position_px[1] * downsample),
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

            # Flow protection warning (if blobs were truncated)
            if (
                len(context.blobs) == 0
                and hasattr(context, "filtered_mask")
                and context.filtered_mask is not None
                and context.filtered_mask.any()
            ):
                cv2.putText(
                    vis,
                    "FLOW PROTECTION: Skipping tracking (occupancy learning...)",
                    (10, 90),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 165, 255),
                    2,
                )

            cv2.imshow("Pipeline Tuning", vis)

            # Update trackbar position if not paused
            if not self.paused:
                self.current_frame_idx = (self.current_frame_idx + 1) % len(self.frames)
                cv2.setTrackbarPos("Frame", "Controls", self.current_frame_idx)

            # Handle keyboard
            key = cv2.waitKey(33) & 0xFF  # ~30fps
            if key == ord("q") or key == 27:  # Q or Esc
                break
            elif key == ord(" "):  # Space
                self.paused = not self.paused
                cv2.setTrackbarPos("Paused", "Controls", int(self.paused))

        cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path, help="Path to video file")
    args = parser.parse_args()

    workbench = InteractivePipeline(args.video)
    workbench.run()


if __name__ == "__main__":
    main()
