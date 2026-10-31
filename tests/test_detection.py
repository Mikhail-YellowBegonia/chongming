"""Deterministic component fixtures; none of these parameters are runtime defaults."""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch

import cv2
import numpy as np
from numpy.typing import NDArray

from chongming.composition.graph import declare_assembly
from chongming.contracts import device as d
from chongming.contracts import model as m
from chongming.modules.detection import (
    FILL_RATIO_SCORE,
    MOG2_ALGORITHM_ID,
    TRACK_QUALITY_SCORE,
    TRACKING_ALGORITHM_ID,
)
from chongming.modules.device import MainDetectionModule, MotionDetectionModule, Tracking2DModule
from tests.fixtures import architecture_specs

MS = 1_000_000


def detection_policy(**changes: Any) -> d.DetectionPolicy:
    # A frozen, easily segmented black/white fixture, not a tuned operating profile.
    return replace(
        d.DetectionPolicy(
            algorithm_id=MOG2_ALGORITHM_ID,
            score_definition=FILL_RATIO_SCORE,
            minimum_component_area_px2=1,
            maximum_component_area_px2=None,
            background_warmup_frame_count=2,
            background_history_frame_count=20,
            background_variance_threshold=16.0,
            background_learning_rate=0.0,
            background_detect_shadows=False,
        ),
        **changes,
    )


def tracking_policy(**changes: Any) -> d.Tracking2DPolicy:
    return replace(
        d.Tracking2DPolicy(
            algorithm_id=TRACKING_ALGORITHM_ID,
            association_gate_px=6.0,
            confirmation_frame_count=2,
            maximum_missing_duration_ns=500 * MS,
            quality_definition=TRACK_QUALITY_SCORE,
        ),
        **changes,
    )


def frame(
    pixels: NDArray[np.uint8] | None = None,
    *,
    time_ns: int = 0,
    station: str = "a",
    session: str = "run-1",
    pixel_format: m.PixelFormat = m.PixelFormat.GRAY8,
    padding: int = 0,
) -> m.CameraFrame:
    if pixels is None:
        pixels = np.zeros((32, 32), dtype=np.uint8)
    height, width = pixels.shape[:2]
    row = width * (1 if pixel_format is m.PixelFormat.GRAY8 else 3)
    packed = np.full((height, row + padding), 255, dtype=np.uint8)
    packed[:, :row] = pixels.reshape(height, row)
    return m.CameraFrame(
        station_id=m.StationId(station),
        station_session_id=m.SessionId(session),
        camera_role=m.CameraRole.MAIN,
        frame_id=m.FrameId(f"frame-{time_ns}"),
        capture_time=m.CaptureTime(
            clock_domain_id="experiment",
            exposure_start_ns=time_ns,
            exposure_end_ns=time_ns,
            clock_uncertainty_ns=0,
            rolling_shutter_model="none",
        ),
        image=m.ImagePlane(
            width_px=width,
            height_px=height,
            pixel_format=pixel_format,
            row_stride_bytes=row + padding,
            pixels=packed.tobytes(),
        ),
        camera_config_ref=m.ConfigDigest("camera-config-1"),
    )


def batch(
    time_ns: int, *points: tuple[float, float], station: str = "a", session: str = "run-1"
) -> d.Candidate2DBatch:
    source = frame(time_ns=time_ns, station=station, session=session)
    return d.Candidate2DBatch(
        main_frame_ref=m.FrameRef(
            station_id=source.station_id,
            station_session_id=source.station_session_id,
            camera_role=source.camera_role,
            frame_id=source.frame_id,
        ),
        capture_time=source.capture_time,
        processing_status=m.ProcessingStatus.PROCESSED,
        candidates=tuple(
            d.Candidate2D(
                centroid_px=point,
                centroid_covariance_px2=None,
                detection_score=0.75,
                detection_score_definition=FILL_RATIO_SCORE,
            )
            for point in points
        ),
        reason_codes=(),
    )


