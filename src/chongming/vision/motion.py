"""Motion detection stages: frame differencing variants."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from chongming.vision.pipeline import PipelineContext, Stage


@dataclass
class FrameDiffConfig:
    """Configuration for frame differencing."""

    mode: str = "two_frame"  # "two_frame", "three_frame", "five_frame"
    threshold: float = 2.0  # Binarization threshold (very low for high sensitivity with 2x downsample)
    use_binarize: bool = True


class FrameDifferenceStage(Stage):
    """Frame differencing for motion detection.

    Supports two-frame, three-frame, and five-frame differencing.
    Three-frame is more robust to noise and illumination changes.
    """

    def __init__(self, config: FrameDiffConfig) -> None:
        self.config = config
        self.frame_buffer: deque[np.ndarray] = deque(maxlen=self._buffer_size())

    def _buffer_size(self) -> int:
        """Required buffer size for the selected mode."""
        if self.config.mode == "two_frame":
            return 1
        elif self.config.mode == "three_frame":
            return 2
        elif self.config.mode == "five_frame":
            return 4
        else:
            raise ValueError(f"Unknown mode: {self.config.mode}")

    def process(self, context: PipelineContext) -> None:
        # Use preprocessed frame if available, otherwise original
        frame = context.preprocessed if context.preprocessed is not None else context.frame

        # Convert to grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Need enough history
        maxlen = self.frame_buffer.maxlen
        if maxlen is None or len(self.frame_buffer) < maxlen:
            self.frame_buffer.append(gray)
            # Output empty mask on first frames
            context.motion_mask = np.zeros_like(gray, dtype=np.uint8)
            return

        # Compute frame difference based on mode
        if self.config.mode == "two_frame":
            diff = self._two_frame_diff(gray)
        elif self.config.mode == "three_frame":
            diff = self._three_frame_diff(gray)
        elif self.config.mode == "five_frame":
            diff = self._five_frame_diff(gray)
        else:
            raise ValueError(f"Unknown mode: {self.config.mode}")

        # Binarization
        if self.config.use_binarize:
            _, motion_mask = cv2.threshold(diff, self.config.threshold, 255, cv2.THRESH_BINARY)
        else:
            motion_mask = diff

        context.motion_mask = motion_mask
        context.debug_data["motion_mask"] = motion_mask

        # Update buffer
        self.frame_buffer.append(gray)

    def _two_frame_diff(self, current: np.ndarray) -> np.ndarray:
        """Two-frame differencing: |frame(t) - frame(t-1)|"""
        prev = self.frame_buffer[-1]
        return cv2.absdiff(current, prev)

    def _three_frame_diff(self, current: np.ndarray) -> np.ndarray:
        """Three-frame differencing: |frame(t) - frame(t-1)| AND |frame(t) - frame(t-2)|"""
        prev1 = self.frame_buffer[-1]
        prev2 = self.frame_buffer[-2]
        diff1 = cv2.absdiff(current, prev1)
        diff2 = cv2.absdiff(current, prev2)
        return cv2.bitwise_and(diff1, diff2)

    def _five_frame_diff(self, current: np.ndarray) -> np.ndarray:
        """Five-frame differencing: combine multiple temporal differences."""
        # Compute differences with multiple frames
        diffs = []
        for i in range(1, 5):
            prev = self.frame_buffer[-i]
            diffs.append(cv2.absdiff(current, prev))

        # Combine: require motion in at least 2 of 4 comparisons
        result = diffs[0]
        for diff in diffs[1:]:
            result = cv2.bitwise_and(result, diff)
        return result

    def reset(self) -> None:
        """Clear frame buffer."""
        self.frame_buffer.clear()
