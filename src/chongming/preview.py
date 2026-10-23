"""Local scene video preview, independent of unfinished online algorithms.

A single producer owns ScenarioSource. Browsers only read an atomic latest
snapshot; their polling/render rate never creates exposures or advances time.
Video encodings carry only ordinary CameraFrame pixels. Privileged annotations
are a separate monitor field and are drawn solely in the development UI.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import logging
import math
import os
import secrets
import statistics
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from chongming.contracts.model import CameraFrame
from chongming.contracts.scene import CameraFrameTruth, ScenarioSpec, ScenarioStep
from chongming.rendering.godot import close_default_renderer, default_renderer
from chongming.scenarios.authoring import compile_authoring, save_authoring
from chongming.scenarios.demo import demo_source, demo_spec
from chongming.scenarios.serialization import load_scenario

logger = logging.getLogger(__name__)

# Only provisioned, pinned local assets can be selected from the editor.
_LANDSCAPE_ASSETS = {
    "hannover": (
        Path(__file__).resolve().parents[2] / "output/osm/hannover-500m/landscape-dem.json",
        "85a4f6bd9a4b0df848f96dcf2e003cddbf510c06f1c85cc9a6fcbea0fd4b9d78",
    ),
}


class DiscreteRenderClock:
    """Wall-clock pacing for preview rendering, independent of experiment time.

    A tick only controls when the next already-defined experiment step is
    produced. It never changes exposure timestamps, advances the experiment
    clock, or drops a step from a quantitative scenario source.
    """

    def __init__(self, render_fps: float) -> None:
        if not 0 < render_fps <= 240:
            raise ValueError("render_fps must be greater than 0 and at most 240")
        self.render_fps = float(render_fps)
        self.period_ns = max(1, round(1_000_000_000 / self.render_fps))
        self._next_tick_ns: int | None = None

    def begin(self) -> None:
        self._next_tick_ns = time.perf_counter_ns() + self.period_ns

    def wait_for_next_tick(
        self, condition: threading.Condition, interrupted: Callable[[], bool]
    ) -> None:
        """Wait until the next render tick without blocking shutdown/control."""
        with condition:
            now = time.perf_counter_ns()
            if self._next_tick_ns is None:
                self.begin()
            assert self._next_tick_ns is not None
            delay_ns = self._next_tick_ns - now
            if delay_ns > 0 and condition.wait_for(interrupted, timeout=delay_ns / 1_000_000_000):
                return
            now = time.perf_counter_ns()
            if self._next_tick_ns <= now:
                missed = (now - self._next_tick_ns) // self.period_ns + 1
                self._next_tick_ns += missed * self.period_ns
            else:
                self._next_tick_ns += self.period_ns


def encode_video(frame: CameraFrame) -> str:
    """Lossless PNG transport of the ordinary RGB8 plane, without annotations."""
    image = Image.frombytes(
        "RGB",
        (frame.image.width_px, frame.image.height_px),
        bytes(frame.image.pixels),
        "raw",
        "RGB",
        frame.image.row_stride_bytes,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=1)
    return base64.b64encode(output.getvalue()).decode("ascii")


def _video_payload(step: ScenarioStep, station_id: str | None) -> list[dict[str, Any]]:
    return [
        {
            "station_id": frame.station_id,
            "role": frame.camera_role.value,
            "frame_id": frame.frame_id,
            "session_id": frame.station_session_id,
            "width": frame.image.width_px,
            "height": frame.image.height_px,
            "capture": asdict(frame.capture_time),
            "png": encode_video(frame),
        }
        for frame in step.video.frames
        if station_id is None or frame.station_id == station_id
    ]


def _preview_metadata(step: ScenarioStep, sequence: int, loop_no: int) -> dict[str, Any]:
    scene = step.truth.logical_scene
    return {
        "sequence": sequence,
        "loop": loop_no,
        "time_ns": step.video.sample_time.time_ns,
        "step_no": step.video.sample_time.step_no,
        "rendered": step.video.rendered,
        "render_skip_reason": step.video.render_skip_reason,
        "skipped_frame_ids": list(step.video.skipped_frame_ids),
        "packets": {
            "calibration_event_count": len(step.packets.calibration_events),
            "ray_count": len(step.packets.rays),
            "diagnostic_count": len(step.packets.diagnostics),
            "rays": [asdict(item) for item in step.packets.rays],
        },
        "monitor": {
            "terrain": asdict(scene.terrain),
            "world_objects": [asdict(item) for item in scene.world_objects],
            "vegetation": [asdict(item) for item in scene.vegetation],
            "cameras": [asdict(item) for item in scene.cameras],
            "flying_objects": [asdict(item) for item in scene.flying_objects],
            "visibility": [asdict(item) for item in scene.target_visibility],
            "frame_truth": [asdict(item) for item in step.truth.camera_frames],
        },
    }


def preview_payload(step: ScenarioStep, sequence: int, loop_no: int) -> dict[str, Any]:
    return {**_preview_metadata(step, sequence, loop_no), "video": _video_payload(step, None)}


class PreviewRuntime:
    """Bounded latest-frame buffer and one simulation worker, shared by all viewers."""

    def __init__(
        self,
        spec: ScenarioSpec,
        *,
        render_fps: float = 10.0,
        video_enabled: bool = True,
        authoring_document: dict[str, Any] | None = None,
        authoring_path: Path | None = None,
    ) -> None:
        self.spec = spec
        self._authoring_document = copy.deepcopy(authoring_document) if authoring_document else None
        self._authoring_path = authoring_path
        self._editing = False
        self._edit_token: str | None = None
        self._draft: dict[str, Any] | None = None
        self._edit_digest: str | None = None
        self._video_before_edit: bool | None = None
        self._paused_before_edit: bool | None = None
        self._freeview_before_edit: bool | None = None
        self._freeview_enabled = False
        self._edit_lock = threading.RLock()
        self._producing = False
        self.video_enabled = video_enabled
        self.render_clock = DiscreteRenderClock(render_fps)
        minimum_period = min(
            (camera.exposure.frame_period_ns for camera in spec.scene_definition.cameras),
            default=self.render_clock.period_ns,
        )
        # Allow one nanosecond of period quantization: floor(1e9 / 15)
        # must still select every second exposure for a 7.5 Hz render clock.
        self.render_every = max(1, math.ceil(self.render_clock.period_ns / (minimum_period + 1)))
        self._condition = threading.Condition()
        self._stopped = False
        self._paused = False
        self._pending_steps = 0
        self._reset_version = 0
        self._published: dict[str, Any] | None = None
        self._latest_step: ScenarioStep | None = None
        self._encoded: dict[str, dict[str, Any]] = {}
        self._encode_lock = threading.Lock()
        self._station_ids = tuple(
            dict.fromkeys(str(camera.station_id) for camera in spec.scene_definition.cameras)
        )
        self._error: str | None = None
        self._generation_samples_ms: deque[float] = deque(maxlen=120)
        self._rendered_samples_ms: deque[float] = deque(maxlen=120)
        self._last_logical_timing: dict[str, Any] | None = None
        self._gpu_submissions = 0
        self._logical_step_count = 0
        self._rendered_step_count = 0
        self._skipped_step_count = 0
        self._thread = threading.Thread(
            target=self._produce, name="scene-video-producer", daemon=True
        )

    def start(self) -> None:
        default_renderer()
        self._thread.start()

    def close(self) -> None:
        if self._editing:
            self.cancel_edit(closing=True)
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            raise RuntimeError("scene video producer did not stop")
        close_default_renderer()

    def control(self, action: str) -> None:
        with self._condition:
            if self._editing and action != "pause":
                raise ValueError("experiment controls are locked during editing")
            if action == "pause":
                self._paused = True
                self._pending_steps = 0
            elif action == "play":
                self._paused = False
                self._pending_steps = 0
            elif action == "step":
                self._paused = True
                self._pending_steps = min(self._pending_steps + 1, 10)
            elif action == "restart":
                self._reset_version += 1
                self._pending_steps = 0
            else:
                raise ValueError("unknown preview control")
            self._condition.notify_all()

    def editor_status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "active": self._editing,
                "locked": self._editing,
                "document": copy.deepcopy(
                    self._draft if self._editing else self._authoring_document
                ),
                "path": str(self._authoring_path) if self._authoring_path else None,
                "freeview": self._freeview_enabled,
                "landscapes": [
                    name for name, (path, _digest) in _LANDSCAPE_ASSETS.items() if path.is_file()
                ],
            }

    def begin_edit(self) -> dict[str, Any]:
        with self._edit_lock, self._condition:
            if self._authoring_document is None:
                raise ValueError("editor requires an authoring JSON scenario")
            if self._editing:
                raise ValueError("another editor session already holds the lock")
            was_paused = self._paused
            self._paused_before_edit = was_paused
            self._freeview_before_edit = self._freeview_enabled
            self._paused = True
            self._pending_steps = 0
            self._condition.notify_all()
            if not self._condition.wait_for(lambda: not self._producing, timeout=30):
                self._paused = was_paused
                self._paused_before_edit = None
                self._condition.notify_all()
                raise RuntimeError("preview producer did not reach an editing boundary")
            if self._authoring_path is None:
                self._paused_before_edit = None
                self._paused = was_paused
                raise ValueError("editor requires an authoring path")
            try:
                raw = self._authoring_path.read_bytes()
                document = json.loads(raw)
                compiled = compile_authoring(document, asset_root=self._authoring_path.parent)
            except Exception:
                self._paused = was_paused
                self._paused_before_edit = None
                self._freeview_before_edit = None
                self._condition.notify_all()
                raise
            self._edit_digest = hashlib.sha256(raw).hexdigest()
            self._authoring_document = copy.deepcopy(document)
            self._draft = copy.deepcopy(document)
            self.spec = compiled
            self._editing = True
            self._edit_token = secrets.token_urlsafe(24)
            self._video_before_edit = self.video_enabled
            self._freeview_enabled = True
            # The freeview has its own native render loop. Do not spend time
            # producing sensor images while the experiment is locked for edits.
            self.video_enabled = False
            self._reset_version += 1
            if self._latest_step is None:
                self._pending_steps = 1
            if self._latest_step is not None:
                self._push_editor(True, reopen=True)
            self._condition.notify_all()
        return {**self.editor_status(), "token": self._edit_token}

    def set_freeview(self, enabled: bool) -> dict[str, Any]:
        """Enable the renderer-owned freeview without taking the edit lock."""
        with self._edit_lock:
            with self._condition:
                if self._editing and not enabled:
                    raise ValueError("freeview is required while editing")
                self._freeview_enabled = bool(enabled)
                self._condition.notify_all()
            self._push_editor(self._freeview_enabled, reopen=enabled)
        return self.editor_status()

    def _require_edit(self, token: str | None = None) -> dict[str, Any]:
        if not self._editing or self._draft is None:
            raise ValueError("edit session is not active")
        if token != self._edit_token:
            raise ValueError("edit session token is missing or invalid")
        return self._draft

    def edit_update(
        self, kind: str, object_id: str, patch: dict[str, Any], *, token: str | None = None
    ) -> dict[str, Any]:
        with self._edit_lock:
            return self._edit_update(kind, object_id, patch, token)

    def _edit_update(
        self, kind: str, object_id: str, patch: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        document = copy.deepcopy(self._require_edit(token))
        if not isinstance(patch, dict) or not patch:
            raise ValueError("edit patch must be a nonempty object")
        if kind == "station":
            entries = document.get("stations", [])
            allowed = {"position_enu_m", "main_yaw_deg", "main_elevation_deg", "auxiliary_yaw_deg"}
        elif kind == "target":
            entries = document.get("targets", [])
            allowed = {"display_name", "geometry", "motion"}
        else:
            raise ValueError("editable kind must be station or target")
        if not isinstance(entries, list):
            raise TypeError(f"{kind}s must be an array")
        item = next((entry for entry in entries if entry.get("id") == object_id), None)
        if item is None:
            raise ValueError(f"unknown {kind} id: {object_id}")
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unsupported edit fields: {sorted(unknown)}")
        item.update(copy.deepcopy(patch))
        self._publish_draft(document)
        return self.editor_status()

    def _publish_draft(self, document: dict[str, Any]) -> None:
        compiled = compile_authoring(
            document, asset_root=self._authoring_path.parent if self._authoring_path else None
        )
        with self._condition:
            self._draft = document
            self.spec = compiled
            self._reset_version += 1
            self._pending_steps = 1
            self._condition.notify_all()

    def edit_create(self, kind: str, object_id: str, *, token: str | None = None) -> dict[str, Any]:
        with self._edit_lock:
            document = copy.deepcopy(self._require_edit(token))
            if (
                not object_id
                or len(object_id) > 64
                or not all(
                    char.isascii() and (char.isalnum() or char in "_-") for char in object_id
                )
            ):
                raise ValueError("ID must be 1-64 ASCII letters, digits, underscores or hyphens")
            if kind == "station":
                entries = document.setdefault("stations", [])
                if len(entries) >= 4:
                    raise ValueError("at most four stations are supported")
                new = {
                    "id": object_id,
                    "position_enu_m": [0, 0, 8],
                    "main_yaw_deg": 0,
                    "main_elevation_deg": 0,
                }
            elif kind == "target":
                entries = document.setdefault("targets", [])
                new = {
                    "id": object_id,
                    "geometry": {"type": "sphere", "radius_m": 1},
                    "motion": {
                        "type": "constant_velocity",
                        "position_enu_m": [0, 0, 40],
                        "velocity_enu_mps": [0, 0, 0],
                    },
                }
            else:
                raise ValueError("editable kind must be station or target")
            if any(item.get("id") == object_id for item in entries):
                raise ValueError("ID already exists")
            entries.append(new)
            self._publish_draft(document)
            return self.editor_status()

    def edit_delete(self, kind: str, object_id: str, *, token: str | None = None) -> dict[str, Any]:
        with self._edit_lock:
            document = copy.deepcopy(self._require_edit(token))
            field = {"station": "stations", "target": "targets"}.get(kind)
            if field is None:
                raise ValueError("editable kind must be station or target")
            entries = document.get(field, [])
            if kind == "station" and len(entries) <= 1:
                raise ValueError("at least one station is required")
            filtered = [item for item in entries if item.get("id") != object_id]
            if len(filtered) == len(entries):
                raise ValueError("unknown object ID")
            document[field] = filtered
            self._publish_draft(document)
            return self.editor_status()

    def edit_landscape(self, landscape_id: str, *, token: str | None = None) -> dict[str, Any]:
        with self._edit_lock:
            document = copy.deepcopy(self._require_edit(token))
            if landscape_id not in _LANDSCAPE_ASSETS or self._authoring_path is None:
                raise ValueError("landscape is not provisioned")
            path, digest = _LANDSCAPE_ASSETS[landscape_id]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("local landscape asset is missing or has a different digest")
            document["osm"] = {
                "asset": {
                    "uri": os.path.relpath(path, self._authoring_path.parent),
                    "sha256": digest,
                }
            }
            self._publish_draft(document)
            return self.editor_status()

    def cancel_edit(self, *, token: str | None = None, closing: bool = False) -> dict[str, Any]:
        with self._edit_lock:
            if not self._editing:
                return self.editor_status()
            if not closing:
                self._require_edit(token)
            document = self._authoring_document
            compiled = None
            if self._authoring_path is not None:
                document = json.loads(self._authoring_path.read_bytes())
                compiled = compile_authoring(document, asset_root=self._authoring_path.parent)
            with self._condition:
                if compiled is not None:
                    self.spec = compiled
                    self._authoring_document = copy.deepcopy(document)
                self._push_editor(False)
                self._draft = None
                self._edit_digest = None
                self._edit_token = None
                self._editing = False
                if self._video_before_edit is not None:
                    self.video_enabled = self._video_before_edit
                self._video_before_edit = None
                if self._paused_before_edit is not None:
                    self._paused = self._paused_before_edit
                self._paused_before_edit = None
                if self._freeview_before_edit is not None:
                    self._freeview_enabled = self._freeview_before_edit
                self._freeview_before_edit = None
                self._reset_version += 1
                self._condition.notify_all()
            self._push_editor(self._freeview_enabled)
        return self.editor_status()

    def commit_edit(self, *, token: str | None = None) -> dict[str, Any]:
        with self._edit_lock:
            document = self._require_edit(token)
            if self._authoring_path is None or self._edit_digest is None:
                raise ValueError("edit session has no authoring path")
            compiled = compile_authoring(document, asset_root=self._authoring_path.parent)
            if hashlib.sha256(self._authoring_path.read_bytes()).hexdigest() != self._edit_digest:
                raise ValueError("authoring JSON changed outside the editor; cancel and reload")
            save_authoring(document, self._authoring_path)
            with self._condition:
                self._push_editor(False)
                self.spec = compiled
                self._authoring_document = copy.deepcopy(document)
                self._station_ids = tuple(str(c.station_id) for c in compiled.station_configs)
                self.render_every = max(
                    1,
                    math.ceil(
                        self.render_clock.period_ns
                        / (
                            min(
                                (
                                    c.exposure.frame_period_ns
                                    for c in compiled.scene_definition.cameras
                                ),
                                default=self.render_clock.period_ns,
                            )
                            + 1
                        )
                    ),
                )
                self._draft = None
                self._edit_digest = None
                self._edit_token = None
                self._reset_version += 1
                self._editing = False
                if self._video_before_edit is not None:
                    self.video_enabled = self._video_before_edit
                self._video_before_edit = None
                if self._paused_before_edit is not None:
                    self._paused = self._paused_before_edit
                self._paused_before_edit = None
                if self._freeview_before_edit is not None:
                    self._freeview_enabled = self._freeview_before_edit
                self._freeview_before_edit = None
                self._condition.notify_all()
            self._push_editor(self._freeview_enabled)
        return self.editor_status()

    def _push_editor(self, enabled: bool, *, reopen: bool = False) -> None:
        with self._condition:
            step = self._latest_step
        if step is None or not step.truth.logical_scene.cameras:
            return
        camera = step.truth.logical_scene.cameras[0]
        with self._condition:
            editing = self._editing
        visible = default_renderer().editor_update(
            step.truth.logical_scene,
            camera,
            enabled=enabled,
            realtime=editing,
            reopen=reopen or (editing and enabled),
        )
        with self._condition:
            self._freeview_enabled = visible or (editing and enabled)

    def snapshot(
        self, station_id: str | None = None, after_sequence: int | None = None
    ) -> dict[str, Any]:
        station_id = station_id or self._station_ids[0]
        if station_id not in self._station_ids:
            raise ValueError("unknown preview station")
        with self._condition:
            result = {
                "state": "failed" if self._error else "paused" if self._paused else "playing",
                "error": self._error,
                "snapshot": self._published,
                "profile": self.spec.scene_definition.cameras[0].metadata.profile_id,
                "render_clock_fps": self.render_clock.render_fps,
                "render_every": self.render_every,
                "logical_step_count": self._logical_step_count,
                "rendered_step_count": self._rendered_step_count,
                "skipped_step_count": self._skipped_step_count,
                "gpu_submissions": self._gpu_submissions,
                "last_logical_timing": self._last_logical_timing,
                "generation_stats_ms": self._timing_stats(self._generation_samples_ms),
                "rendered_generation_stats_ms": self._timing_stats(self._rendered_samples_ms),
            }
            step, payload, cache = self._latest_step, self._published, self._encoded
        if payload is not None and payload["sequence"] == after_sequence:
            result["snapshot"] = None
            return result
        if step is not None and payload is not None:
            # Encode on demand outside the producer's lock. All viewers share
            # one per-station cache for this immutable latest sample. Replacing
            # the cache on publication bounds history; paused polls reuse PNGs.
            with self._encode_lock:
                encoded = cache.get(station_id)
                if encoded is None:
                    began = time.perf_counter()
                    encoded = {"video": _video_payload(step, station_id)}
                    encoded["png_encode_ms"] = round((time.perf_counter() - began) * 1000, 2)
                    cache[station_id] = encoded
            result["snapshot"] = {**payload, **encoded, "preview_station_id": station_id}
        return result

    @staticmethod
    def _timing_stats(values: deque[float]) -> dict[str, Any]:
        samples = sorted(values)
        return {
            "p50": round(statistics.median(samples), 2) if samples else None,
            "p95": round(samples[max(0, math.ceil(len(samples) * 0.95) - 1)], 2)
            if samples
            else None,
            "max": round(max(samples), 2) if samples else None,
            "sample_count": len(samples),
            "window_capacity": values.maxlen,
        }

    def _produce(self) -> None:
        sequence, loop_no, reset_version = 0, 1, 0
        latest_frames: dict[tuple[str, str], CameraFrame] = {}
        latest_truths: dict[tuple[str, str], CameraFrameTruth] = {}
        try:
            source = demo_source(
                self.spec, render_every=self.render_every, video_enabled=self.video_enabled
            )
            steps = iter(source.generate(self.spec))
            while True:

                def ready(version: int = reset_version) -> bool:
                    return (
                        self._stopped
                        or not self._paused
                        or self._pending_steps > 0
                        or self._reset_version != version
                    )

                with self._condition:
                    self._condition.wait_for(ready)
                    if self._stopped:
                        return
                    if self._reset_version != reset_version:
                        reset_version = self._reset_version
                        source = demo_source(
                            self.spec,
                            render_every=self.render_every,
                            video_enabled=self.video_enabled,
                        )
                        steps = iter(source.generate(self.spec))
                        loop_no += 1
                        latest_frames.clear()
                        latest_truths.clear()
                        # Restart publishes frame zero even while paused.
                    self._producing = True
                began = time.perf_counter()
                try:
                    step = next(steps)
                except StopIteration:
                    source = demo_source(
                        self.spec, render_every=self.render_every, video_enabled=self.video_enabled
                    )
                    steps = iter(source.generate(self.spec))
                    loop_no += 1
                    latest_frames.clear()
                    latest_truths.clear()
                    step = next(steps)
                generated = time.perf_counter()
                timing = step.truth.timing
                assert timing is not None
                with self._condition:
                    self._logical_step_count += 1
                    self._generation_samples_ms.append(timing.total_ms)
                    self._last_logical_timing = asdict(timing)
                    self._gpu_submissions += timing.gpu_submissions
                with self._condition:
                    editing = self._editing
                    video_enabled = self.video_enabled
                if not step.video.rendered and video_enabled and not editing:
                    # Logic, virtual events and privileged truth were still generated;
                    # this tick simply has no pixel payload to publish or encode.
                    with self._condition:
                        self._skipped_step_count += 1
                        self._producing = False
                        self._condition.notify_all()
                    with self._edit_lock:
                        if self._freeview_enabled and reset_version == self._reset_version:
                            with self._condition:
                                self._latest_step = step
                            self._push_editor(True)
                    continue
                with self._condition:
                    if step.video.rendered:
                        self._rendered_step_count += 1
                        self._rendered_samples_ms.append(timing.total_ms)
                    else:
                        self._skipped_step_count += 1
                    stats = self._timing_stats(self._generation_samples_ms)
                    render_stats = self._timing_stats(self._rendered_samples_ms)
                for frame in step.video.frames:
                    key = (str(frame.station_id), frame.camera_role.value)
                    latest_frames[key] = frame
                    latest_truths[key] = next(
                        t for t in step.truth.camera_frames if t.frame_id == frame.frame_id
                    )
                # Each role retains its own last rendered capture and matching
                # overlay truth. Independent schedules never blank the other role.
                display_step = replace(
                    step,
                    video=replace(step.video, frames=tuple(latest_frames.values())),
                    truth=replace(
                        step.truth,
                        camera_frames=(
                            step.truth.camera_frames if editing else tuple(latest_truths.values())
                        ),
                    ),
                )
                payload = _preview_metadata(display_step, sequence + 1, loop_no)
                payload.update(
                    gpu_render_ms=round(timing.gpu_ms, 2),
                    imager_overhead_ms=round(max(0, timing.render_ms - timing.gpu_ms), 2),
                    scene_logic_ms=round(timing.logic_ms + timing.frame_truth_ms, 2),
                    virtual_ms=round(timing.virtual_ms, 2),
                    render_ms=round((generated - began) * 1000, 2),
                    generation_stats_ms=stats,
                    rendered_generation_stats_ms=render_stats,
                )
                with self._condition:
                    if reset_version != self._reset_version:
                        self._producing = False
                        self._condition.notify_all()
                        continue
                    sequence += 1
                    if self._pending_steps:
                        self._pending_steps -= 1
                    self._published = payload
                    self._latest_step = display_step
                    self._encoded = {}
                    self._producing = False
                    self._condition.notify_all()

                # Serialize with cancel/commit so a stale producer completion
                # cannot reopen the native freeview after editing ended.
                with self._edit_lock:
                    if self._freeview_enabled:
                        self._push_editor(True)

                def interrupted(version: int = reset_version) -> bool:
                    return self._stopped or self._paused or self._reset_version != version

                # This is wall-clock pacing only. The source's experiment
                # timestamps remain authoritative and are never rewritten.
                self.render_clock.wait_for_next_tick(self._condition, interrupted)
        except Exception as error:
            logger.exception("Scene video producer failed")
            with self._condition:
                self._producing = False
                self._error = f"{type(error).__name__}: {error}"
                self._condition.notify_all()


def preview_server(runtime: PreviewRuntime, port: int) -> ThreadingHTTPServer:
    page = files("chongming.web").joinpath("preview.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                self.reply(200, page, "text/html; charset=utf-8")
            elif path == "/api/preview":
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    station_id = query.get("station", [None])[0]
                    after = query.get("after", [None])[0]
                    snapshot = runtime.snapshot(
                        station_id, int(after) if after is not None else None
                    )
                except ValueError:
                    self.reply(400, b"Invalid preview request", "text/plain")
                    return
                self.reply(
                    200,
                    json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode(),
                    "application/json; charset=utf-8",
                )
            elif path in {"/api/editor", "/api/freeview"}:
                self.reply(
                    200,
                    json.dumps(runtime.editor_status(), ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                )
            else:
                self.reply(404, b"Not found", "text/plain")

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.reply(403, b"Same-origin controls only", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("invalid request body length")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError("request body must be a JSON object")
            except (ValueError, TypeError, json.JSONDecodeError):
                self.reply(400, b'{"error":"Invalid JSON request body"}', "application/json")
                return
            if self.path == "/api/editor/begin":
                try:
                    result = runtime.begin_edit()
                except (ValueError, OSError, RuntimeError) as error:
                    self.reply(409, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path == "/api/editor/cancel":
                try:
                    result = runtime.cancel_edit(token=body.get("token"))
                except (ValueError, OSError, RuntimeError) as error:
                    self.reply(409, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path == "/api/editor/commit":
                try:
                    result = runtime.commit_edit(token=body.get("token"))
                except (ValueError, OSError, RuntimeError) as error:
                    self.reply(409, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path == "/api/editor/update":
                try:
                    result = runtime.edit_update(
                        str(body["kind"]), str(body["id"]), body["patch"], token=body.get("token")
                    )
                except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
                    self.reply(400, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path in {"/api/editor/create", "/api/editor/delete", "/api/editor/landscape"}:
                try:
                    if self.path.endswith("/create"):
                        result = runtime.edit_create(
                            str(body["kind"]), str(body["id"]), token=body.get("token")
                        )
                    elif self.path.endswith("/delete"):
                        result = runtime.edit_delete(
                            str(body["kind"]), str(body["id"]), token=body.get("token")
                        )
                    else:
                        result = runtime.edit_landscape(
                            str(body["landscape_id"]), token=body.get("token")
                        )
                except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
                    self.reply(400, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path == "/api/freeview":
                try:
                    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
                        raise TypeError("freeview requires a boolean enabled field")
                    result = runtime.set_freeview(body["enabled"])
                except (ValueError, TypeError, OSError, RuntimeError) as error:
                    self.reply(409, json.dumps({"error": str(error)}).encode(), "application/json")
                    return
                self.reply(200, json.dumps(result, ensure_ascii=False).encode(), "application/json")
                return
            if self.path != "/api/control":
                self.reply(404, b"Not found", "text/plain")
                return
            try:
                if not isinstance(body, dict) or not isinstance(body.get("action"), str):
                    raise TypeError("control requires a string action")
                runtime.control(body["action"])
            except (ValueError, KeyError, TypeError):
                self.reply(400, b"Invalid control", "text/plain")
                return
            self.reply(200, b'{"ok":true}', "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="启动虚拟场景主辅视频与场景总览")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stations", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--fps", type=int, default=30, help="实验采样率；默认 30，图像分辨率固定")
    parser.add_argument(
        "--render-fps",
        type=float,
        default=10.0,
        help="独立的预览渲染墙钟频率；不改变实验时间戳，默认 10",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="从 scenario-authoring-v2 文件加载可编辑场景；提供后忽略 --stations/--fps",
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-video", action="store_true", help="只看逻辑与事件，完全停止像素生产")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        authoring_path = Path(args.scenario) if args.scenario is not None else None
        authoring_document = None
        if authoring_path is not None:
            authoring_document = json.loads(authoring_path.read_text())
        spec = (
            load_scenario(authoring_path, asset_root=authoring_path.parent)
            if authoring_path is not None
            else demo_spec(station_count=args.stations, fps=args.fps)
        )
        runtime = PreviewRuntime(
            spec,
            render_fps=args.render_fps,
            video_enabled=not args.no_video,
            authoring_document=authoring_document,
            authoring_path=authoring_path,
        )
        server = preview_server(runtime, args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    url = f"http://127.0.0.1:{server.server_port}/"
    runtime.start()
    print(
        f"虚拟场景预览：{url}\nCtrl+C 停止。视频采样来自实验时钟，页面刷新不会推进时间。",
        flush=True,
    )
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
        close_default_renderer()


if __name__ == "__main__":
    main()
