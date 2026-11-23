"""Core scoring engine for Edge, Center, and Integrated Run Output packages."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chongming_proto.online import common_pb2, online_pb2
from chongming_proto.truth import truth_pb2
from chongming_scorer.attribution import attribute_messages
from chongming_scorer.difficulty import score_difficulty_strata
from chongming_scorer.metrics import (
    CenterSizeMetrics,
    DetectionMetrics,
    projection_pixel_gates,
)
from chongming_scorer.performance import summarize_edge_timings
from chongming_scorer.storage import RunOutputPackage, TestScenePackage


def build_score_event(
    run_id: str,
    experiment_time_ns: int,
    matched_errors: Sequence[float],
    *,
    unmatched_count: int,
    unevaluable_count: int,
    error_unit: str,
    extra_metrics: Mapping[str, tuple[float | None, str]] | None = None,
) -> truth_pb2.ScoreEvent:
    """Create a ScoreEvent; error statistics are defined on matched samples only."""
    event = truth_pb2.ScoreEvent(
        run_id=run_id,
        experiment_time_ns=int(experiment_time_ns),
        matched_count=len(matched_errors),
        unmatched_count=int(unmatched_count),
        unevaluable_count=int(unevaluable_count),
        error_unit=error_unit,
    )
    if matched_errors:
        event.mean_error = math.fsum(matched_errors) / len(matched_errors)
        event.rmse = math.sqrt(math.fsum(err * err for err in matched_errors) / len(matched_errors))
        event.max_error = max(matched_errors)
    if extra_metrics:
        for name, (value, unit) in sorted(extra_metrics.items()):
            metric = truth_pb2.ScoreMetric(name=name, unit=unit)
            if value is not None:
                metric.value = float(value)
            event.extra_metrics.append(metric)
    return event


@dataclass
class SceneTruthIndex:
    """In-memory index of static Test Scene frame_index and TruthFrames."""

    frame_index: dict[str, truth_pb2.FrameIndexEntry]
    expected_main_stations_by_time: dict[int, set[str]]
    truth_by_time: dict[int, truth_pb2.TruthFrame]
    projections_by_frame: dict[str, list[truth_pb2.StationTargetProjection]]
    projections_by_time: dict[tuple[str, int], list[truth_pb2.StationTargetProjection]]

    @classmethod
    def from_scene(cls, scene: TestScenePackage) -> SceneTruthIndex:
        """Build a shared truth and frame-index lookup from a TestScenePackage."""
        frame_index: dict[str, truth_pb2.FrameIndexEntry] = {}
        expected_main_stations_by_time: dict[int, set[str]] = defaultdict(set)
        for entry in scene.iter_frame_index():
            if entry.camera_role == common_pb2.CAMERA_ROLE_MAIN:
                frame_index[entry.frame_id] = entry
                ns = (
                    entry.capture_time.exposure_start_ns + entry.capture_time.exposure_end_ns
                ) // 2
                expected_main_stations_by_time[ns].add(entry.station_id)

        instance = cls(
            frame_index=frame_index,
            expected_main_stations_by_time=expected_main_stations_by_time,
            truth_by_time={},
            projections_by_frame=defaultdict(list),
            projections_by_time=defaultdict(list),
        )
        for truth_frame in scene.iter_truth_frames():
            instance.register_truth_frame(truth_frame)
        return instance

    def register_truth_frame(self, truth_frame: truth_pb2.TruthFrame) -> None:
        """Index one TruthFrame or verify an existing frame matches byte-for-byte."""
        ns = int(truth_frame.experiment_time_ns)
        existing = self.truth_by_time.get(ns)
        if existing is not None:
            if existing.SerializeToString() != truth_frame.SerializeToString():
                raise ValueError(f"conflicting TruthFrame for experiment_time_ns={ns}")
            return
        self.truth_by_time[ns] = truth_frame
        for proj in truth_frame.projections:
            if proj.HasField("frame_id") and proj.frame_id:
                self.projections_by_frame[proj.frame_id].append(proj)
            self.projections_by_time[(proj.station_id, ns)].append(proj)


class EdgeIncrementalScorer:
    """Stateful Edge scorer shared by offline batch evaluation and online V-Switch streams."""

    def __init__(
        self,
        scene: TestScenePackage,
        *,
        run_id: str,
        gate_px: float = 10.0,
        size_k: float | None = 0.5,
        warmup_s: float = 15.0,
        include_difficulty: bool = True,
        scene_index: SceneTruthIndex | None = None,
        station_origins: Mapping[str, int] | None = None,
    ) -> None:
        if not 0 <= warmup_s < math.inf:
            raise ValueError("warmup_s must be finite and nonnegative")
        self.scene = scene
        self.run_id = run_id
        self.gate_px = gate_px
        self.size_k = size_k
        self.warmup_s = warmup_s
        self.warmup_ns = round(warmup_s * 1e9)
        self.include_difficulty = include_difficulty
        self.scene_index = SceneTruthIndex.from_scene(scene) if scene_index is None else scene_index
        self._aggregate = DetectionMetrics(gate_px, "px")
        projection_pixel_gates((), gate_px, size_k)

        self.messages: list[online_pb2.BusMessage] = []
        self._batches: list[online_pb2.DetectionBatch] = []
        self._precomputed_origins = station_origins is not None
        self._origins: dict[str, int] = dict(station_origins) if station_origins is not None else {}
        self._station_metrics: dict[str, DetectionMetrics] = {
            station: DetectionMetrics(gate_px, "px") for station in sorted(self._origins)
        }
        self._processed: dict[str, int] = dict.fromkeys(self._origins, 0)
        self._warmup_frames: dict[str, int] = dict.fromkeys(self._origins, 0)
        self._frame_records: list[dict[str, Any]] = []
        self._per_time_errors: dict[int, list[float]] = defaultdict(list)
        self._per_time_fp: dict[int, int] = defaultdict(int)
        self._per_time_fn: dict[int, int] = defaultdict(int)
        self._per_time_ignored: dict[int, int] = defaultdict(int)
        self._seen_stations_by_time: dict[int, set[str]] = defaultdict(set)
        self._flushed_times: set[int] = set()
        self._score_events: list[truth_pb2.ScoreEvent] = []

    def feed_bus_message(self, msg: online_pb2.BusMessage) -> list[truth_pb2.ScoreEvent]:
        """Ingest one edge BusMessage and emit completed per-timestamp ScoreEvents."""
        self.messages.append(msg)
        if msg.WhichOneof("payload") == "detection_batch":
            return self.feed_batch(msg.detection_batch)
        return []

    def feed_batch(self, batch: online_pb2.DetectionBatch) -> list[truth_pb2.ScoreEvent]:
        """Ingest one DetectionBatch and return any newly completed ScoreEvents."""
        if batch.processing_status != common_pb2.PROCESSING_STATUS_PROCESSED:
            raise RuntimeError(f"edge frame {batch.main_frame_ref.frame_id!r} failed processing")
        station = batch.main_frame_ref.station_id
        frame_id = batch.main_frame_ref.frame_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2

        if station not in self._origins:
            self._origins[station] = ns
            self._station_metrics[station] = DetectionMetrics(self.gate_px, "px")
            self._processed[station] = 0
            self._warmup_frames[station] = 0
        elif not self._precomputed_origins and ns < self._origins[station]:
            raise ValueError(
                f"edge frame {frame_id!r} for station {station!r} has non-monotonic time {ns}"
            )

        indexed = self.scene_index.frame_index.get(frame_id)
        if indexed is not None:
            indexed_ns = (
                indexed.capture_time.exposure_start_ns + indexed.capture_time.exposure_end_ns
            ) // 2
            if (
                indexed.station_id != station
                or indexed.station_session_id != batch.main_frame_ref.station_session_id
                or indexed_ns != ns
            ):
                raise ValueError(
                    f"frame identity or capture time differs from scene index for {frame_id!r}"
                )
        elif ns not in self.scene_index.truth_by_time:
            raise ValueError(f"edge frame {frame_id!r} has no matching truth frame")

        self._batches.append(batch)
        self._processed[station] += 1
        excluded = ns - self._origins[station] < self.warmup_ns
        record: dict[str, Any] = {
            "station_id": station,
            "frame_id": frame_id,
            "time_ns": ns,
            "warmup_excluded": excluded,
            "decode_ms": batch.decode_duration_ns / 1e6,
            "cv_ms": batch.cv_duration_ns / 1e6,
            "observation_count": len(batch.observations),
            "predictions_px": [
                [obs.centroid_px.x, obs.centroid_px.y] for obs in batch.observations
            ],
        }
        if excluded:
            self._warmup_frames[station] += 1
        else:
            projs = self.scene_index.projections_by_frame.get(
                frame_id
            ) or self.scene_index.projections_by_time.get((station, ns), [])
            targets: dict[str, tuple[float, ...]] = {}
            for proj in projs:
                if proj.occlusion_status == truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV:
                    continue
                if not proj.HasField("projected_centroid_px"):
                    raise ValueError(
                        f"in-FOV projection missing projected_centroid_px for {proj.target_id!r}"
                    )
                targets[proj.target_id] = (
                    proj.projected_centroid_px.x,
                    proj.projected_centroid_px.y,
                )
            gates = projection_pixel_gates(projs, self.gate_px, self.size_k)
            predictions = [(obs.centroid_px.x, obs.centroid_px.y) for obs in batch.observations]
            row_result = self._station_metrics[station].add(
                predictions, targets, target_gates=gates
            )
            self._aggregate.add(predictions, targets, target_gates=gates)
            record["eligible_targets_px"] = targets
            record["target_gates_px"] = gates
            record.update(row_result)
            self._per_time_errors[ns].extend(row_result["matched_errors"])
            self._per_time_fp[ns] += row_result["fp"]
            self._per_time_fn[ns] += row_result["fn"]
            self._per_time_ignored[ns] += row_result["ignored_predictions"]
        self._frame_records.append(record)
        self._seen_stations_by_time[ns].add(station)

        expected = self.scene_index.expected_main_stations_by_time.get(ns)
        if expected and expected.issubset(self._seen_stations_by_time[ns]):
            ev = self._flush_time(ns)
            if ev is not None:
                return [ev]
        return []

    def _flush_time(self, ns: int) -> truth_pb2.ScoreEvent | None:
        if ns in self._flushed_times or ns not in self._per_time_fp:
            return None
        self._flushed_times.add(ns)
        event = build_score_event(
            self.run_id,
            ns,
            self._per_time_errors[ns],
            unmatched_count=self._per_time_fp[ns],
            unevaluable_count=self._per_time_ignored[ns],
            error_unit="px",
            extra_metrics={"missed_targets": (float(self._per_time_fn[ns]), "count")},
        )
        self._score_events.append(event)
        return event

    def flush_remaining(self) -> list[truth_pb2.ScoreEvent]:
        """Flush ScoreEvents for any remaining un-emitted timestamps."""
        emitted: list[truth_pb2.ScoreEvent] = []
        for ns in sorted(self._per_time_fp):
            event = self._flush_time(ns)
            if event is not None:
                emitted.append(event)
        return emitted

    def finalize(self) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
        """Finalize edge scoring and return sorted ScoreEvents, report dict, and frame records."""
        if not self._batches:
            raise ValueError("edge_events.pb.zst contains no DetectionBatch messages")
        self.flush_remaining()
        timings = summarize_edge_timings(self._batches)
        difficulty = (
            score_difficulty_strata(self.scene, self._frame_records, gate_px=self.gate_px)
            if self.include_difficulty
            else None
        )
        report: dict[str, Any] = {
            "format": "chongming-scorer-edge-v1",
            "status": "complete",
            "run_id": self.run_id,
            "test_scene_sha256": self.scene.content_sha256,
            "warmup_s": self.warmup_s,
            "gate_px": self.gate_px,
            "size_k": self.size_k,
            "gate_rule": (
                "max(gate_px, size_k * projected_diagonal_px)"
                if self.size_k is not None
                else "fixed gate_px"
            ),
            "responsibility": "main-camera target center in FOV; occlusion ignored",
            "matching": "maximum cardinality then minimum total pixel distance within gate",
            "aggregate": self._aggregate.summary(),
            "stations": {
                station: {
                    "metrics": self._station_metrics[station].summary(),
                    "processed_frames": self._processed[station],
                    "warmup_frames": self._warmup_frames[station],
                    **timings["stations"][station],
                }
                for station in sorted(self._origins)
            },
            "timings": timings,
        }
        if difficulty is not None:
            report["difficulty"] = difficulty
        events = sorted(self._score_events, key=lambda item: item.experiment_time_ns)
        return events, report, list(self._frame_records)


class CenterIncrementalScorer:
    """Stateful Center scorer shared by offline batch evaluation and online V-Switch streams."""

    def __init__(
        self,
        scene: TestScenePackage,
        *,
        run_id: str,
        gate_m: float = 10.0,
        size_k: float | None = None,
        miss_k: float = 3.0,
        warmup_s: float = 0.0,
        warmup_origin_ns: int | None = None,
        minimum_stations: int = 2,
        scene_index: SceneTruthIndex | None = None,
        defer_schedule_evaluation: bool = False,
    ) -> None:
        if minimum_stations < 2:
            raise ValueError("minimum_stations must be at least two")
        if not 0 <= warmup_s < math.inf:
            raise ValueError("warmup_s must be finite and nonnegative")
        self.scene = scene
        self.run_id = run_id
        self.gate_m = gate_m
        self.size_k = size_k
        self.miss_k = miss_k
        self.warmup_s = warmup_s
        self.warmup_ns = round(warmup_s * 1e9)
        self.origin_ns = scene.start_time_ns if warmup_origin_ns is None else warmup_origin_ns
        self.minimum_stations = minimum_stations
        self.scene_index = SceneTruthIndex.from_scene(scene) if scene_index is None else scene_index
        self.defer_schedule_evaluation = defer_schedule_evaluation

        self._metrics = DetectionMetrics(gate_m, "m")
        self._size_metrics = CenterSizeMetrics(size_k, miss_k) if size_k is not None else None
        self.messages: list[online_pb2.BusMessage] = []
        self._predictions: dict[int, list[tuple[float, ...]]] = defaultdict(list)
        self._seen_measurements: dict[str, bytes] = {}
        self._schedule_times: set[int] = set()
        self._evaluated_times: set[int] = set()
        self._nonlocated = 0
        self._excluded = 0
        self._records: list[dict[str, Any]] = []
        self._score_events: list[truth_pb2.ScoreEvent] = []

    def feed_bus_message(self, msg: online_pb2.BusMessage) -> list[truth_pb2.ScoreEvent]:
        """Ingest one center BusMessage and emit a ScoreEvent when a scheduled window completes."""
        self.messages.append(msg)
        kind = msg.WhichOneof("payload")
        if kind == "progress_report":
            report_msg = msg.progress_report
            if report_msg.source_role == common_pb2.COMPONENT_ROLE_CENTER:
                ns = int(report_msg.completed_time_ns)
                if report_msg.finite_end_reached and ns == 0 and self.scene.start_time_ns > 0:
                    return []
                self._schedule_times.add(ns)
                if not self.defer_schedule_evaluation and ns not in self._evaluated_times:
                    ev = self._evaluate_time(ns)
                    if ev is not None:
                        return [ev]
            return []

        if kind == "fusion_measurement":
            meas = msg.fusion_measurement
            serialized = meas.SerializeToString()
            previous = self._seen_measurements.get(meas.measurement_id)
            if previous is not None:
                if previous != serialized:
                    raise ValueError(
                        f"conflicting duplicate measurement ID {meas.measurement_id!r}"
                    )
                return []
            self._seen_measurements[meas.measurement_id] = serialized
            if meas.status == common_pb2.FUSION_STATUS_LOCATED and meas.HasField("position_enu_m"):
                if meas.reference_time_ns in self._evaluated_times:
                    raise ValueError(
                        f"measurement at reference_time_ns={meas.reference_time_ns} arrived after window evaluation"
                    )
                pos = (
                    meas.position_enu_m.x,
                    meas.position_enu_m.y,
                    meas.position_enu_m.z,
                )
                self._predictions[meas.reference_time_ns].append(pos)
            else:
                self._nonlocated += 1
        return []

    def _evaluate_time(self, ns: int) -> truth_pb2.ScoreEvent | None:
        self._evaluated_times.add(ns)
        if not self.scene.start_time_ns <= ns <= self.scene.end_time_ns:
            raise ValueError("center reference time outside recorded scenario")
        if ns - self.origin_ns < self.warmup_ns:
            self._excluded += 1
            return None
        truth_frame = self.scene_index.truth_by_time.get(ns)
        if truth_frame is None:
            raise ValueError(f"missing TruthFrame for evaluated reference_time_ns={ns}")

        visible: dict[str, set[str]] = defaultdict(set)
        for proj in truth_frame.projections:
            if proj.occlusion_status == truth_pb2.OCCLUSION_STATUS_UNSPECIFIED:
                raise ValueError(
                    f"projection for target {proj.target_id!r} has unspecified occlusion_status"
                )
            if proj.occlusion_status != truth_pb2.OCCLUSION_STATUS_OUT_OF_FOV:
                visible[proj.target_id].add(proj.station_id)

        eligible: dict[str, tuple[float, ...]] = {}
        ignored: list[tuple[float, ...]] = []
        diagonals: dict[str, float] = {}
        ignored_diagonals: list[float] = []
        states: dict[str, dict[str, Any]] = {}

        for target in truth_frame.targets:
            pos = (
                target.position_enu_m.x,
                target.position_enu_m.y,
                target.position_enu_m.z,
            )
            station_set = visible.get(target.target_id, set())
            if len(station_set) >= self.minimum_stations:
                eligible[target.target_id] = pos
                if self._size_metrics is not None:
                    if (
                        not target.HasField("body_aabb_diagonal_m")
                        or target.body_aabb_diagonal_m <= 0
                    ):
                        raise ValueError(
                            f"TargetTruth {target.target_id!r} missing positive body_aabb_diagonal_m"
                        )
                    diagonals[target.target_id] = target.body_aabb_diagonal_m
                states[target.target_id] = {
                    "velocity_enu_mps": [
                        target.velocity_enu_mps.x,
                        target.velocity_enu_mps.y,
                        target.velocity_enu_mps.z,
                    ],
                    "coverage_stations": sorted(station_set),
                }
            else:
                ignored.append(pos)
                if self._size_metrics is not None:
                    if (
                        not target.HasField("body_aabb_diagonal_m")
                        or target.body_aabb_diagonal_m <= 0
                    ):
                        raise ValueError(
                            f"TargetTruth {target.target_id!r} missing positive body_aabb_diagonal_m"
                        )
                    ignored_diagonals.append(target.body_aabb_diagonal_m)

        preds = self._predictions.get(ns, [])
        if self._size_metrics is None:
            result = self._metrics.add(preds, eligible, ignored=ignored)
            primary = result
        else:
            result = self._size_metrics.add(preds, eligible, diagonals, ignored, ignored_diagonals)
            result["ignored_targets_enu_m"] = ignored
            result["ignored_target_diagonals_m"] = ignored_diagonals
            for key, state in states.items():
                result["targets"][key].update(state)
            primary = result["precise"]

        self._records.append({"reference_time_ns": ns, **result})
        event = build_score_event(
            self.run_id,
            ns,
            primary["matched_errors"],
            unmatched_count=primary["fp"],
            unevaluable_count=primary["ignored_predictions"],
            error_unit="m",
            extra_metrics={"missed_targets": (float(primary["fn"]), "count")},
        )
        self._score_events.append(event)
        return event

    def flush_remaining(self) -> list[truth_pb2.ScoreEvent]:
        """Evaluate all pending scheduled reference times in chronological order."""
        if not set(self._predictions).issubset(self._schedule_times):
            raise ValueError(
                "measurement reference time has no sealed-window schedule; recall is undefined"
            )
        emitted: list[truth_pb2.ScoreEvent] = []
        for ns in sorted(self._schedule_times):
            if ns in self._evaluated_times:
                continue
            event = self._evaluate_time(ns)
            if event is not None:
                emitted.append(event)
        return emitted

    def finalize(self) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
        """Finalize center scoring and return sorted ScoreEvents, report dict, and records."""
        self.flush_remaining()
        report: dict[str, Any] = {
            "format": (
                "center-size-evaluation-v1"
                if self._size_metrics is not None
                else "center-evaluation-v1"
            ),
            "status": "complete",
            "run_id": self.run_id,
            "test_scene_sha256": self.scene.content_sha256,
            "warmup_s": self.warmup_s,
            "warmup_origin_ns": self.origin_ns,
            "excluded_reference_times": self._excluded,
            "nonlocated_outputs": self._nonlocated,
            "minimum_stations": self.minimum_stations,
            "responsibility_exclusion": (
                "precise outputs ignore only hits within precise gate of nonresponsible targets"
            ),
            "responsibility": (
                f"target center in FOV of at least {self.minimum_stations} distinct main stations"
            ),
            "metrics": (
                self._metrics.summary()
                if self._size_metrics is None
                else self._size_metrics.summary()
            ),
        }
        events = sorted(self._score_events, key=lambda item: item.experiment_time_ns)
        return events, report, list(self._records)


class IntegratedIncrementalScorer:
    """Combined incremental Edge + Center + attribution scorer."""

    def __init__(
        self,
        scene: TestScenePackage,
        *,
        run_id: str,
        gate_px: float = 10.0,
        size_k_px: float | None = 0.5,
        gate_m: float = 10.0,
        size_k_m: float | None = None,
        miss_k: float = 3.0,
        warmup_s: float = 15.0,
        warmup_origin_ns: int | None = None,
        minimum_stations: int = 2,
        scene_index: SceneTruthIndex | None = None,
        edge_station_origins: Mapping[str, int] | None = None,
        defer_center_schedule_evaluation: bool = False,
    ) -> None:
        self.scene = scene
        self.run_id = run_id
        self.gate_px = gate_px
        self.size_k_px = size_k_px
        self.size_k_m = size_k_m
        self.warmup_s = warmup_s
        self.minimum_stations = minimum_stations
        self.scene_index = SceneTruthIndex.from_scene(scene) if scene_index is None else scene_index
        self.edge = EdgeIncrementalScorer(
            scene,
            run_id=run_id,
            gate_px=gate_px,
            size_k=size_k_px,
            warmup_s=warmup_s,
            include_difficulty=True,
            scene_index=self.scene_index,
            station_origins=edge_station_origins,
        )
        self.center = CenterIncrementalScorer(
            scene,
            run_id=run_id,
            gate_m=gate_m,
            size_k=size_k_m,
            miss_k=miss_k,
            warmup_s=warmup_s,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
            scene_index=self.scene_index,
            defer_schedule_evaluation=defer_center_schedule_evaluation,
        )

    def feed_bus_message(self, msg: online_pb2.BusMessage) -> list[truth_pb2.ScoreEvent]:
        """Route one BusMessage to edge and/or center scorers and emit ScoreEvents."""
        kind = msg.WhichOneof("payload")
        if kind in {"detection_batch", "calibration_state", "ray_event", "diagnostic_event"}:
            return self.edge.feed_bus_message(msg)
        if kind in {"fusion_measurement", "track_event", "sparse_voxel_grid"}:
            return self.center.feed_bus_message(msg)
        if (
            kind == "progress_report"
            and msg.progress_report.source_role == common_pb2.COMPONENT_ROLE_CENTER
        ):
            return self.center.feed_bus_message(msg)
        return []

    def flush_remaining(self) -> list[truth_pb2.ScoreEvent]:
        """Flush remaining ScoreEvents from both Edge and Center scorers."""
        return [*self.edge.flush_remaining(), *self.center.flush_remaining()]

    def finalize(self) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any]]:
        """Finalize integrated scoring and build the combined report dict."""
        edge_scores, edge_report, _ = self.edge.finalize()
        center_scores, center_report, center_records = self.center.finalize()
        attribution: dict[str, Any] | None = None
        if self.size_k_m is not None:
            attribution = attribute_messages(
                self.scene,
                self.edge.messages,
                self.center.messages,
                center_records,
                gate_px=self.gate_px,
                size_k_px=self.size_k_px,
                precise_k_m=self.size_k_m,
                minimum_stations=self.minimum_stations,
                warmup_s=self.warmup_s,
            )
        combined_report: dict[str, Any] = {
            "format": "chongming-scorer-integrated-v1",
            "status": "complete",
            "run_id": self.run_id,
            "test_scene_sha256": self.scene.content_sha256,
            "edge": edge_report,
            "center": center_report,
        }
        if attribution is not None:
            combined_report["attribution"] = attribution
        all_scores = sorted(
            [*edge_scores, *center_scores],
            key=lambda item: (item.experiment_time_ns, item.error_unit),
        )
        return all_scores, combined_report


def _scan_edge_station_origins(messages: Sequence[online_pb2.BusMessage]) -> dict[str, int]:
    origins: dict[str, int] = {}
    for msg in messages:
        if msg.WhichOneof("payload") != "detection_batch":
            continue
        batch = msg.detection_batch
        if batch.processing_status != common_pb2.PROCESSING_STATUS_PROCESSED:
            raise RuntimeError(f"edge frame {batch.main_frame_ref.frame_id!r} failed processing")
        station = batch.main_frame_ref.station_id
        ns = (batch.capture_time.exposure_start_ns + batch.capture_time.exposure_end_ns) // 2
        if station not in origins or ns < origins[station]:
            origins[station] = ns
    return origins


def score_edge_run(
    scene: TestScenePackage,
    run: RunOutputPackage,
    *,
    gate_px: float = 10.0,
    size_k: float | None = 0.5,
    warmup_s: float = 15.0,
    include_difficulty: bool = True,
    scene_index: SceneTruthIndex | None = None,
) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
    """Score Edge DetectionBatch outputs against immutable Test Scene truth."""
    run.verify_matches_scene(scene)
    if not run.has_edge_events:
        raise FileNotFoundError("Run Output is missing edge_events.pb.zst")

    messages = list(run.iter_edge_events())
    origins = _scan_edge_station_origins(messages)
    scorer = EdgeIncrementalScorer(
        scene,
        run_id=run.run_id,
        gate_px=gate_px,
        size_k=size_k,
        warmup_s=warmup_s,
        include_difficulty=include_difficulty,
        scene_index=scene_index,
        station_origins=origins,
    )
    for msg in messages:
        scorer.feed_bus_message(msg)
    return scorer.finalize()


def score_center_run(
    scene: TestScenePackage,
    run: RunOutputPackage,
    *,
    gate_m: float = 10.0,
    size_k: float | None = None,
    miss_k: float = 3.0,
    warmup_s: float = 0.0,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
    scene_index: SceneTruthIndex | None = None,
) -> tuple[list[truth_pb2.ScoreEvent], dict[str, Any], list[dict[str, Any]]]:
    """Score Center FusionMeasurement outputs against immutable Test Scene truth."""
    run.verify_matches_scene(scene)
    if not run.has_center_output:
        raise FileNotFoundError("Run Output is missing center_output.pb.zst")

    scorer = CenterIncrementalScorer(
        scene,
        run_id=run.run_id,
        gate_m=gate_m,
        size_k=size_k,
        miss_k=miss_k,
        warmup_s=warmup_s,
        warmup_origin_ns=warmup_origin_ns,
        minimum_stations=minimum_stations,
        scene_index=scene_index,
        defer_schedule_evaluation=True,
    )
    for msg in run.iter_center_output():
        scorer.feed_bus_message(msg)
    return scorer.finalize()


def score_run(
    scene_dir: Path,
    run_dir: Path,
    *,
    output_dir: Path | None = None,
    mode: str | None = None,
    gate_px: float = 10.0,
    size_k_px: float | None = 0.5,
    gate_m: float = 10.0,
    size_k_m: float | None = None,
    miss_k: float = 3.0,
    warmup_s: float | None = None,
    warmup_origin_ns: int | None = None,
    minimum_stations: int = 2,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Score a Run Output package against an immutable Test Scene and persist results."""
    scene = TestScenePackage.open(scene_dir, verify_hashes=verify_hashes)
    run = RunOutputPackage.open(run_dir, verify_hashes=verify_hashes)
    run.verify_matches_scene(scene)
    effective_mode = run.mode if mode is None else mode
    scene_index = SceneTruthIndex.from_scene(scene)

    if effective_mode == "edge":
        edge_warmup = 15.0 if warmup_s is None else warmup_s
        scores, report, _ = score_edge_run(
            scene,
            run,
            gate_px=gate_px,
            size_k=size_k_px,
            warmup_s=edge_warmup,
            scene_index=scene_index,
        )
        run.write_scores(scores, report, output_dir=output_dir)
        return report

    if effective_mode == "center":
        center_warmup = 0.0 if warmup_s is None else warmup_s
        scores, report, _ = score_center_run(
            scene,
            run,
            gate_m=gate_m,
            size_k=size_k_m,
            miss_k=miss_k,
            warmup_s=center_warmup,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
            scene_index=scene_index,
        )
        run.write_scores(scores, report, output_dir=output_dir)
        return report

    if effective_mode == "integrated":
        effective_warmup = 15.0 if warmup_s is None else warmup_s
        if not run.has_edge_events:
            raise FileNotFoundError("Run Output is missing edge_events.pb.zst")
        if not run.has_center_output:
            raise FileNotFoundError("Run Output is missing center_output.pb.zst")
        edge_messages = list(run.iter_edge_events())
        origins = _scan_edge_station_origins(edge_messages)
        integrated = IntegratedIncrementalScorer(
            scene,
            run_id=run.run_id,
            gate_px=gate_px,
            size_k_px=size_k_px,
            gate_m=gate_m,
            size_k_m=size_k_m,
            miss_k=miss_k,
            warmup_s=effective_warmup,
            warmup_origin_ns=warmup_origin_ns,
            minimum_stations=minimum_stations,
            scene_index=scene_index,
            edge_station_origins=origins,
            defer_center_schedule_evaluation=True,
        )
        for msg in edge_messages:
            integrated.feed_bus_message(msg)
        for msg in run.iter_center_output():
            integrated.feed_bus_message(msg)
        all_scores, combined_report = integrated.finalize()
        run.write_scores(all_scores, combined_report, output_dir=output_dir)
        return combined_report

    raise ValueError(f"unsupported score mode: {effective_mode!r}")
