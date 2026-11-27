from datetime import datetime


def validate_ohlcv(row: dict) -> None:
    required = {"open", "high", "low", "close", "volume", "timestamp"}
    missing = required - row.keys()
    if missing:
        raise ValueError(f"Missing candle fields: {sorted(missing)}")

    for key in ("open", "high", "low", "close", "volume"):
        value = row[key]
        if value is None or float(value) < 0:
            raise ValueError(f"{key} must be a non-negative number")

    if float(row["high"]) < max(float(row["open"]), float(row["close"]), float(row["low"])):
        raise ValueError("high must be >= open, close and low")
    if float(row["low"]) > min(float(row["open"]), float(row["close"]), float(row["high"])):
        raise ValueError("low must be <= open, close and high")

    if not isinstance(row["timestamp"], datetime):
        raise ValueError("timestamp must be a datetime object")


def validate_option_type(option_type: str) -> None:
    if option_type not in {"CE", "PE"}:
        raise ValueError("option_type must be CE or PE")
