"""Durable progress and cooperative limits for recorded device evaluations."""

from __future__ import annotations

import json
import math
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvaluationBudget:
    candidate_limit: int = 100
    overload_hold_s: float = 10.0
    phase_limit_s: float = 2.0

    def __post_init__(self) -> None:
        if type(self.candidate_limit) is not int or self.candidate_limit < 1:
            raise ValueError("candidate_limit must be a positive integer")
        for value in (self.overload_hold_s, self.phase_limit_s):
            if not math.isfinite(value) or value < 0:
                raise ValueError("hold and phase limits must be finite and nonnegative")
        if self.phase_limit_s == 0:
            raise ValueError("phase_limit_s must be positive")

    def skip_tracking(self, count: int, elapsed_ns: int) -> bool:
        if count <= self.candidate_limit:
            return False
        if elapsed_ns < round(self.overload_hold_s * 1e9):
            return True
        raise EvaluationLimit("candidate_limit")


class EvaluationLimit(RuntimeError):
    """A declared evaluation budget was exceeded; no complete quality score exists."""


def write_json(path: Path, value: Any) -> None:
    """Replace a checkpoint atomically so a killed worker leaves valid JSON."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class EvaluationAttempt:
    def __init__(self, output: Path, budget: EvaluationBudget, stations: list[str]) -> None:
        self.output = output
        self.budget = budget
        self.began = time.monotonic()
        self.state: dict[str, Any] = {
            "budget": asdict(budget),
            "processed_frames": dict.fromkeys(stations, 0),
            "detected_frames": dict.fromkeys(stations, 0),
            "scored_frames": dict.fromkeys(stations, 0),
            "overload_suppressed_frames": dict.fromkeys(stations, 0),
            "last_valid_frame": None,
            "last_detected_frame": None,
            "current_frame": None,
            "phase": "initialization",
            "candidate_peak": 0,
            "rss_peak_bytes": 0,
        }
        self.phase("initialization")

    def phase(self, name: str) -> None:
        self.state["phase"] = name
        self.state["phase_started_monotonic"] = time.monotonic()
        self.checkpoint()

    def checkpoint(self) -> None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.state["rss_peak_bytes"] = int(peak if sys.platform == "darwin" else peak * 1024)
        self.state["wall_s"] = time.monotonic() - self.began
        write_json(self.output / "progress.json", self.state)

    def check_phase(self, duration_ms: float) -> None:
        if duration_ms > self.budget.phase_limit_s * 1000:
            raise EvaluationLimit("phase_time_limit")
