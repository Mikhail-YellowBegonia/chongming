"""Checked binding to the separately provisioned, pinned official ViBe C library."""

from __future__ import annotations

import ctypes
import hashlib
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from chongming.vision.pipeline import PipelineContext, Stage

PREFIX = "libvibeModel_Sequential_"
DEFAULTS = {"NumberOfSamples": 30, "MatchingThreshold": 10, "MatchingNumber": 2, "UpdateFactor": 8}


def seed_native(seed: int) -> None:
    """Seed the official implementation's process-global libc RNG once per replay."""
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("ViBe seed must be a uint32 integer")
    srand = ctypes.CDLL(None).srand
    srand.argtypes = [ctypes.c_uint32]
    srand.restype = None
    srand(seed)


@dataclass(frozen=True)
class VibeConfig:
    library_path: str
    library_sha256: str


class VibeStage(Stage):
    def __init__(self, config: VibeConfig) -> None:
        path = Path(config.library_path).resolve(strict=True)
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != config.library_sha256:
            raise ValueError("ViBe shared-library digest mismatch")
        self.config = config
        self.library = ctypes.CDLL(str(path))
        self._new = getattr(self.library, PREFIX + "New")
        self._new.argtypes = []
        self._new.restype = ctypes.c_void_p
        self._free = getattr(self.library, PREFIX + "Free")
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = ctypes.c_int32
        self._init = getattr(self.library, PREFIX + "AllocInit_8u_C1R")
        self._init.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        self._init.restype = ctypes.c_int32
        self._segment = getattr(self.library, PREFIX + "Segmentation_8u_C1R")
        self._update = getattr(self.library, PREFIX + "Update_8u_C1R")
        for function in (self._segment, self._update):
            function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            function.restype = ctypes.c_int32
        self._model: Any = None
        self._release: weakref.finalize[..., VibeStage] | None = None
        self._shape: tuple[int, ...] | None = None
        self.parameters: dict[str, int] = {}

    def process(self, context: PipelineContext) -> None:
        image = context.preprocessed if context.preprocessed is not None else context.frame
        gray = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        if min(gray.shape) < 3:
            raise ValueError("ViBe requires images at least 3 by 3 pixels")
        if self._model is None:
            self._model = self._new()
            if not self._model:
                raise MemoryError("official ViBe model allocation failed")
            self._release = weakref.finalize(self, self._free, self._model)
            for key, expected in DEFAULTS.items():
                getter = getattr(self.library, PREFIX + "Get" + key)
                getter.argtypes = [ctypes.c_void_p]
                getter.restype = ctypes.c_uint32
                self.parameters[key] = int(getter(self._model))
                if self.parameters[key] != expected:
                    raise ValueError(
                        "ViBe library parameters differ from the declared official defaults"
                    )
            self._check(
                self._init(self._model, gray.ctypes.data, gray.shape[1], gray.shape[0]),
                "initialization",
            )
            self._shape = gray.shape
        if gray.shape != self._shape:
            raise ValueError("ViBe input dimensions changed without reset")
        mask = np.empty_like(gray)
        self._check(self._segment(self._model, gray.ctypes.data, mask.ctypes.data), "segmentation")
        # The update consumes the unfiltered native foreground map, before shared morphology.
        self._check(self._update(self._model, gray.ctypes.data, mask.ctypes.data), "update")
        context.motion_mask = mask
        context.debug_data["motion_mask"] = mask
        context.debug_data["vibe_parameters"] = self.parameters.copy()

    @staticmethod
    def _check(result: int, phase: str) -> None:
        if result != 0:
            raise RuntimeError(f"official ViBe {phase} failed with code {result}")

    def reset(self) -> None:
        if self._release is not None and self._release.alive:
            result = self._release()
            if result is not None:
                self._check(result, "cleanup")
        self._model = None
        self._shape = None
        self.parameters = {}
