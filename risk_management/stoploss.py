"""Volatility-aware initial and one-way trailing stops for long option paper trades."""
from __future__ import annotations

import math


def _validate_finite_numbers(**values: float) -> None:
    for name, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be a finite number")


def stop_from_premium(
    entry: float,
    risk_fraction: float | None = None,
    *,
    underlying_atr_pct: float | None = None,
    atr_multiplier: float = 1.5,
    min_risk_fraction: float = 0.08,
    max_risk_fraction: float = 0.35,
) -> float:
    """Return an initial premium stop using volatility or an explicit risk fraction."""

    _validate_finite_numbers(
        entry=entry,
        atr_multiplier=atr_multiplier,
        min_risk_fraction=min_risk_fraction,
        max_risk_fraction=max_risk_fraction,
    )

    if entry <= 0 or atr_multiplier <= 0:
        raise ValueError("entry and atr_multiplier must be positive")

    if not 0 < min_risk_fraction < max_risk_fraction < 1:
        raise ValueError("Require 0 < min_risk_fraction < max_risk_fraction < 1")

    if risk_fraction is not None:
        _validate_finite_numbers(risk_fraction=risk_fraction)

        if not 0 < risk_fraction < 1:
            raise ValueError("Invalid risk_fraction")

        fraction = float(risk_fraction)
    else:
        _validate_finite_numbers(underlying_atr_pct=underlying_atr_pct)

        if underlying_atr_pct <= 0:
            raise ValueError("A positive underlying_atr_pct is required")

        fraction = min(
            max(float(underlying_atr_pct) * atr_multiplier, min_risk_fraction),
            max_risk_fraction,
        )

    stop = round(entry * (1.0 - fraction), 2)

    if stop <= 0 or stop >= entry:
        raise ValueError("Calculated stop is invalid")

    return stop


def trailing_stop_from_peak(
    *,
    entry: float,
    initial_stop: float,
    current_stop: float,
    peak_bid: float,
    tick_size: float,
) -> float:
    """Calculate a one-way trailing stop that never moves lower for a long option."""

    _validate_finite_numbers(
        entry=entry,
        initial_stop=initial_stop,
        current_stop=current_stop,
        peak_bid=peak_bid,
        tick_size=tick_size,
    )

    if entry <= 0 or tick_size <= 0 or not 0 < initial_stop < entry:
        raise ValueError("Invalid entry, initial stop, or tick size")

    if current_stop <= 0 or peak_bid <= 0:
        raise ValueError("Current stop and peak bid must be positive")

    initial_risk = entry - initial_stop
    candidate = peak_bid - initial_risk

    # Round upward to the instrument tick so rounding cannot loosen the stop.
    candidate = math.ceil((candidate - 1e-12) / tick_size) * tick_size
    candidate = round(candidate, 8)

    return max(round(current_stop, 8), candidate)