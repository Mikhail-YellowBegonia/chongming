"""Center orchestration; only consumed sealed windows advance tracking time."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import perf_counter

from chongming.contracts import fusion as f
from chongming.contracts import model as m
from chongming.modules.base import ModuleShell
from chongming.modules.fusion_postintersection import exclusive_tracking_inputs, suppress_duplicates
from chongming.ports import fusion as ports


@dataclass(kw_only=True)
class CenterFusionModule(ModuleShell):
    business_implemented = True
    center_session_id: m.SessionId
    policy: f.FusionPolicy
    stations: ports.StationRegistry
    calibrations: ports.CalibrationRegistry
    admission: ports.EventAdmission
    windows: ports.EventWindows
    candidates: ports.CandidateGenerator
    association: ports.CrossStationAssociator
    geometry: ports.GeometryRefiner
    tracker: ports.CenterTracker
    clock: ports.EventClockMapping
    duplicate_distance_m: float | None = None
    _last_window_end_ns: int | None = field(default=None, init=False)
    stage_timings: list[dict[str, float | int]] = field(default_factory=list, init=False)

    def register_station(self, config: m.StationConfig) -> f.RegistryWriteResult:
        return self.stations.register(config)

    def register_calibration(self, solution: m.CalibrationSolution) -> f.RegistryWriteResult:
        return self.calibrations.register_solution(solution)

    def _offer(self, outcomes: tuple[f.AdmissionOutcome, ...]) -> list[f.WindowUpdate]:
        def order(outcome: f.AdmissionOutcome) -> tuple[int, int]:
            ray = outcome.admitted_ray
            if ray is None:
                return 1, 0
            capture = ray.event.capture_time
            return 0, (capture.exposure_start_ns + capture.exposure_end_ns) // 2

        # A prerequisite update can release several old rays at once. Buffer them
        # in exposure order before control-only offers trigger deadline checks.
        return [self.windows.offer(outcome) for outcome in sorted(outcomes, key=order)]

    def process(self, received: m.ReceivedEvent) -> f.FusionBatchResult:
        now = received.arrival_time
        retried = self.admission.reconsider_quarantined(now)
        current = self.admission.admit(received)
        if (
            isinstance(received.event, m.CalibrationStateEvent)
            and current.record.decision is f.AdmissionDecision.ACCEPTED
        ):
            retried += self.admission.reconsider_quarantined(now)
        outcomes = (*retried, current)
        return self._finish(
            now, tuple(outcome.record for outcome in outcomes), self._offer(outcomes)
        )

    def report_progress(self, progress: f.StationEventTimeProgress) -> f.FusionBatchResult:
        outcomes = self.admission.reconsider_quarantined(progress.received_at)
        updates = self._offer(outcomes)
        updates.append(self.windows.report_progress(progress))
        return self._finish(
            progress.received_at, tuple(outcome.record for outcome in outcomes), updates
        )

    def tick(self, now: m.ProcessingTime) -> f.FusionBatchResult:
        outcomes = self.admission.reconsider_quarantined(now)
        updates = self._offer(outcomes)
        updates.append(self.windows.advance(now))
        return self._finish(now, tuple(outcome.record for outcome in outcomes), updates)

    def _finish(
        self,
        now: m.ProcessingTime,
        admissions: tuple[f.AdmissionRecord, ...],
        updates: list[f.WindowUpdate],
    ) -> f.FusionBatchResult:
        windows = tuple(window for update in updates for window in update.sealed_windows)
        candidates, associations, geometry = [], [], []
        measurements: list[m.FusionMeasurement] = []
        tracks: list[m.TrackEvent] = []
        suppressed: list[f.SuppressedMeasurement] = []
        tracking_exclusions: list[f.SuppressedMeasurement] = []
        for window in windows:
            if window.key.center_session_id != self.center_session_id or (
                self._last_window_end_ns is not None
                and window.key.start_ns < self._last_window_end_ns
            ):
                raise ValueError("sealed window session/order mismatch or repeated consumption")
            clock = window.key.capture_clock_domain_id
            began = perf_counter()
            predictions = self.tracker.predict(clock, window.reference_time_ns)
            candidate_batch = self.candidates.generate(window, predictions)
            proposed_at = perf_counter()
            association_batch = self.association.associate(window, candidate_batch, predictions)
            associated_at = perf_counter()
            # Motion priors are queried at each group's actual support epoch,
            # not at a nominal center later than all of its observations.
            results: list[f.GeometryResult] = []
            for group in association_batch.selected_groups:
                group_predictions = self.tracker.predict(clock, group.reference_time_ns)
                results.extend(
                    self.geometry.refine(
                        window,
                        replace(association_batch, selected_groups=(group,)),
                        group_predictions,
                    ).results
                )
            geometry_batch = (
                f.GeometryBatch(window=window.key, results=tuple(results))
                if association_batch.selected_groups
                else self.geometry.refine(window, association_batch, predictions)
            )
            current = tuple(result.measurement for result in geometry_batch.results)
            refined_at = perf_counter()
            if self.duplicate_distance_m is not None:
                current, removed = suppress_duplicates(current, self.duplicate_distance_m)
                suppressed.extend(removed)
            deduplicated_at = perf_counter()
            located = [
                measurement
                for measurement in current
                if measurement.status is m.FusionStatus.LOCATED
            ]
            event_time = max(
                (measurement.reference_time_ns for measurement in located),
                default=window.reference_time_ns,
            )
            tracking_current = current
            if self.duplicate_distance_m is not None:
                tracking_current, excluded = exclusive_tracking_inputs(current)
                tracking_exclusions.extend(excluded)
            tracks.extend(self.tracker.update(tracking_current, clock, event_time))
            # Exposure reference may lie on either side of the window center.
            # Neither it nor that center exceeds this consumed sealed window.
            horizon = max(window.reference_time_ns, event_time)
            tracks.extend(
                self.tracker.advance(
                    f.EventClockReading(
                        clock_domain_id=clock, event_time_ns=horizon, mapping_uncertainty_ns=0
                    )
                )
            )
            candidates.append(candidate_batch)
            associations.append(association_batch)
            geometry.append(geometry_batch)
            measurements.extend(current)
            ended_at = perf_counter()
            self.stage_timings.append(
                {
                    "window_start_ns": window.key.start_ns,
                    "candidate_ms": (proposed_at - began) * 1000,
                    "association_ms": (associated_at - proposed_at) * 1000,
                    "geometry_ms": (refined_at - associated_at) * 1000,
                    "deduplication_ms": (deduplicated_at - refined_at) * 1000,
                    "tracking_ms": (ended_at - deduplicated_at) * 1000,
                    "window_pipeline_ms": (ended_at - began) * 1000,
                    "geometry_groups": len(results),
                }
            )
            self._last_window_end_ns = window.key.end_ns
        combined = f.WindowUpdate(
            receipts=tuple(receipt for update in updates for receipt in update.receipts),
            sealed_windows=windows,
            watermark=updates[-1].watermark,
        )
        return f.FusionBatchResult(
            center_session_id=self.center_session_id,
            produced_at=now,
            admissions=admissions,
            window_update=combined,
            candidates=tuple(candidates),
            associations=tuple(associations),
            geometry=tuple(geometry),
            measurements=tuple(measurements),
            tracks=tuple(tracks),
            suppressed_measurements=tuple(suppressed),
            tracking_exclusions=tuple(tracking_exclusions),
        )
