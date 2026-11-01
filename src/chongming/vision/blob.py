"""Blob extraction: connected component analysis."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from chongming.vision.pipeline import Blob, PipelineContext, Stage


@dataclass
class BlobExtractionConfig:
    """Configuration for blob extraction."""

    min_area_px2: float = 10.0  # Minimum component area
    max_area_px2: float | None = None  # Maximum area (None = no limit)
    apply_dilation: bool = True  # Dilate before connected components (merge target fragments)
    dilation_kernel_size: int = 5  # Size of dilation kernel (5: better multi-target separation)


class BlobExtractionStage(Stage):
    """Extract connected components (blobs) from filtered mask.

    Optional dilation before extraction helps merge nearby pixels.
    """

    def __init__(self, config: BlobExtractionConfig) -> None:
        self.config = config
        self.dilation_kernel: np.ndarray | None
        if config.apply_dilation:
            self.dilation_kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (config.dilation_kernel_size, config.dilation_kernel_size)
            )
        else:
            self.dilation_kernel = None

    def process(self, context: PipelineContext) -> None:
        # Use filtered_mask if available, otherwise fall back to motion_mask
        if context.filtered_mask is not None:
            mask = context.filtered_mask
        elif context.motion_mask is not None:
            mask = context.motion_mask
        else:
            raise ValueError(
                "BlobExtractionStage requires either filtered_mask or motion_mask from previous stage"
            )

        # Optional dilation to merge nearby pixels
        working_mask = mask
        if self.config.apply_dilation and self.dilation_kernel is not None:
            working_mask = cv2.dilate(mask, self.dilation_kernel)

        # Connected component analysis
        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            working_mask, connectivity=8
        )

        blobs = []
        # Skip label 0 (background)
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]

            # Area filtering
            if area < self.config.min_area_px2:
                continue
            if self.config.max_area_px2 is not None and area > self.config.max_area_px2:
                continue

            centroid = (float(centroids[i, 0]), float(centroids[i, 1]))
            bbox = (
                int(stats[i, cv2.CC_STAT_LEFT]),
                int(stats[i, cv2.CC_STAT_TOP]),
                int(stats[i, cv2.CC_STAT_WIDTH]),
                int(stats[i, cv2.CC_STAT_HEIGHT]),
            )

            blobs.append(Blob(centroid_px=centroid, area_px2=float(area), bounding_box=bbox))

        context.blobs = blobs
        context.debug_data["blob_count"] = len(blobs)
        context.debug_data["dilated_mask"] = working_mask if self.config.apply_dilation else None
