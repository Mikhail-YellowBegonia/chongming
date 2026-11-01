"""Basic preprocessing stages: downsampling and exposure adjustment."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from chongming.vision.pipeline import PipelineContext, Stage


@dataclass
class PreprocessConfig:
    """Configuration for preprocessing stage."""

    downsample_factor: int = 2  # 1 = no downsampling, 2 = half size (2x: acceptable quality, 4x performance boost)
    adjust_exposure: bool = False
    target_brightness: float = 128.0  # Target mean brightness (0-255)


class PreprocessStage(Stage):
    """Preprocessing: downsampling and optional exposure adjustment."""

    def __init__(self, config: PreprocessConfig) -> None:
        self.config = config

    def process(self, context: PipelineContext) -> None:
        frame = context.frame

        # Downsample
        if self.config.downsample_factor > 1:
            for _ in range(self.config.downsample_factor // 2):
                frame = cv2.pyrDown(frame)

        # Exposure adjustment (simple brightness normalization)
        if self.config.adjust_exposure:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            current_mean = np.mean(gray)
            if current_mean > 0:
                scale = self.config.target_brightness / current_mean
                frame = np.clip(frame * scale, 0, 255).astype(np.uint8)

        context.preprocessed = frame
        context.debug_data["preprocessed"] = frame
