"""Adaptive per-pixel Gaussian-mixture background subtraction for the workbench."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from chongming.vision.pipeline import PipelineContext, Stage


@dataclass(frozen=True)
class MOG2Config:
    history: int
    variance_threshold: float
    learning_rate: float
    detect_shadows: bool

    def __post_init__(self) -> None:
        if type(self.history) is not int or self.history < 1:
            raise ValueError("MOG2 history must be a positive integer")
        if not math.isfinite(self.variance_threshold) or self.variance_threshold <= 0:
            raise ValueError("MOG2 variance threshold must be finite and positive")
        if not math.isfinite(self.learning_rate) or not (
            self.learning_rate == -1 or 0 <= self.learning_rate <= 1
        ):
            raise ValueError("MOG2 learning rate must be -1 (auto) or 0..1")
        if type(self.detect_shadows) is not bool:
            raise ValueError("MOG2 shadow switch must be boolean")


class MOG2Stage(Stage):
    """Emit the same binary motion-mask contract as FrameDifferenceStage."""

    def __init__(self, config: MOG2Config):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.model = cv2.createBackgroundSubtractorMOG2(
            history=self.config.history,
            varThreshold=self.config.variance_threshold,
            detectShadows=self.config.detect_shadows,
        )

    def process(self, context: PipelineContext) -> None:
        if context.preprocessed is None:
            raise ValueError("MOG2 requires a preprocessed image")
        image = context.preprocessed
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        raw = self.model.apply(gray, learningRate=self.config.learning_rate)
        # OpenCV encodes shadows separately (normally 127); only 255 is foreground.
        context.motion_mask = np.where(raw == 255, 255, 0).astype(np.uint8)
        context.debug_data["motion_mask"] = context.motion_mask
