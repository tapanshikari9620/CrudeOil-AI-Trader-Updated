def bullish_conditions(row: dict) -> tuple[bool, list[str]]:
    checks = {
        "price_above_vwap": row["close"] > row["vwap"],
        "price_above_ema20": row["close"] > row["ema20"],
        "ema9_above_ema20": row["ema9"] > row["ema20"],
        "rsi_above_55": row["rsi14"] > 55,
        "positive_momentum": row["momentum"] > 0,
        "volume_confirmation": row["volume"] > row["volume_sma20"],
    }
    return all(checks.values()), [k for k, v in checks.items() if not v]
