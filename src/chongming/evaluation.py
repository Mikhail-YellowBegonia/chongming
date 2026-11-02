"""Offline, responsibility-gated detection metrics. Truth never enters algorithms."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


def assignment(costs: list[list[float]], columns: int) -> list[int]:
    """Maximum cardinality, then minimum total error (residual augmenting paths)."""
    rows = [-1] * len(costs)
    owners = [-1] * columns
    for _ in range(min(len(rows), columns)):
        rd = [0.0 if column < 0 else math.inf for column in rows]
        cd = [math.inf] * columns
        previous = [-1] * columns
        for _ in range(len(rows) + columns):
            changed = False
            for i in range(len(rows)):
                for j in range(columns):
                    if rows[i] != j and rd[i] + costs[i][j] < cd[j]:
                        cd[j], previous[j], changed = rd[i] + costs[i][j], i, True
            for j, i in enumerate(owners):
                if i >= 0 and cd[j] - costs[i][j] < rd[i]:
                    rd[i], changed = cd[j] - costs[i][j], True
            if not changed:
                break
        ends = [j for j in range(columns) if owners[j] < 0 and math.isfinite(cd[j])]
        if not ends:
            break
        end = min(ends, key=lambda j: (cd[j], j))
        while end >= 0:
            i = previous[end]
            old = rows[i]
            rows[i], owners[end] = end, i
            end = old
    return rows


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


@dataclass
class DetectionMetrics:
    """One target-time opportunity per eligible target; duplicates count as FP."""

    gate: float
    unit: str
    frames: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    false_alarms: int = 0
    duplicates: int = 0
    ignored_predictions: int = 0
    per_target: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.gate) or self.gate <= 0:
            raise ValueError("matching gate must be finite and positive")

    def add(
        self,
        predictions: Sequence[tuple[float, ...]],
        targets: Mapping[str, tuple[float, ...]],
        *,
        ignored: Sequence[tuple[float, ...]] = (),
        target_gates: dict[str, float] | None = None,
        ignored_gates: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        ids = sorted(targets)
        points = [targets[key] for key in ids]
        gates = [self.gate if target_gates is None else target_gates[key] for key in ids]
        if any(not math.isfinite(g) or g <= 0 for g in gates):
            raise ValueError("target gates must be finite and positive")
        dimensions = {len(p) for p in [*predictions, *points, *ignored]}
        if len(dimensions) > 1 or any(
            not all(math.isfinite(x) for x in p) for p in [*predictions, *points, *ignored]
        ):
            raise ValueError("evaluation positions must have equal dimensions and finite values")
        ignore_limits = [self.gate] * len(ignored) if ignored_gates is None else list(ignored_gates)
        if len(ignore_limits) != len(ignored) or any(
            not math.isfinite(g) or g <= 0 for g in ignore_limits
        ):
            raise ValueError(
                "ignored gates must match ignored positions and be finite and positive"
            )
        distances = [[math.dist(p, t) for t in points] for p in predictions]
        matched = assignment(
            [[d if d <= gates[j] else math.inf for j, d in enumerate(row)] for row in distances],
            len(points),
        )
        errors = [distances[i][j] for i, j in enumerate(matched) if j >= 0]
        tp = len(errors)
        fp = false_alarms = duplicates = ignored_count = 0
        for i, column in enumerate(matched):
            if column >= 0:
                continue
            near_eligible = any(v <= gates[j] for j, v in enumerate(distances[i]))
            near_ignored = any(
                math.dist(predictions[i], t) <= g for t, g in zip(ignored, ignore_limits)
            )
            if near_ignored and not near_eligible:
                ignored_count += 1
            else:
                fp += 1
                if near_eligible:
                    duplicates += 1
                else:
                    false_alarms += 1
        used = set(matched) - {-1}
        for j, key in enumerate(ids):
            counts = self.per_target.setdefault(key, {"tp": 0, "fn": 0})
            counts["tp" if j in used else "fn"] += 1
        fn = len(points) - tp
        recall, precision = _ratio(tp, tp + fn), _ratio(tp, tp + fp)
        self.frames += 1
        self.tp += tp
        self.fp += fp
        self.fn += fn
        self.false_alarms += false_alarms
        self.duplicates += duplicates
        self.ignored_predictions += ignored_count
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "recall": recall,
            "precision": precision,
            "false_alarms": false_alarms,
            "duplicates": duplicates,
            "ignored_predictions": ignored_count,
            "matched_errors": errors,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "recall": _ratio(self.tp, self.tp + self.fn),
            "precision": _ratio(self.tp, self.tp + self.fp),
            "f1": _ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn),
            "false_alarms": self.false_alarms,
            "duplicates": self.duplicates,
            "ignored_predictions": self.ignored_predictions,
            "gate": self.gate,
            "error_unit": self.unit,
            "per_target": {
                key: {**counts, "recall": _ratio(counts["tp"], sum(counts.values()))}
                for key, counts in sorted(self.per_target.items())
            },
        }


def target_pixel_gates(
    targets: list[dict[str, Any]], floor_px: float, size_k: float | None
) -> dict[str, float]:
    """Use recorded projected-envelope dimensions, never pixel area as a length."""
    if not math.isfinite(floor_px) or floor_px <= 0:
        raise ValueError("pixel floor must be finite and positive")
    if size_k is not None and (not math.isfinite(size_k) or size_k <= 0):
        raise ValueError("size k must be finite and positive")
    gates = {}
    for target in targets:
        if not target["projection"]["center_in_field_of_view"]:
            continue
        gate = floor_px
        if size_k is not None:
            width, height = target["projected_width_px"], target["projected_height_px"]
            if (
                width is None
                or height is None
                or not math.isfinite(width)
                or not math.isfinite(height)
                or width <= 0
                or height <= 0
            ):
                raise ValueError("dynamic scoring requires positive recorded projection dimensions")
            gate = max(floor_px, size_k * math.hypot(width, height))
        gates[target["truth_object_id"]] = gate
    return gates


def annotate(record: dict[str, Any], gate: float) -> dict[str, Any]:
    """Reconstruct the evaluator's exact assignment, without changing its scores."""
    ids = sorted(record["eligible_targets_px"])
    targets = [record["eligible_targets_px"][key] for key in ids]
    predictions = record["predictions_px"]
    gates = [record.get("target_gates_px", {}).get(key, gate) for key in ids]
    costs = [[math.dist(p, t) for t in targets] for p in predictions]
    matches = assignment(
        [[v if v <= gates[j] else math.inf for j, v in enumerate(row)] for row in costs], len(ids)
    )
    used = set(matches) - {-1}
    return {
        "targets": [
            {"id": key, "xy": targets[j], "matched": j in used, "gate": gates[j]}
            for j, key in enumerate(ids)
        ],
        "predictions": [
            {
                "xy": p,
                "target": matches[i],
                "kind": "matched"
                if matches[i] >= 0
                else (
                    "duplicate"
                    if any(v <= gates[j] for j, v in enumerate(costs[i]))
                    else "false_alarm"
                ),
            }
            for i, p in enumerate(predictions)
        ],
    }
