"""Tracking stage: Kalman filter with asymmetric trust logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from chongming.vision.pipeline import Blob, Detection, PipelineContext, Stage


@dataclass
class TrackingConfig:
    """Configuration for tracking."""

    confirm_frames: int = 3  # Frames needed to confirm a new target
    max_lost_frames: int = 10  # Max frames to coast without observation
    association_gate_px: float = 50.0  # Max distance for blob-to-track matching
    covariance_floor: float = 5.0  # Minimum covariance to prevent overconfidence
    process_noise_pos: float = 10.0  # Process noise for position
    process_noise_vel: float = 5.0  # Process noise for velocity
    measurement_noise: float = 5.0  # Measurement noise


class KalmanFilter2D:
    """Simple 2D Kalman filter for constant velocity model.

    State: [x, y, vx, vy]
    Measurement: [x, y]
    """

    def __init__(self, initial_pos: tuple[float, float], config: TrackingConfig) -> None:
        self.config = config

        # State: [x, y, vx, vy]
        self.state = np.array([initial_pos[0], initial_pos[1], 0.0, 0.0], dtype=np.float32)

        # Covariance
        self.covariance = np.eye(4, dtype=np.float32) * 100.0

        # State transition matrix (constant velocity, dt=1/30 assumed)
        dt = 1.0 / 30.0
        self.F = np.array(
            [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float32
        )

        # Measurement matrix (observe position only)
        self.H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32)

        # Process noise
        q_pos = config.process_noise_pos
        q_vel = config.process_noise_vel
        self.Q = np.diag([q_pos, q_pos, q_vel, q_vel]).astype(np.float32)

        # Measurement noise
        self.R = np.eye(2, dtype=np.float32) * config.measurement_noise**2

    def predict(self) -> tuple[float, float]:
        """Predict next state.

        Returns:
            Predicted position (x, y)
        """
        self.state = self.F @ self.state
        self.covariance = self.F @ self.covariance @ self.F.T + self.Q

        # Apply covariance floor
        min_cov = self.config.covariance_floor**2
        np.fill_diagonal(self.covariance, np.maximum(np.diag(self.covariance), min_cov))

        return (float(self.state[0]), float(self.state[1]))

    def update(self, measurement: tuple[float, float]) -> None:
        """Update with measurement."""
        z = np.array([measurement[0], measurement[1]], dtype=np.float32)

        # Innovation
        y = z - self.H @ self.state

        # Innovation covariance
        S = self.H @ self.covariance @ self.H.T + self.R

        # Kalman gain
        K = self.covariance @ self.H.T @ np.linalg.inv(S)

        # Update state and covariance
        self.state = self.state + K @ y
        self.covariance = (np.eye(4) - K @ self.H) @ self.covariance

    def get_position(self) -> tuple[float, float]:
        """Get current position estimate."""
        return (float(self.state[0]), float(self.state[1]))

    def get_velocity(self) -> tuple[float, float]:
        """Get current velocity estimate."""
        return (float(self.state[2]), float(self.state[3]))


@dataclass
class Track:
    """A tracked target with lifecycle management."""

    track_id: int
    kalman: KalmanFilter2D
    lifecycle: Literal["tentative", "confirmed", "coasting"]
    consecutive_hits: int = 0  # Current uninterrupted observation streak
    frames_since_update: int = 0  # Frames since last measurement
    last_measurement_time: float = 0.0
    lifetime_frames: int = 0  # Total frames this track has existed
    measurement: Blob | None = None


class TrackingStage(Stage):
    """Track blobs over time with asymmetric trust logic.

    New targets need multiple confirmations before being output.
    Lost targets can coast for longer before being dropped.
    """

    def __init__(self, config: TrackingConfig) -> None:
        self.config = config
        self.tracks: list[Track] = []
        self.next_id = 1
        self.last_timestamp: float | None = None

    def process(self, context: PipelineContext) -> None:
        blobs = context.blobs
        timestamp = context.timestamp

        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
            raise ValueError("Tracking timestamps must increase strictly")
        dt = 1.0 / 30.0 if self.last_timestamp is None else timestamp - self.last_timestamp
        self.last_timestamp = timestamp
        # Predict all tracks and increment lifetime
        for track in self.tracks:
            track.measurement = None
            track.kalman.F[0, 2] = dt
            track.kalman.F[1, 3] = dt
            track.kalman.predict()
            track.frames_since_update += 1
            track.lifetime_frames += 1

        # Associate blobs to tracks
        unmatched_blobs, _unmatched_tracks = self._associate(blobs, self.tracks)

        # Update matched tracks
        for track in self.tracks:
            if track.frames_since_update == 0:  # Was matched this frame
                track.last_measurement_time = timestamp
                track.consecutive_hits += 1
                if track.lifecycle == "tentative":
                    if track.consecutive_hits >= self.config.confirm_frames:
                        track.lifecycle = "confirmed"
                elif track.lifecycle == "coasting":
                    track.lifecycle = "confirmed"

        for track in self.tracks:
            if track.frames_since_update > 0:
                track.consecutive_hits = 0

        # Handle coasting tracks
        for track in self.tracks:
            if track.frames_since_update > 0 and track.lifecycle == "confirmed":
                track.lifecycle = "coasting"

        # Remove lost tracks
        self.tracks = [
            t for t in self.tracks if t.frames_since_update <= self.config.max_lost_frames
        ]

        # Create new tracks for unmatched blobs
        for blob in unmatched_blobs:
            new_track = Track(
                track_id=self.next_id,
                kalman=KalmanFilter2D(blob.centroid_px, self.config),
                lifecycle="confirmed" if self.config.confirm_frames == 1 else "tentative",
                consecutive_hits=1,
                frames_since_update=0,
                last_measurement_time=timestamp,
                lifetime_frames=1,
                measurement=blob,
            )
            self.tracks.append(new_track)
            self.next_id += 1

        # Output only confirmed tracks
        detections = []
        for track in self.tracks:
            if track.lifecycle == "confirmed" and track.measurement is not None:
                pos = track.kalman.get_position()
                vel = track.kalman.get_velocity()
                detections.append(
                    Detection(
                        track_id=track.track_id,
                        position_px=pos,
                        velocity_pxps=vel,
                        timestamp=timestamp,
                        measurement_px=track.measurement.centroid_px,
                        measurement_score=min(
                            1.0,
                            track.measurement.area_px2
                            / (
                                track.measurement.bounding_box[2]
                                * track.measurement.bounding_box[3]
                            ),
                        ),
                    )
                )

        context.detections = detections
        context.debug_data["track_count"] = len(self.tracks)
        context.debug_data["confirmed_count"] = len(detections)

    def _associate(self, blobs: list[Blob], tracks: list[Track]) -> tuple[list[Blob], list[Track]]:
        """Associate blobs to tracks using nearest neighbor within gate.

        Returns:
            (unmatched_blobs, unmatched_tracks)
        """
        if not blobs or not tracks:
            return (blobs, tracks)

        # Same row-major greedy minimum as the workbench, without Python cubic loops.
        positions = np.asarray([b.centroid_px for b in blobs], dtype=np.float32)
        predictions = np.asarray([t.kalman.get_position() for t in tracks], dtype=np.float32)
        distances = np.linalg.norm(positions[:, None, :] - predictions[None, :, :], axis=2)
        matched_blobs: set[int] = set()
        matched_tracks: set[int] = set()
        while True:
            min_i, min_j = np.unravel_index(np.argmin(distances), distances.shape)
            if distances[min_i, min_j] > self.config.association_gate_px:
                break
            i, j = int(min_i), int(min_j)
            matched_blobs.add(i)
            matched_tracks.add(j)
            tracks[j].kalman.update(blobs[i].centroid_px)
            tracks[j].frames_since_update = 0
            tracks[j].measurement = blobs[i]
            distances[i, :] = np.inf
            distances[:, j] = np.inf

        unmatched_blobs = [b for i, b in enumerate(blobs) if i not in matched_blobs]
        unmatched_tracks = [t for j, t in enumerate(tracks) if j not in matched_tracks]

        return (unmatched_blobs, unmatched_tracks)

    def reset(self) -> None:
        """Clear all tracks."""
        self.tracks.clear()
        self.next_id = 1
        self.last_timestamp = None
