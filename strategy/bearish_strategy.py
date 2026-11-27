def bearish_conditions(row: dict) -> tuple[bool, list[str]]:
    checks = {
        "price_below_vwap": row["close"] < row["vwap"],
        "price_below_ema20": row["close"] < row["ema20"],
        "ema9_below_ema20": row["ema9"] < row["ema20"],
        "rsi_below_45": row["rsi14"] < 45,
        "negative_momentum": row["momentum"] < 0,
        "volume_confirmation": row["volume"] > row["volume_sma20"],
    }
    return all(checks.values()), [k for k, v in checks.items() if not v]
