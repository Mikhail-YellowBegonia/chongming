"""Percentile timing distributions and separate decode vs CV summaries per D3b."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from chongming_proto.online import online_pb2


def distribution(values: Sequence[float]) -> dict[str, Any]:
    """Nearest-rank percentiles, including cold-start samples."""
    if not values:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    for value in values:
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError("timing duration must be finite and nonnegative")
    ordered = sorted(float(v) for v in values)
    return {
        "samples": len(ordered),
        "mean": math.fsum(ordered) / len(ordered),
        "p50": ordered[math.ceil(len(ordered) * 0.5) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def summarize_edge_timings(batches: Iterable[online_pb2.DetectionBatch]) -> dict[str, Any]:
    """Summarize Edge decode and CV algorithm latencies strictly separately per D3b."""
    per_station_decode: dict[str, list[float]] = defaultdict(list)
    per_station_cv: dict[str, list[float]] = defaultdict(list)
    all_decode: list[float] = []
    all_cv: list[float] = []
    for batch in batches:
        station = batch.main_frame_ref.station_id
        decode_ms = batch.decode_duration_ns / 1e6
        cv_ms = batch.cv_duration_ns / 1e6
        per_station_decode[station].append(decode_ms)
        per_station_cv[station].append(cv_ms)
        all_decode.append(decode_ms)
        all_cv.append(cv_ms)
    return {
        "scope": "decode_ms and cv_ms are reported separately per D3b; total_algorithm_ms excludes decode",
        "aggregate": {
            "decode_ms": distribution(all_decode),
            "cv_ms": distribution(all_cv),
            "total_algorithm_ms": distribution(all_cv),
        },
        "stations": {
            station: {
                "decode_ms": distribution(per_station_decode[station]),
                "cv_ms": distribution(per_station_cv[station]),
                "total_algorithm_ms": distribution(per_station_cv[station]),
            }
            for station in sorted(per_station_cv)
        },
    }
