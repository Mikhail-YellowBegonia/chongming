"""Validated, named Gaussian streams for generation, never online algorithm input."""

from __future__ import annotations

import hashlib
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray


@lru_cache(maxsize=256)
def covariance_factor(
    covariance: tuple[tuple[float, ...], ...], dimension: int, label: str
) -> NDArray[np.float64]:
    """Return a PSD square root; singular and exactly zero covariances are legal.

    Validation is relative to matrix scale, so tiny negative variances are not
    accepted merely because their units make them numerically small.
    """
    matrix = np.asarray(covariance, dtype=np.float64)
    if matrix.shape != (dimension, dimension) or not np.isfinite(matrix).all():
        raise ValueError(f"{label} must be a finite {dimension}x{dimension} covariance")
    scale = float(np.max(np.abs(matrix)))
    normalized = matrix / scale if scale else matrix
    tolerance = 32 * dimension * np.finfo(np.float64).eps
    if not np.allclose(normalized, normalized.T, rtol=0, atol=tolerance):
        raise ValueError(f"{label} must be symmetric; use rad² for angles and m² for origins")
    eigenvalues, eigenvectors = np.linalg.eigh((normalized + normalized.T) / 2)
    if float(eigenvalues.min()) < -tolerance:
        raise ValueError(f"{label} must be positive semidefinite (negative variance found)")
    factor: NDArray[np.float64] = np.asarray(
        eigenvectors * np.sqrt(np.maximum(eigenvalues, 0)) * np.sqrt(scale), dtype=np.float64
    )
    factor.setflags(write=False)
    return factor


def gaussian_sample(
    covariance: tuple[tuple[float, ...], ...], *, seed: int, stream: str, label: str
) -> tuple[float, ...]:
    """PCG64 stream depends only on the explicit seed and semantic sample key."""
    factor = covariance_factor(covariance, len(covariance), label)
    if not np.any(factor):
        return (0.0,) * len(covariance)
    digest = hashlib.sha256(f"{seed}:{stream}".encode()).digest()
    generator = np.random.Generator(np.random.PCG64(int.from_bytes(digest, "big")))
    return tuple(float(value) for value in factor @ generator.standard_normal(len(covariance)))
