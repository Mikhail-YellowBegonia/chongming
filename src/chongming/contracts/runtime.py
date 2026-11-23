"""Online module lifecycle and health declarations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chongming.contracts.model import ProcessingTime, StationId


class ModuleState(StrEnum):
    DECLARED = "declared"
    INITIALIZING = "initializing"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class ModuleNode:
    """Construction dependencies are not staggered module activation stages."""

    module_key: str
    dependencies: tuple[str, ...]
    station_id: StationId | None


@dataclass(frozen=True, kw_only=True)
class ModuleHealth:
    module_key: str
    state: ModuleState
    observed_at: ProcessingTime
    reason_codes: tuple[str, ...]
