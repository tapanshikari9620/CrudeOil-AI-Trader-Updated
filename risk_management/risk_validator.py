"""Non-negotiable execution checks; no fixed rupee profit/loss vetoes."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]


def validate_trade(
    *,
    quantity: int,
    entry: float,
    stop: float,
    lot_size: int,
    trades_today: int,
    open_positions: int,
    data_timestamp: datetime,
    max_data_age_seconds: int,
    max_trades: int = 5,
    max_positions: int = 1,
    now: datetime | None = None,
) -> RiskDecision:
    """Validate structure, exposure count and data freshness only.

    Profit targets and planned rupee loss are strategy outputs, not fixed gates.
    """
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        reasons.append("invalid_quantity")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        reasons.append("invalid_lot_size")
    elif isinstance(quantity, int) and quantity > 0 and quantity % lot_size:
        reasons.append("invalid_quantity_or_lot_multiple")
    try:
        entry_value = float(entry)
        stop_value = float(stop)
    except (TypeError, ValueError, OverflowError):
        reasons.append("invalid_option_prices")
    else:
        if not math.isfinite(entry_value) or not math.isfinite(stop_value):
            reasons.append("non_finite_option_prices")
        elif entry_value <= 0 or stop_value <= 0 or stop_value >= entry_value:
            reasons.append("invalid_option_prices")
    if trades_today >= max_trades:
        reasons.append("max_trades_reached")
    if open_positions >= max_positions:
        reasons.append("max_positions_reached")
    if data_timestamp.tzinfo is None or data_timestamp.utcoffset() is None or now.tzinfo is None or now.utcoffset() is None:
        reasons.append("timezone_required")
    elif (now - data_timestamp).total_seconds() < 0:
        reasons.append("future_market_data")
    elif (now - data_timestamp).total_seconds() > max_data_age_seconds:
        reasons.append("stale_market_data")
    return RiskDecision(not reasons, tuple(reasons))
