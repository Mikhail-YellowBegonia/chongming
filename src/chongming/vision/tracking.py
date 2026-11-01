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
    tentative_hits: int = 0  # Consecutive hits while tentative
    frames_since_update: int = 0  # Frames since last measurement
    last_measurement_time: float = 0.0
    lifetime_frames: int = 0  # Total frames this track has existed


class TrackingStage(Stage):
    """Track blobs over time with asymmetric trust logic.

    New targets need multiple confirmations before being output.
    Lost targets can coast for longer before being dropped.
    """

    def __init__(self, config: TrackingConfig) -> None:
        self.config = config
        self.tracks: list[Track] = []
        self.next_id = 1

    def process(self, context: PipelineContext) -> None:
        blobs = context.blobs
        timestamp = context.timestamp

        # Predict all tracks and increment lifetime
        for track in self.tracks:
            track.kalman.predict()
            track.frames_since_update += 1
            track.lifetime_frames += 1

        # Associate blobs to tracks
        unmatched_blobs, _unmatched_tracks = self._associate(blobs, self.tracks)

        # Update matched tracks
        for track in self.tracks:
            if track.frames_since_update == 0:  # Was matched this frame
                track.last_measurement_time = timestamp
                if track.lifecycle == "tentative":
                    track.tentative_hits += 1
                    if track.tentative_hits >= self.config.confirm_frames:
                        track.lifecycle = "confirmed"
                elif track.lifecycle == "coasting":
                    track.lifecycle = "confirmed"

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
                lifecycle="tentative",
                tentative_hits=1,
                frames_since_update=0,
                last_measurement_time=timestamp,
                lifetime_frames=1,
            )
            self.tracks.append(new_track)
            self.next_id += 1

        # Output only confirmed tracks
        detections = []
        for track in self.tracks:
            if track.lifecycle == "confirmed":
                pos = track.kalman.get_position()
                vel = track.kalman.get_velocity()
                detections.append(
                    Detection(
                        track_id=track.track_id,
                        position_px=pos,
                        velocity_pxps=vel,
                        timestamp=timestamp,
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

        # Build distance matrix
        distances = np.zeros((len(blobs), len(tracks)), dtype=np.float32)
        for i, blob in enumerate(blobs):
            for j, track in enumerate(tracks):
                predicted_pos = track.kalman.get_position()
                dist = np.linalg.norm(np.array(blob.centroid_px) - np.array(predicted_pos))
                distances[i, j] = dist

        # Greedy nearest-neighbor matching within gate
        matched_blobs = set()
        matched_tracks = set()

        while True:
            min_dist = np.inf
            min_i, min_j = -1, -1

            for i in range(len(blobs)):
                if i in matched_blobs:
                    continue
                for j in range(len(tracks)):
                    if j in matched_tracks:
                        continue
                    if distances[i, j] < min_dist:
                        min_dist = distances[i, j]
                        min_i, min_j = i, j

            if min_dist > self.config.association_gate_px:
                break

            # Match found
            matched_blobs.add(min_i)
            matched_tracks.add(min_j)

            # Update track with measurement
            tracks[min_j].kalman.update(blobs[min_i].centroid_px)
            tracks[min_j].frames_since_update = 0

        unmatched_blobs = [b for i, b in enumerate(blobs) if i not in matched_blobs]
        unmatched_tracks = [t for j, t in enumerate(tracks) if j not in matched_tracks]

        return (unmatched_blobs, unmatched_tracks)

    def reset(self) -> None:
        """Clear all tracks."""
        self.tracks.clear()
        self.next_id = 1
