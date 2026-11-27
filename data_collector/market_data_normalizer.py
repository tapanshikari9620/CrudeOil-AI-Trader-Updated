import math
from datetime import datetime

from utils.date_utils import to_utc
from utils.validators import validate_ohlcv

SUPPORTED_INSTRUMENT_TYPES = {"FUT", "CE", "PE"}


def validate_instrument_metadata(instrument_key: str, instrument_metadata: dict) -> str:
    """Validate key, MCX segment, and supported type from the official master row."""
    if not isinstance(instrument_metadata, dict):
        raise ValueError("Verified instrument metadata is required")
    required_metadata = ("instrument_key", "segment", "instrument_type")
    missing = [key for key in required_metadata if instrument_metadata.get(key) in (None, "")]
    if missing:
        raise ValueError(f"Instrument metadata missing required fields: {missing}")
    if instrument_metadata["instrument_key"] != instrument_key:
        raise ValueError("Instrument metadata key does not match the requested instrument_key")
    if instrument_metadata["segment"] != "MCX_FO":
        raise ValueError(f"Unsupported instrument segment: {instrument_metadata['segment']!r}")
    instrument_type = str(instrument_metadata["instrument_type"]).upper()
    if instrument_type not in SUPPORTED_INSTRUMENT_TYPES:
        raise ValueError(f"Unsupported instrument_type in instrument metadata: {instrument_type!r}")
    return instrument_type


def normalize_candle(
        instrument_key: str,
        interval: str,
        raw: list,
        instrument_metadata: dict,
        source: str = "upstox",
) -> dict:
    """Normalize a candle using a matching, verified Upstox instrument-master row."""
    instrument_type = validate_instrument_metadata(instrument_key, instrument_metadata)
    if not isinstance(raw, (list, tuple)) or len(raw) < 6:
        raise ValueError("Candle payload must contain timestamp, OHLC and volume")

    ts = datetime.fromisoformat(str(raw[0]).replace("Z", "+00:00"))
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("Candle timestamp must include a timezone")
    try:
        values = [float(raw[index]) for index in range(1, 6)]
        open_interest = float(raw[6]) if len(raw) > 6 and raw[6] is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Candle OHLCV and open interest must be numeric") from exc
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Candle OHLCV values must be finite")
    if open_interest is not None and (not math.isfinite(open_interest) or open_interest < 0):
        raise ValueError("Candle open_interest must be finite and non-negative")
    item = {
        "instrument_key": instrument_key,
        "exchange": "MCX",
        "instrument_type": instrument_type,
        "interval": interval,
        "timestamp": to_utc(ts),
        "open": values[0],
        "high": values[1],
        "low": values[2],
        "close": values[3],
        "volume": values[4],
        "open_interest": open_interest,
        "source": source,
    }
    validate_ohlcv(item)
    return item
