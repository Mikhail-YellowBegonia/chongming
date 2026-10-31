"""Explicit clean-ray reference plugin; all inputs and composition are online-only."""

from __future__ import annotations

from chongming.composition.center import CenterAssembly, declare_center
from chongming.contracts.development import (
    CenterAlgorithmResult,
    CenterInputStep,
    CenterRegistration,
)
from chongming.contracts.model import ProcessingTime, SessionId
from chongming.ports.fusion import FusionEngine
from chongming.ports.lifecycle import ModuleBinding
from chongming.voxel_diagnostics import VoxelDiagnostics


class FusionEngineAdapter:
    """Preserve stage diagnostics as well as measurements and lifecycle events."""

    def __init__(self, engine: FusionEngine) -> None:
        self.engine = engine

    def start(self, registration: CenterRegistration) -> None:
        for station in registration.stations:
            result = self.engine.register_station(station)
            if result.status.value not in {"registered", "already_registered"}:
                raise ValueError(f"station registration failed: {result.reason_codes}")

    def process(self, step: CenterInputStep) -> CenterAlgorithmResult:
        for solution in step.solutions:
            result = self.engine.register_calibration(solution)
            if result.status.value not in {"registered", "already_registered"}:
                raise ValueError(f"calibration registration failed: {result.reason_codes}")
        batches = [self.engine.process(event) for event in step.events]
        batches.extend(self.engine.report_progress(progress) for progress in step.progress)
        batches.append(self.engine.tick(step.time))
        return CenterAlgorithmResult(
            measurements=tuple(
                measurement for batch in batches for measurement in batch.measurements
            ),
            tracks=tuple(track for batch in batches for track in batch.tracks),
            batches=tuple(batches),
        )

    def close(self) -> None:
        close_engine = getattr(self.engine, "close", None)
        if close_engine is not None:
            close_engine()


class ReferenceFusionAlgorithm:
    def __init__(self) -> None:
        self.voxel_diagnostics = VoxelDiagnostics()
        self.center: CenterAssembly | None = None
        self._adapter: FusionEngineAdapter | None = None
        self._started: list[ModuleBinding] = []
        self._now: ProcessingTime | None = None

    def start(self, registration: CenterRegistration) -> None:
        if self.center is not None:
            raise RuntimeError("reference algorithm already started")
        self._now = registration.started_at
        self.center, bindings = declare_center(
            center_session_id=SessionId(registration.center_session_id),
            policy=registration.policy,
            clock_mapping=registration.clock_mapping,
            voxel_diagnostics=self.voxel_diagnostics,
        )
        try:
            for binding in bindings:
                self._started.append(binding)
                binding.module.start_module(binding.node.module_key, registration.started_at)
            self._adapter = FusionEngineAdapter(self.center.fusion)
            self._adapter.start(registration)
        except BaseException:
            self.close()
            raise

    def process(self, step: CenterInputStep) -> CenterAlgorithmResult:
        if self._adapter is None:
            raise RuntimeError("reference algorithm is not running")
        self._now = step.time
        self.voxel_diagnostics.clear()
        result = self._adapter.process(step)
        last_window = next(
            (
                window.key
                for batch in reversed(result.batches)
                for window in reversed(batch.window_update.sealed_windows)
            ),
            None,
        )
        # A last window without unguided voxel search must clear an earlier grid,
        # including when other windows in this invocation did run voxel search.
        if self.voxel_diagnostics.window != last_window:
            self.voxel_diagnostics.clear()
        return result

    def close(self) -> None:
        if self._adapter is not None:
            self._adapter.close()
        if self._now is not None:
            for binding in reversed(self._started):
                binding.module.stop_module(binding.node.module_key, self._now)
        self._started.clear()
        self._adapter = None
        self.voxel_diagnostics.clear()


def create_algorithm() -> ReferenceFusionAlgorithm:
    """Factory for --algorithm chongming.reference:create_algorithm."""
    return ReferenceFusionAlgorithm()
