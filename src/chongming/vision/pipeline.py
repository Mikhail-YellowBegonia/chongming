"""Core pipeline abstractions for modular CV processing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class PipelineContext:
    """Data container passed between pipeline stages.

    Stages read inputs and write outputs to this shared context.
    """

    # Input
    frame: np.ndarray  # Current frame (BGR, uint8)
    timestamp: float  # Seconds

    # Intermediate results (populated by stages)
    preprocessed: np.ndarray | None = None  # Preprocessed frame
    motion_mask: np.ndarray | None = None  # Binary motion mask (uint8, 0 or 255)
    filtered_mask: np.ndarray | None = None  # After background filtering
    blobs: list[Blob] = field(default_factory=list)  # Connected components
    detections: list[Detection] = field(default_factory=list)  # Final tracked targets

    # Debug outputs (for visualization)
    debug_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Blob:
    """A connected component extracted from motion mask."""

    centroid_px: tuple[float, float]  # (x, y)
    area_px2: float
    bounding_box: tuple[int, int, int, int]  # (x, y, w, h)


@dataclass
class Detection:
    """A confirmed tracked target."""

    track_id: int
    position_px: tuple[float, float]  # (x, y)
    velocity_pxps: tuple[float, float] | None  # (vx, vy) pixels per second
    timestamp: float


class Stage(ABC):
    """Base class for a processing stage in the pipeline."""

    @abstractmethod
    def process(self, context: PipelineContext) -> None:
        """Process the current frame. Results are written to context."""
        ...

    def reset(self) -> None:
        """Reset internal state (called when switching video streams)."""


class Pipeline:
    """A sequence of processing stages."""

    def __init__(self, stages: list[Stage]) -> None:
        self.stages = stages

    def process_frame(self, frame: np.ndarray, timestamp: float) -> list[Detection]:
        """Process one frame through all stages.

        Args:
            frame: BGR image (uint8)
            timestamp: Capture time in seconds

        Returns:
            List of detected targets
        """
        context = PipelineContext(frame=frame, timestamp=timestamp)
        for stage in self.stages:
            stage.process(context)
        return context.detections

    def reset(self) -> None:
        """Reset all stages (e.g., when starting a new video)."""
        for stage in self.stages:
            stage.reset()
