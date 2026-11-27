
"""Source-aware tick-size normalization for verified MCX crude contracts.

Verified conversions:
- Standard CRUDEOIL FUT, lot 100: raw 100.0 -> ₹1.00
- Mini CRUDEOILM FUT, lot 10: raw 100.0 -> ₹1.00
- Standard CRUDEOIL CE/PE, lot 100: raw 10.0 -> ₹0.10
- Mini CRUDEOILM CE/PE, lot 10: raw 5.0 -> ₹0.05

Original source metadata must be preserved separately.
"""

from __future__ import annotations

import math


def normalize_upstox_tick_size(
    raw_tick_size: float,
    *,
    instrument_type: str,
    trading_symbol: str,
    underlying_symbol: str,
    lot_size: int,
) -> float:
    """Normalize verified MCX Crude Oil futures and options tick sizes."""

    try:
        tick = float(raw_tick_size)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("tick_size must be numeric") from exc

    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("tick_size must be finite and positive")

    if (
        isinstance(lot_size, bool)
        or not isinstance(lot_size, int)
        or lot_size <= 0
    ):
        raise ValueError("lot_size must be a positive integer")

    kind = str(instrument_type).upper().strip()
    symbol = str(trading_symbol).upper().replace(" ", "")
    underlying = str(underlying_symbol).upper().strip()

    is_standard = (
        underlying == "CRUDEOIL"
        and symbol.startswith("CRUDEOIL")
        and lot_size == 100
    )

    is_mini = (
        underlying == "CRUDEOILM"
        and symbol.startswith("CRUDEOILM")
        and lot_size == 10
    )

    # Standard and Mini Crude Oil futures.
    if kind == "FUT" and (is_standard or is_mini):
        if math.isclose(tick, 100.0, rel_tol=0.0, abs_tol=1e-9):
            return 1.00

    # Standard Crude Oil options.
    if kind in {"CE", "PE"} and is_standard:
        if math.isclose(tick, 10.0, rel_tol=0.0, abs_tol=1e-9):
            return 0.10

    # Mini Crude Oil options.
    if kind in {"CE", "PE"} and is_mini:
        if math.isclose(tick, 5.0, rel_tol=0.0, abs_tol=1e-9):
            return 0.05

    # Do not modify unrelated or unverified instruments.
    return tick
