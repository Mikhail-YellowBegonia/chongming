"""Size-aware center-only evaluation: precise, intermediate and off-target bands."""

from __future__ import annotations

import math
from typing import Any

from chongming.contracts import scene as s
from chongming.evaluation import DetectionMetrics, annotate
from chongming.rendering.geometry import resolve_geometry


def model_aabb_diagonal(geometry: s.GeometrySpec) -> float:
    """Body-space model AABB diagonal in metres, independent of target pose."""
    geometry = resolve_geometry(geometry)
    if isinstance(geometry, s.SphereGeometry):
        diagonal = 2 * geometry.radius_m * math.sqrt(3)
    elif isinstance(geometry, s.BoxGeometry):
        diagonal = 2 * math.sqrt(sum(v * v for v in geometry.half_extents_body_m))
    elif isinstance(geometry, s.TriangleMeshGeometry):
        diagonal = math.sqrt(
            sum(
                (
                    max(v[i] for v in geometry.vertices_body_m)
                    - min(v[i] for v in geometry.vertices_body_m)
                )
                ** 2
                for i in range(3)
            )
        )
    else:
        raise TypeError(f"unsupported target model geometry: {type(geometry).__name__}")
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise ValueError("target AABB diagonal must be finite and positive")
    return diagonal


class CenterSizeMetrics:
    """Independent maximum-cardinality assignments at each explicit threshold."""

    def __init__(self, precise_k: float, miss_k: float):
        if not 0 < precise_k < miss_k < math.inf:
            raise ValueError("require finite 0 < precise_k < miss_k")
        self.precise_k = precise_k
        self.miss_k = miss_k
        self.precise = DetectionMetrics(1, "m")
        self.coarse = DetectionMetrics(1, "m")

    def add(
        self,
        predictions: list[tuple[float, ...]],
        targets: dict[str, tuple[float, ...]],
        diagonals: dict[str, float],
        ignored: list[tuple[float, ...]],
        ignored_diagonals: list[float],
    ) -> dict[str, Any]:
        strict_gates = {key: self.precise_k * diagonals[key] for key in targets}
        # Exactly 3 diagonals is off target, not an intermediate match.
        coarse_gates = {key: math.nextafter(self.miss_k * diagonals[key], 0) for key in targets}
        ignore_gates = [math.nextafter(self.miss_k * d, 0) for d in ignored_diagonals]
        strict = self.precise.add(
            predictions,
            targets,
            ignored=ignored,
            target_gates=strict_gates,
            ignored_gates=[self.precise_k * d for d in ignored_diagonals],
        )
        coarse = self.coarse.add(
            predictions,
            targets,
            ignored=ignored,
            target_gates=coarse_gates,
            ignored_gates=ignore_gates,
        )
        fine_annotations = annotate(
            {
                "eligible_targets_px": targets,
                "predictions_px": predictions,
                "target_gates_px": strict_gates,
            },
            1,
        )
        coarse_annotations = annotate(
            {
                "eligible_targets_px": targets,
                "predictions_px": predictions,
                "target_gates_px": coarse_gates,
            },
            1,
        )
        fine_hits = {t["id"] for t in fine_annotations["targets"] if t["matched"]}
        coarse_hits = {t["id"] for t in coarse_annotations["targets"] if t["matched"]}
        diagnostics = {}
        for key, target in targets.items():
            nearest = min((math.dist(p, target) for p in predictions), default=math.inf)
            diagnostics[key] = {
                "position_enu_m": target,
                "aabb_diagonal_m": diagonals[key],
                "precise_hit": key in fine_hits,
                "within_miss_gate": key in coarse_hits,
                "nearest_output_error_m": nearest if math.isfinite(nearest) else None,
                "nearest_relative_error": nearest / diagonals[key]
                if math.isfinite(nearest)
                else None,
            }
        return {
            "precise": strict,
            "within_miss_gate": coarse,
            "targets": diagnostics,
            "predictions_enu_m": predictions,
            "intermediate_matches": coarse["tp"] - strict["tp"],
            "completely_off_target_outputs": coarse["false_alarms"],
        }

    def summary(self) -> dict[str, Any]:
        fine, coarse = self.precise.summary(), self.coarse.summary()
        return {
            "precise_k": self.precise_k,
            "miss_k": self.miss_k,
            "precise": fine,
            "within_miss_gate": coarse,
            "intermediate_matches": coarse["tp"] - fine["tp"],
            "no_match_within_miss_gate": coarse["fn"],
            "completely_off_target_outputs": coarse["false_alarms"],
        }