def tracker(**changes: Any) -> Tracking2DModule:
    return Tracking2DModule(
        station_id=m.StationId("a"),
        station_session_id=m.SessionId("run-1"),
        policy=tracking_policy(**changes),
    )


class MotionDetectionTests(unittest.TestCase):
    def test_real_mog2_distinguishes_warmup_empty_and_current_foreground(self) -> None:
        detector = MotionDetectionModule(policy=detection_policy())
        for index in range(2):
            result = detector.detect(frame(time_ns=index * MS))
            self.assertEqual(result.processing_status, m.ProcessingStatus.PROCESSED)
            self.assertEqual(result.candidates, ())
            self.assertEqual(result.reason_codes, ("background_warmup",))
        empty = detector.detect(frame(time_ns=2 * MS))
        self.assertEqual(empty.reason_codes, ("no_foreground_candidates",))
        image = np.zeros((32, 32), dtype=np.uint8)
        image[8:12, 10:14] = 255
        before = image.copy()
        result = detector.detect(frame(image, time_ns=3 * MS))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].centroid_px, (11.5, 9.5))
        self.assertIsNone(result.candidates[0].centroid_covariance_px2)
        self.assertEqual(result.candidates[0].detection_score, 1.0)
        self.assertEqual(result.candidates[0].detection_score_definition, FILL_RATIO_SCORE)
        self.assertEqual(result.reason_codes, ())
        np.testing.assert_array_equal(image, before)
        # Removing a frozen-background foreground object must not leave a predicted detection.
        removed = detector.detect(frame(time_ns=4 * MS))
        self.assertFalse(removed.candidates)
        self.assertEqual(removed.processing_status, m.ProcessingStatus.PROCESSED)

    def test_connected_component_centroid_fill_ratio_and_eight_connectivity(self) -> None:
        detector = MotionDetectionModule(policy=detection_policy(background_warmup_frame_count=1))
        detector.detect(frame())
        image = np.zeros((32, 32), dtype=np.uint8)
        image[5, 5:7] = 255
        image[6, 5] = 255
        image[10, 15] = image[11, 16] = 255
        result = detector.detect(frame(image, time_ns=MS))
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidates[0].centroid_px, (16 / 3, 16 / 3))
        self.assertEqual(result.candidates[0].detection_score, 0.75)
        self.assertEqual(result.candidates[1].centroid_px, (15.5, 10.5))
        self.assertEqual(result.candidates[1].detection_score, 0.5)
        self.assertTrue(all(item.centroid_covariance_px2 is None for item in result.candidates))
        self.assertEqual(FILL_RATIO_SCORE.kind, m.ScoreKind.RANKING)
        self.assertIsNone(FILL_RATIO_SCORE.cross_station_comparison_group)

    def test_area_filter_includes_boundaries_and_does_not_create_observations_from_noise(
        self,
    ) -> None:
        detector = MotionDetectionModule(
            policy=detection_policy(
                minimum_component_area_px2=4,
                maximum_component_area_px2=9,
                background_warmup_frame_count=1,
            )
        )
        detector.detect(frame())
        image = np.zeros((32, 32), dtype=np.uint8)
        image[1, 1] = 255
        image[5:7, 5:7] = 255
        image[10:13, 10:13] = 255
        image[18:22, 18:22] = 255
        result = detector.detect(frame(image, time_ns=MS))
        self.assertEqual([item.centroid_px for item in result.candidates], [(5.5, 5.5), (11, 11)])

    def test_color_formats_strides_and_memoryviews_preserve_pixel_coordinates(self) -> None:
        for pixel_format in m.PixelFormat:
            with self.subTest(pixel_format=pixel_format):
                detector = MotionDetectionModule(
                    policy=detection_policy(background_warmup_frame_count=1)
                )
                shape = (24, 32) if pixel_format is m.PixelFormat.GRAY8 else (24, 32, 3)
                image = np.zeros(shape, dtype=np.uint8)
                detector.detect(frame(image, pixel_format=pixel_format, padding=7))
                image[4:8, 9:12] = 255
                source = frame(image, time_ns=MS, pixel_format=pixel_format, padding=7)
                source = replace(
                    source, image=replace(source.image, pixels=memoryview(source.image.pixels))
                )
                result = detector.detect(source)
                self.assertEqual(result.candidates[0].centroid_px, (10, 5.5))
                self.assertEqual(len(result.candidates), 1)

    def test_shadow_labels_are_excluded_and_learning_configuration_is_explicit(self) -> None:
        model = Mock()
        mask = np.zeros((32, 32), dtype=np.uint8)
        mask[3:5, 3:5] = 127
        mask[10:12, 10:12] = 255
        model.apply.return_value = mask
        detector = MotionDetectionModule(
            policy=detection_policy(
                background_warmup_frame_count=0,
                background_learning_rate=0.125,
                background_detect_shadows=True,
            )
        )
        with patch(
            "chongming.modules.detection.cv2.createBackgroundSubtractorMOG2", return_value=model
        ) as factory:
            result = detector.detect(frame())
        factory.assert_called_once_with(history=20, varThreshold=16.0, detectShadows=True)
        self.assertEqual(model.apply.call_args.kwargs, {"learningRate": 0.125})
        self.assertEqual([item.centroid_px for item in result.candidates], [(10.5, 10.5)])

    def test_missing_and_invalid_policy_values_never_become_runtime_defaults(self) -> None:
        missing = d.DetectionPolicy(
            algorithm_id=MOG2_ALGORITHM_ID,
            score_definition=FILL_RATIO_SCORE,
            minimum_component_area_px2=1,
            maximum_component_area_px2=None,
        )
        with patch("chongming.modules.detection.cv2.createBackgroundSubtractorMOG2") as factory:
            with self.assertRaisesRegex(ValueError, "explicitly configured"):
                MotionDetectionModule(policy=missing).detect(frame())
            factory.assert_not_called()
        cases = {
            "algorithm_id": ("unimplemented",),
            "score_definition": (
                replace(FILL_RATIO_SCORE, kind=m.ScoreKind.CALIBRATED_PROBABILITY),
            ),
            "background_warmup_frame_count": (None, -1, True, 1.5),
            "background_history_frame_count": (None, 0, True),
            "background_variance_threshold": (None, 0, float("nan"), float("inf"), True),
            "background_learning_rate": (None, -1, 1.1, float("nan"), True),
            "background_detect_shadows": (None, 1),
            "minimum_component_area_px2": (0, -1, float("nan"), True),
            "maximum_component_area_px2": (0, float("nan"), True),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    MotionDetectionModule(policy=detection_policy(**{field: value})).detect(frame())
        with self.assertRaises(ValueError):
            MotionDetectionModule(
                policy=detection_policy(
                    minimum_component_area_px2=10,
                    maximum_component_area_px2=9,
                )
            ).detect(frame())

    def test_invalid_frames_do_not_train_or_bind_the_background(self) -> None:
        base = frame()
        bad_frames = [
            replace(base, camera_role=m.CameraRole.AUXILIARY),
            replace(base, frame_id=m.FrameId("")),
            replace(base, image=replace(base.image, width_px=0)),
            replace(base, capture_time=replace(base.capture_time, exposure_end_ns=-1)),
        ]
        detector = MotionDetectionModule(policy=detection_policy())
        with patch("chongming.modules.detection.cv2.createBackgroundSubtractorMOG2") as factory:
            for source in bad_frames:
                with self.assertRaises(ValueError):
                    detector.detect(source)
            factory.assert_not_called()
        # A valid frame from a different stream remains free to establish the binding.
        first = detector.detect(frame(station="b", session="fresh"))
        self.assertEqual(first.reason_codes, ("background_warmup",))

    def test_malformed_pixels_fail_the_exposure_without_counting_as_background_training(
        self,
    ) -> None:
        detector = MotionDetectionModule(policy=detection_policy())
        with patch("chongming.modules.detection.cv2.createBackgroundSubtractorMOG2") as factory:
            for time_ns, changes in ((0, {"pixels": b""}), (MS, {"row_stride_bytes": 1})):
                source = frame(time_ns=time_ns)
                result = detector.detect(replace(source, image=replace(source.image, **changes)))
                self.assertEqual(result.processing_status, m.ProcessingStatus.FAILED)
                self.assertEqual(result.reason_codes[0], "invalid_main_frame")
                self.assertFalse(result.candidates)
            factory.assert_not_called()
        with self.assertRaisesRegex(ValueError, "increase strictly"):
            detector.detect(frame(time_ns=MS))
        first = detector.detect(frame(time_ns=2 * MS))
        self.assertEqual(first.reason_codes, ("background_warmup",))

    def test_stream_station_session_camera_clock_shape_and_time_are_isolated(self) -> None:
        detector = MotionDetectionModule(policy=detection_policy())
        detector.detect(frame())
        source = frame(time_ns=MS)
        wrong = [
            replace(source, station_id=m.StationId("b")),
            replace(source, station_session_id=m.SessionId("run-2")),
            replace(source, camera_config_ref=m.ConfigDigest("other")),
            replace(source, capture_time=replace(source.capture_time, clock_domain_id="other")),
            frame(np.zeros((16, 16), dtype=np.uint8), time_ns=MS),
            frame(
                np.zeros((32, 32, 3), dtype=np.uint8), time_ns=MS, pixel_format=m.PixelFormat.RGB8
            ),
            frame(),
            frame(time_ns=-1),
        ]
        for source in wrong:
            with self.subTest(source=source.frame_id), self.assertRaises(ValueError):
                detector.detect(source)
        self.assertEqual(detector.detect(frame(time_ns=MS)).reason_codes, ("background_warmup",))
        self.assertNotIn("background_warmup", detector.detect(frame(time_ns=2 * MS)).reason_codes)
        fresh = MotionDetectionModule(policy=detection_policy())
        self.assertEqual(fresh.detect(frame(session="run-2")).reason_codes, ("background_warmup",))

    def test_backend_failure_is_reported_with_traceback_without_silent_reset(self) -> None:
        model = Mock()
        model.apply.side_effect = cv2.error("deliberate backend error")
        detector = MotionDetectionModule(policy=detection_policy())
        with (
            patch(
                "chongming.modules.detection.cv2.createBackgroundSubtractorMOG2", return_value=model
            ) as factory,
            self.assertLogs("chongming.modules.detection", level="ERROR") as logs,
        ):
            failed = detector.detect(frame())
            unavailable = detector.detect(frame(time_ns=MS))
        self.assertEqual(failed.processing_status, m.ProcessingStatus.FAILED)
        self.assertEqual(failed.reason_codes, ("background_processing_failed",))
        self.assertEqual(unavailable.reason_codes, ("background_model_unavailable_after_failure",))
        self.assertIsNotNone(logs.records[0].exc_info)
        factory.assert_called_once()
        model.apply.assert_called_once()


class LocalTrackingTests(unittest.TestCase):
    def test_confirmation_velocity_coasting_and_reacquisition_use_only_real_measurements(
        self,
    ) -> None:
        module = tracker()
        first = module.update(batch(0, (10, 5)))
        local_id = first.observations[0].local_track_id
        self.assertIsNone(first.tracks[0].velocity_pxps)
        self.assertEqual(first.tracks[0].lifecycle, "tentative")
        self.assertEqual(first.tracks[0].track_quality, 0.5)
        second = module.update(batch(100 * MS, (12, 5)))
        self.assertEqual(second.tracks[0].velocity_pxps, (20.0, 0.0))
        self.assertEqual(second.tracks[0].lifecycle, "confirmed")
        self.assertEqual(second.tracks[0].track_quality, 1)
        missed = module.update(batch(200 * MS))
        self.assertEqual(missed.observations, ())
        self.assertEqual(missed.tracks[0].centroid_px, (14.0, 5.0))
        self.assertEqual(missed.tracks[0].lifecycle, "coasting")
        self.assertEqual(missed.tracks[0].track_quality, 0)
        recovered = module.update(batch(300 * MS, (16, 5)))
        self.assertEqual(recovered.observations[0].local_track_id, local_id)
        self.assertEqual(recovered.observations[0].centroid_px, (16, 5))
        self.assertIsNone(recovered.observations[0].centroid_covariance_px2)
        self.assertEqual(recovered.tracks[0].lifecycle, "confirmed")
        self.assertEqual(recovered.tracks[0].velocity_pxps, (20.0, 0.0))
        self.assertEqual(recovered.tracks[0].track_quality, 0.5)

    def test_constant_velocity_prediction_not_last_position_drives_the_gate(self) -> None:
        module = tracker(association_gate_px=3)
        first = module.update(batch(0, (0, 0)))
        module.update(batch(100 * MS, (3, 0)))  # inclusive gate boundary
        result = module.update(batch(300 * MS, (9, 0)))
        self.assertEqual(
            result.observations[0].local_track_id, first.observations[0].local_track_id
        )
        self.assertEqual(result.tracks[0].velocity_pxps, (30.0, 0.0))

    def test_misses_reset_tentative_confirmation_streak(self) -> None:
        module = tracker()
        module.update(batch(0, (1, 1)))
        module.update(batch(100 * MS))
        third = module.update(batch(200 * MS, (1, 1)))
        self.assertEqual(third.tracks[0].lifecycle, "tentative")
        fourth = module.update(batch(300 * MS, (1, 1)))
        self.assertEqual(fourth.tracks[0].lifecycle, "confirmed")

    def test_expiry_uses_capture_nanoseconds_and_old_ids_do_not_return(self) -> None:
        module = tracker()
        first = module.update(batch(0, (1, 1)))
        local_id = first.observations[0].local_track_id
        boundary = module.update(batch(500 * MS))
        self.assertEqual(boundary.tracks[0].lifecycle, "coasting")
        expired = module.update(batch(500 * MS + 1, (1, 1)))
        self.assertEqual([track.lifecycle for track in expired.tracks], ["deleted", "tentative"])
        self.assertEqual(expired.tracks[0].local_track_id, local_id)
        self.assertEqual(expired.tracks[0].track_quality, 0)
        self.assertNotEqual(expired.observations[0].local_track_id, local_id)
        later = module.update(batch(500 * MS + 2))
        self.assertNotIn(local_id, [track.local_track_id for track in later.tracks])

    def test_distance_ordered_matching_is_one_to_one_with_deterministic_ties(self) -> None:
        module = tracker(association_gate_px=10)
        first = module.update(batch(0, (0, 0), (4, 0)))
        result = module.update(batch(MS, (2, 0)))
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(
            result.observations[0].local_track_id, first.observations[0].local_track_id
        )
        self.assertEqual(len(result.tracks), 2)
        self.assertEqual(sum(track.lifecycle == "coasting" for track in result.tracks), 1)
        # Reversing distinct candidate order does not swap unambiguous spatial matches.
        module = tracker(association_gate_px=10)
        first = module.update(batch(0, (0, 0), (8, 0)))
        result = module.update(batch(MS, (9, 0), (1, 0)))
        self.assertEqual(
            [observation.local_track_id for observation in result.observations],
            [first.observations[1].local_track_id, first.observations[0].local_track_id],
        )

    def test_each_candidate_is_observed_once_and_covariance_and_scores_are_preserved(self) -> None:
        module = tracker(confirmation_frame_count=1)
        source = batch(0, (1, 2), (3, 4))
        candidate = replace(source.candidates[0], centroid_covariance_px2=((0.25, 0.0), (0.0, 0.5)))
        source = replace(source, candidates=(candidate, source.candidates[1]))
        result = module.update(source)
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(len({item.local_track_id for item in result.observations}), 2)
        for actual, expected in zip(result.observations, source.candidates, strict=True):
            self.assertEqual(actual.centroid_px, expected.centroid_px)
            self.assertEqual(actual.centroid_covariance_px2, expected.centroid_covariance_px2)
            self.assertEqual(actual.detection_score, expected.detection_score)
            self.assertEqual(actual.detection_score_definition, expected.detection_score_definition)
            self.assertEqual(actual.main_frame_ref, source.main_frame_ref)
            self.assertEqual(actual.capture_time, source.capture_time)
        self.assertTrue(all(track.lifecycle == "confirmed" for track in result.tracks))

    def test_invalid_input_is_rejected_before_expiry_or_any_state_change(self) -> None:
        module = tracker()
        first = module.update(batch(0, (0, 0)))
        source = batch(100 * MS, (1, 0))
        wrong = [
            batch(100 * MS, (1, 0), station="b"),
            batch(100 * MS, (1, 0), session="run-2"),
            replace(
                source,
                main_frame_ref=replace(source.main_frame_ref, camera_role=m.CameraRole.AUXILIARY),
            ),
            replace(source, capture_time=replace(source.capture_time, clock_domain_id="other")),
            batch(0, (1, 0)),
            batch(-1, (1, 0)),
            replace(
                source, candidates=(replace(source.candidates[0], centroid_px=(float("nan"), 0)),)
            ),
            replace(
                source,
                candidates=(
                    replace(
                        source.candidates[0], centroid_covariance_px2=((-1.0, 0.0), (0.0, 1.0))
                    ),
                ),
            ),
            replace(source, candidates=(replace(source.candidates[0], detection_score=True),)),
            replace(source, processing_status=m.ProcessingStatus.FAILED),
        ]
        for candidate_batch in wrong:
            with self.subTest(batch=candidate_batch), self.assertRaises(ValueError):
                module.update(candidate_batch)
        result = module.update(source)
        self.assertEqual(
            result.observations[0].local_track_id, first.observations[0].local_track_id
        )
        self.assertEqual(result.tracks[0].lifecycle, "confirmed")
        self.assertEqual(result.tracks[0].velocity_pxps, (10.0, 0.0))

    def test_failed_batch_is_not_a_successful_empty_detection_or_a_new_track_sample(self) -> None:
        module = tracker()
        first = module.update(batch(0, (0, 0)))
        failed = replace(
            batch(100 * MS),
            processing_status=m.ProcessingStatus.FAILED,
            reason_codes=("backend_failed",),
        )
        result = module.update(failed)
        self.assertEqual(result.observations, ())
        self.assertEqual(result.tracks[0].lifecycle, "coasting")
        self.assertEqual(result.tracks[0].track_quality, 0)
        next_result = module.update(batch(200 * MS, (2, 0)))
        self.assertEqual(
            next_result.observations[0].local_track_id, first.observations[0].local_track_id
        )
        self.assertEqual(next_result.tracks[0].velocity_pxps, (10.0, 0.0))
        self.assertEqual(next_result.tracks[0].lifecycle, "tentative")

    def test_local_ids_are_scoped_by_station_and_session_and_fresh_instances(self) -> None:
        modules = [
            Tracking2DModule(
                station_id=m.StationId(station),
                station_session_id=m.SessionId(session),
                policy=tracking_policy(),
            )
            for station, session in (("a", "run-1"), ("b", "run-1"), ("a", "run-2"))
        ]
        results = [
            module.update(
                batch(
                    0,
                    (index, index),
                    station=str(module.station_id),
                    session=str(module.station_session_id),
                )
            )
            for index, module in enumerate(modules)
        ]
        refs = {
            (
                item.observations[0].main_frame_ref.station_id,
                item.observations[0].main_frame_ref.station_session_id,
                item.observations[0].local_track_id,
            )
            for item in results
        }
        self.assertEqual(len(refs), 3)
        self.assertTrue(all(item.tracks[0].velocity_pxps is None for item in results))

    def test_tracking_policy_never_inherits_the_old_structural_placeholder_values(self) -> None:
        for field, values in {
            "algorithm_id": ("unimplemented",),
            "quality_definition": (FILL_RATIO_SCORE,),
            "association_gate_px": (0, float("nan"), True),
            "confirmation_frame_count": (0, 1.5, True),
            "maximum_missing_duration_ns": (0, -1, True),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    tracker(**{field: value}).update(batch(0))


class MainDetectionTests(unittest.TestCase):
    def test_declared_modules_use_explicit_policies_without_construction_side_effects(self) -> None:
        spec, run = architecture_specs()
        run = replace(
            run,
            stations=tuple(
                replace(
                    station,
                    device_policy=replace(
                        station.device_policy,
                        detection=detection_policy(),
                        tracking=tracking_policy(),
                    ),
                )
                for station in run.stations
            ),
        )
        with patch("chongming.modules.detection.cv2.createBackgroundSubtractorMOG2") as factory:
            assembly = declare_assembly(spec, run)
            factory.assert_not_called()
        for station in assembly.stations:
            self.assertIs(station.detection.detector, station.motion)
            self.assertIs(station.detection.tracker, station.tracking)
            self.assertTrue(station.motion.business_implemented)
            source = frame(
                station=str(station.config.station_id),
                session=str(station.tracking.station_session_id),
            )
            output = station.detection.process(source)
            self.assertEqual(output.detections.processing_status, m.ProcessingStatus.PROCESSED)
            self.assertEqual(output.detections.reason_codes, ("background_warmup",))
            self.assertFalse(output.detections.observations)
            self.assertFalse(output.tracks)

    def test_real_main_pipeline_keeps_predictions_out_of_detection_batches(self) -> None:
        module = MainDetectionModule(
            detector=MotionDetectionModule(policy=detection_policy()), tracker=tracker()
        )
        for index in range(2):
            module.process(frame(time_ns=index * 100 * MS))
        image = np.zeros((32, 32), dtype=np.uint8)
        image[8:12, 10:14] = 255
        detected = module.process(frame(image, time_ns=200 * MS))
        self.assertEqual(len(detected.detections.observations), 1)
        missing = module.process(frame(time_ns=300 * MS))
        self.assertEqual(missing.detections.processing_status, m.ProcessingStatus.PROCESSED)
        self.assertFalse(missing.detections.observations)
        self.assertEqual(missing.tracks[0].lifecycle, "coasting")
        self.assertEqual(missing.detections.reason_codes, ("no_foreground_candidates",))

    def test_main_failure_preserves_status_and_breaks_the_confirmation_streak(self) -> None:
        module = MainDetectionModule(
            detector=MotionDetectionModule(
                policy=detection_policy(background_warmup_frame_count=1)
            ),
            tracker=tracker(),
        )
        module.process(frame())
        image = np.zeros((32, 32), dtype=np.uint8)
        image[8:12, 10:14] = 255
        first = module.process(frame(image, time_ns=100 * MS))
        source = frame(image, time_ns=200 * MS)
        result = module.process(replace(source, image=replace(source.image, pixels=b"")))
        self.assertEqual(result.detections.processing_status, m.ProcessingStatus.FAILED)
        self.assertEqual(result.detections.reason_codes[0], "invalid_main_frame")
        self.assertFalse(result.detections.observations)
        self.assertEqual(result.tracks[0].lifecycle, "coasting")
        self.assertEqual(result.tracks[0].track_quality, 0)
        recovered = module.process(frame(image, time_ns=300 * MS))
        self.assertEqual(recovered.tracks[0].lifecycle, "tentative")
        self.assertEqual(recovered.tracks[0].local_track_id, first.tracks[0].local_track_id)

    def test_foreign_camera_role_is_rejected_before_tracking(self) -> None:
        mock_tracker = Mock()
        module = MainDetectionModule(
            detector=MotionDetectionModule(policy=detection_policy()), tracker=mock_tracker
        )
        with self.assertRaisesRegex(ValueError, "main camera"):
            module.process(replace(frame(), camera_role=m.CameraRole.AUXILIARY))
        mock_tracker.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
