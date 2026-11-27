
from datetime import datetime, timezone


def validate_signal(
    signal,
    *,
    market_timestamp: datetime,
    max_age_seconds: int,
    contract_is_active: bool,
    liquidity_ok: bool,
) -> tuple[bool, tuple[str, ...]]:

    reasons = []

    if not isinstance(market_timestamp, datetime):
        reasons.append("invalid_market_timestamp")
    elif market_timestamp.tzinfo is None or market_timestamp.utcoffset() is None:
        reasons.append("market_timestamp_timezone_missing")
    else:
        now = datetime.now(timezone.utc)
        timestamp = market_timestamp.astimezone(timezone.utc)
        age_seconds = (now - timestamp).total_seconds()

        if age_seconds < 0:
            reasons.append("future_market_data")
        elif age_seconds > max_age_seconds:
            reasons.append("stale_market_data")

    if not contract_is_active:
        reasons.append("inactive_contract")

    if not liquidity_ok:
        reasons.append("insufficient_liquidity")

    if not getattr(signal, "valid", False):
        reasons.append("strategy_signal_invalid")

    return not reasons, tuple(reasons)
