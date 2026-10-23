"""Volunteer algorithm hook; registration and input have online-only types."""

from typing import Protocol

from chongming.contracts.development import (
    CenterInputStep,
    CenterRegistration,
    DeviceAlgorithmOutput,
    DeviceInputStep,
    DeviceRegistration,
)
from chongming.contracts.model import CameraFrame, FusionMeasurement, ProcessingTime, TrackEvent


class CenterAlgorithm(Protocol):
    def start(self, registration: CenterRegistration) -> None: ...

    def process(
        self, step: CenterInputStep
    ) -> tuple[tuple[FusionMeasurement, ...], tuple[TrackEvent, ...]]: ...

    def close(self) -> None: ...


class DeviceAlgorithm(Protocol):
    """One plugin may own independent state keyed by station and image session.

    Each available frame is dispatched immediately by role. Main callbacks do
    not wait for auxiliary frames or require online calibration to start.
    """

    def start(self, registration: DeviceRegistration) -> None: ...

    def process_main(self, frame: CameraFrame, now: ProcessingTime) -> DeviceAlgorithmOutput: ...

    def process_auxiliary(
        self, frame: CameraFrame, now: ProcessingTime
    ) -> DeviceAlgorithmOutput: ...

    def tick(self, step: DeviceInputStep) -> DeviceAlgorithmOutput:
        """Called after available frames, including no-image samples and finite end."""
        ...

    def close(self) -> None: ...
