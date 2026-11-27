def classify_regime(row: dict, max_atr_pct: float = 2.5) -> str:
    close = float(row["close"])
    atr = float(row["atr14"])
    if close <= 0: return "UNKNOWN"
    if atr / close * 100 > max_atr_pct: return "HIGH_VOLATILITY"
    if abs(float(row["ema9"]) - float(row["ema20"])) / close * 100 < 0.03: return "SIDEWAYS"
    return "TRENDING"
