"""Morphological filtering to remove noise and fill gaps."""

from __future__ import annotations

from dataclasses import dataclass

import cv2

from chongming.vision.pipeline import PipelineContext, Stage


@dataclass
class MorphologicalConfig:
    """Configuration for morphological filtering."""

    kernel_size: int = 3  # Size of structuring element
    apply_opening: bool = False  # Remove small noise (erosion + dilation) - can erode small targets
    apply_closing: bool = False  # Fill small gaps (dilation + erosion)


class MorphologicalFilterStage(Stage):
    """Morphological filtering to clean up motion mask.

    Opening (erosion + dilation) removes isolated pixels from vegetation noise.
    Closing (dilation + erosion) fills small gaps in target blobs.
    """

    def __init__(self, config: MorphologicalConfig) -> None:
        self.config = config
        self.kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (config.kernel_size, config.kernel_size)
        )

    def process(self, context: PipelineContext) -> None:
        if context.motion_mask is None:
            raise ValueError("MorphologicalFilterStage requires motion_mask from previous stage")

        mask = context.motion_mask

        # Opening: remove small noise (like vegetation)
        if self.config.apply_opening:
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)

        # Closing: fill small gaps in targets
        if self.config.apply_closing:
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel)

        context.motion_mask = mask
        context.debug_data["morphological_filtered"] = mask
