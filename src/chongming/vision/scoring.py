"""Privileged, read-only workbench scoring; truth never enters processing stages."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from chongming.evaluation import DetectionMetrics, annotate, target_pixel_gates


class WorkbenchScoring:
    """Keep actual recorded exposure times and score cached measured outputs."""

    def __init__(self, recording: Path, station: str, video: Path, warmup_s: float = 15):
        self.frames: list[dict[str, Any]] = []
        metadata = {}
        for line in (recording / "online.jsonl").read_text().splitlines():
            for f in json.loads(line)["frames"]:
                if f["station_id"] == station and f["camera_role"] == "main":
                    if (recording / f["path"]).resolve() != video.resolve():
                        raise ValueError("workbench video does not match station main stream")
                    metadata[f["frame_id"]] = f
        for line in (recording / "truth.jsonl").read_text().splitlines():
            for truth in json.loads(line)["camera_frames"]:
                if truth["frame_id"] in metadata:
                    f = metadata[truth["frame_id"]]
                    if truth["capture_time"] != f["capture_time"]:
                        raise ValueError("scoring truth and frame exposure differ")
                    self.frames.append({"metadata": f, "truth": truth})
        self.frames.sort(key=lambda f: f["metadata"]["frame_index"])
        if not self.frames or [f["metadata"]["frame_index"] for f in self.frames] != list(
            range(len(self.frames))
        ):
            raise ValueError("scoring requires complete contiguous station truth")
        self.times = [
            (
                f["metadata"]["capture_time"]["exposure_start_ns"]
                + f["metadata"]["capture_time"]["exposure_end_ns"]
            )
            / 2e9
            for f in self.frames
        ]
        self.warmup_s = warmup_s
        self.predictions: dict[int, list[tuple[float, ...]]] = {}
        self.cached_key: tuple[int, int, float, float] | None = None
        self.cached_result: tuple[dict[str, Any], dict[str, Any]] | None = None

    def reset(self) -> None:
        self.predictions.clear()
        self.cached_key = None

    def observe(self, index: int, predictions: list[tuple[float, ...]]) -> None:
        self.predictions[index] = predictions
        self.cached_key = None

    def score(self, index: int, floor: float, k: float) -> tuple[dict[str, Any], dict[str, Any]]:
        key = (index, len(self.predictions), floor, k)
        if key == self.cached_key:
            assert self.cached_result is not None
            return self.cached_result
        metric = DetectionMetrics(floor, "px")
        current = {}
        for i in sorted(self.predictions):
            if i > index:
                continue
            truth = self.frames[i]["truth"]["targets"]
            gates = target_pixel_gates(truth, floor, k)
            targets = {
                t["truth_object_id"]: tuple(t["projection"]["centroid_px"])
                for t in truth
                if t["projection"]["center_in_field_of_view"]
            }
            warmup = self.times[i] - self.times[0] < self.warmup_s
            row = {
                "eligible_targets_px": targets,
                "target_gates_px": gates,
                "predictions_px": self.predictions[i],
                "warmup_excluded": warmup,
            }
            if not warmup:
                row.update(metric.add(self.predictions[i], targets, target_gates=gates))
            if i == index:
                current = {**row, **annotate(row, floor)}
        self.cached_key = key
        self.cached_result = (current, metric.summary())
        return self.cached_result

    def draw(self, image: Any, index: int, floor: float, k: float) -> None:
        current, total = self.score(index, floor, k)
        for target in current["targets"]:
            center = tuple(round(v) for v in target["xy"])
            color = (70, 240, 90) if target["matched"] else (60, 60, 255)
            cv2.circle(image, center, round(target["gate"]), color, 1)
        for prediction in current["predictions"]:
            center = tuple(round(v) for v in prediction["xy"])
            color = (255, 230, 40) if prediction["kind"] == "matched" else (30, 170, 255)
            cv2.drawMarker(image, center, color, cv2.MARKER_CROSS, 10, 1)

        def rate(value: float | None) -> str:
            return "N/A" if value is None else f"{value:.1%}"

        label = (
            "WARMUP - excluded"
            if current["warmup_excluded"]
            else (f"Frame TP:{current['tp']} FP:{current['fp']} FN:{current['fn']}")
        )
        for y, text in (
            (115, f"Score k={k:.2f} floor={floor:g}px | {label}"),
            (
                140,
                f"Cumulative recall:{rate(total['recall'])} precision:{rate(total['precision'])}",
            ),
        ):
            cv2.putText(image, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 3)
            cv2.putText(image, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)

    def panel(
        self,
        image: np.ndarray,
        index: int,
        floor: float,
        k: float,
        candidates: list[tuple[float, float]],
        point: tuple[int, int] | None = None,
        timings: dict[str, float] | None = None,
    ) -> np.ndarray:
        """Native diagnostics with explicit recall denominators and local pixel detail."""
        current, total = self.score(index, floor, k)
        panel = np.full((600, 900, 3), 24, dtype=np.uint8)

        def text(message: str, y: int, color: tuple[int, int, int] = (225, 225, 225)) -> None:
            cv2.putText(panel, message, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1)

        text(
            f"Frame {index} / {len(self.frames) - 1} | time {self.times[index] - self.times[0]:.3f}s | k={k:.2f} floor={floor:g}px",
            25,
        )
        text(
            "WARMUP: excluded from totals"
            if current["warmup_excluded"]
            else f"Current TP={current['tp']} FP={current['fp']} FN={current['fn']} eligible={len(current['targets'])}",
            50,
        )
        text(f"Cumulative: {total['frames']} evaluated frames (current station only)", 75)
        text(
            f"Recall = TP/(TP+FN) = {total['tp']}/{total['tp'] + total['fn']} | Precision = TP/(TP+FP) = {total['tp']}/{total['tp'] + total['fp']}",
            100,
        )
        text("Green=matched; red=miss; cyan=matched output; orange=unmatched", 125)
        text("Target                     hit  nearest/gate(px)  candidate nearby", 155)
        for row, target in enumerate(current["targets"]):
            nearest = min(
                (math.dist(p, target["xy"]) for p in self.predictions[index]), default=math.inf
            )
            near_blob = any(math.dist(p, target["xy"]) <= target["gate"] for p in candidates)
            text(
                f"{target['id'][:26]:26} {'YES' if target['matched'] else 'NO ':3}  {nearest:6.1f}/{target['gate']:5.1f}       {'yes' if near_blob else 'no'}",
                180 + row * 22,
                (100, 240, 120) if target["matched"] else (100, 100, 255),
            )
        text("Nearest output may belong to another target. One-to-one matching applies.", 375)
        text("Click the image to inspect pixels; Space=pause; A/D=step; S=save policy", 400)
        if point is None:
            target = next((t for t in current["targets"] if not t["matched"]), None)
            point = (
                tuple(round(v) for v in target["xy"])
                if target
                else (image.shape[1] // 2, image.shape[0] // 2)
            )
        x = max(0, min(image.shape[1] - 1, point[0]))
        y = max(0, min(image.shape[0] - 1, point[1]))
        crop = np.zeros((80, 80, 3), dtype=np.uint8)
        x0, y0 = max(0, x - 40), max(0, y - 40)
        patch = image[y0 : min(image.shape[0], y + 40), x0 : min(image.shape[1], x + 40)]
        crop[
            40 - (y - y0) : 40 - (y - y0) + patch.shape[0],
            40 - (x - x0) : 40 - (x - x0) + patch.shape[1],
        ] = patch
        panel[425:585, 12:172] = cv2.resize(crop, (160, 160), interpolation=cv2.INTER_NEAREST)
        cv2.putText(
            panel,
            f"Raw pixels at ({x}, {y}), 2x zoom",
            (190, 450),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (225, 225, 225),
            1,
        )
        if timings is not None:
            for row, (name, milliseconds) in enumerate(timings.items()):
                cv2.putText(
                    panel,
                    f"{name}: {milliseconds:.3f} ms",
                    (430, 440 + row * 22),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (225, 225, 225),
                    1,
                )
        return panel
