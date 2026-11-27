from datetime import datetime, timezone
import pytest
from utils.validators import validate_ohlcv, validate_option_type


def valid_candle():
    return {
        "timestamp": datetime.now(timezone.utc),
        "open": 70.0, "high": 72.0, "low": 69.0,
        "close": 71.0, "volume": 100.0,
    }


def test_valid_ohlcv_passes():
    validate_ohlcv(valid_candle())


def test_invalid_high_fails():
    row = valid_candle()
    row["high"] = 68.0
    with pytest.raises(ValueError):
        validate_ohlcv(row)


def test_option_type_validation():
    validate_option_type("CE")
    validate_option_type("PE")
    with pytest.raises(ValueError):
        validate_option_type("CALL")
