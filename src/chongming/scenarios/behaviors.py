"""Closed, versioned first-party behavior registry; no configuration imports."""

import math
from dataclasses import replace

from chongming.contracts.scene import FlightBehaviorSpec, KinematicState

REGISTRY = {
    ("constant-velocity", "1"): frozenset({"vx", "vy", "vz"}),
    ("orbit", "1"): frozenset(
        {
            "center_e",
            "center_n",
            "center_u",
            "radius_e",
            "radius_n",
            "period_s",
            "phase_rad",
            "vertical_amplitude_m",
        }
    ),
}


def validate_behavior(spec: FlightBehaviorSpec) -> None:
    allowed = REGISTRY.get((spec.behavior_id, spec.implementation_version))
    if allowed is None:
        raise ValueError(
            f"unknown behavior/version: {spec.behavior_id}/{spec.implementation_version}"
        )
    values = dict(spec.parameters)
    if len(values) != len(spec.parameters) or set(values) != allowed:
        raise ValueError(
            f"behavior {spec.behavior_id}/{spec.implementation_version} has invalid parameters"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in values.values()
    ):
        raise ValueError("behavior velocity parameters must be finite numbers")
    if not spec.random_stream:
        raise ValueError("behavior requires a named deterministic stream")


def sample_behavior(
    spec: FlightBehaviorSpec,
    initial: KinematicState,
    elapsed_ns: int,
) -> KinematicState:
    """Version 1 is analytic and consumes no random numbers or wall time.

    The first trajectory knot supplies the initial pose/time; subsequent knots
    are ignored when a behavior is selected. Future stochastic entries must use
    the scenario seed and the declared stream, never global RNG state.
    """
    validate_behavior(spec)
    values = dict(spec.parameters)
    dt = elapsed_ns / 1e9
    if spec.behavior_id == "constant-velocity":
        velocity = (float(values["vx"]), float(values["vy"]), float(values["vz"]))
        position = tuple(initial.position_enu_m[i] + velocity[i] * dt for i in range(3))
    elif spec.behavior_id == "orbit":
        period = float(values["period_s"])
        if period <= 0:
            raise ValueError("orbit period must be positive")
        omega = 2 * math.pi / period
        phase = float(values["phase_rad"]) + omega * dt
        re, rn = float(values["radius_e"]), float(values["radius_n"])
        amplitude = float(values["vertical_amplitude_m"])
        velocity = (
            re * omega * math.cos(phase),
            -rn * omega * math.sin(phase),
            2 * amplitude * omega * math.cos(2 * phase),
        )
        position = (
            float(values["center_e"]) + re * math.sin(phase),
            float(values["center_n"]) + rn * math.cos(phase),
            float(values["center_u"]) + amplitude * math.sin(2 * phase),
        )
    else:
        raise ValueError("registered behavior implementation is missing")
    return replace(
        initial, position_enu_m=(position[0], position[1], position[2]), velocity_enu_mps=velocity
    )
