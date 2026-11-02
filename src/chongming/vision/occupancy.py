"""Occupancy-ratio filtering to remove dynamic background (vegetation)."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from chongming.vision.pipeline import PipelineContext, Stage


@dataclass
class OccupancyConfig:
    """Configuration for occupancy-ratio filtering."""

    enabled: bool = True
    score_increment: float = 3.0  # Score added when pixel is marked as motion (very aggressive)
    score_decay: float = 0.1  # Score decayed per frame (subtraction, not multiplication)
    mask_threshold: float = 50.0  # Score above which pixel is masked as background
    min_score: float = 0.0  # Minimum score (prevent negative)
    dilate_background_mask: bool = True  # Dilate background mask to catch edges
    dilation_kernel_size: int = 5  # Size of dilation kernel for background mask


class OccupancyFilterStage(Stage):
    """Occupancy-ratio filtering to remove dynamic background.

    Pixels that frequently show motion (high occupancy ratio) are likely
    dynamic background (vegetation, flags) rather than flying targets.
    Accumulates a score map: increments when motion is detected, decays over time.
    """

    def __init__(self, config: OccupancyConfig) -> None:
        self.config = config
        self.score_map: np.ndarray | None = None
        self.dilation_kernel: np.ndarray | None
        if config.dilate_background_mask:
            self.dilation_kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (config.dilation_kernel_size, config.dilation_kernel_size)
            )
        else:
            self.dilation_kernel = None

    def process(self, context: PipelineContext) -> None:
        if context.motion_mask is None:
            raise ValueError("OccupancyFilterStage requires motion_mask from previous stage")

        if not self.config.enabled:
            context.filtered_mask = context.motion_mask.copy()
            context.debug_data["filtered_mask"] = context.filtered_mask
            return

        mask = context.motion_mask

        # Initialize score map on first frame
        if self.score_map is None:
            self.score_map = np.zeros(mask.shape, dtype=np.float32)

        # Update scores: increment where motion detected
        motion_pixels = mask > 0
        self.score_map[motion_pixels] += self.config.score_increment

        # Decay all scores
        self.score_map -= self.config.score_decay
        np.maximum(self.score_map, self.config.min_score, out=self.score_map)

        # Clamp to prevent overflow (max score = 1000)
        np.minimum(self.score_map, 1000.0, out=self.score_map)

        # Create background mask (high-score pixels)
        background_mask = self.score_map > self.config.mask_threshold

        # Dilate background mask to catch edges/borders of vegetation
        if self.config.dilate_background_mask and self.dilation_kernel is not None:
            background_mask_uint8 = (background_mask * 255).astype(np.uint8)
            dilated_mask = cv2.dilate(background_mask_uint8, self.dilation_kernel)
            background_mask = dilated_mask > 0

        # Remove background from motion mask
        filtered_mask = mask.copy()
        filtered_mask[background_mask] = 0

        context.filtered_mask = filtered_mask
        context.debug_data["occupancy_scores"] = self.score_map.copy()
        context.debug_data["background_mask"] = (background_mask * 255).astype(np.uint8)
        context.debug_data["filtered_mask"] = filtered_mask

    def reset(self) -> None:
        """Clear accumulated score map."""
        self.score_map = None
