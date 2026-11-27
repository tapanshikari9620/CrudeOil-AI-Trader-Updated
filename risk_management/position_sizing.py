"""Lot-aware paper position sizing with a hard configurable exposure ceiling."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    lots: int
    estimated_loss: float
    allowed: bool


def calculate_position_size(
    entry: float,
    stop: float,
    lot_size: int,
    max_loss: float | None = None,
    charges_per_lot: float = 0.0,
    slippage_per_unit: float = 0.0,
    *,
    max_lots: int | None = None,
) -> SizingResult:
    """Size by lot count.

    New paper controller usage supplies max_lots (default one lot). Estimated loss
    is reported, not used as a fixed rupee veto. max_loss remains only for backward
    compatibility with older tests/tools and is not used by the new controller.
    """
    numbers = (entry, stop, charges_per_lot, slippage_per_unit)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers):
        raise ValueError("Prices, charges and slippage must be finite numbers")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        raise ValueError("lot_size must be a positive integer")

    if entry <= 0 or stop <= 0:
        raise ValueError("Prices must be positive")
    if stop >= entry:
        raise ValueError("For long option buying, stop must be below entry")
    if charges_per_lot < 0 or slippage_per_unit < 0:
        raise ValueError("Charges and slippage must be non-negative")

    risk_per_unit = entry - stop + slippage_per_unit
    risk_per_lot = risk_per_unit * lot_size + charges_per_lot

    if max_lots is not None:
        if isinstance(max_lots, bool) or not isinstance(max_lots, int) or max_lots < 1:
            raise ValueError("max_lots must be a positive integer")

        if max_lots > 1:
            raise ValueError("Position sizing is restricted to one lot maximum")

        lots = 1

    else:
        if max_loss is None or not math.isfinite(float(max_loss)) or max_loss <= 0:
            raise ValueError("Supply positive max_lots or legacy max_loss")

        # Legacy compatibility: calculate affordability, but never exceed one lot.
        affordable_lots = int(max_loss // risk_per_lot)
        lots = min(affordable_lots, 1)

    return SizingResult(lots * lot_size, lots, lots * risk_per_lot, lots > 0)
