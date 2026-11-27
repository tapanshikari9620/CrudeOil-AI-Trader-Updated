def exit_reason(entry: float, stop: float, target: float, candle: dict) -> tuple[float | None, str | None]:
    # Conservative same-bar rule: if both barriers are touched, assume stop-loss first.
    if candle["low"] <= stop: return stop, "STOP_LOSS"
    if candle["high"] >= target: return target, "TARGET"
    return None, None
