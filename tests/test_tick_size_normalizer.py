
import pytest

from data_collector.tick_size_normalizer import normalize_upstox_tick_size


@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_regular_crude_option_tick_is_normalized(option_type):
    result = normalize_upstox_tick_size(
        10.0,
        instrument_type=option_type,
        trading_symbol=f"CRUDEOIL 7500 {option_type}",
        underlying_symbol="CRUDEOIL",
        lot_size=100,
    )

    assert result == pytest.approx(0.10)


def test_regular_crude_future_tick_is_normalized():
    result = normalize_upstox_tick_size(
        100.0,
        instrument_type="FUT",
        trading_symbol="CRUDEOIL FUT 18 DEC 26",
        underlying_symbol="CRUDEOIL",
        lot_size=100,
    )

    assert result == pytest.approx(1.00)


@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_mini_crude_option_tick_is_normalized(option_type):
    result = normalize_upstox_tick_size(
        5.0,
        instrument_type=option_type,
        trading_symbol=f"CRUDEOILM 7850 {option_type} 16 DEC 26",
        underlying_symbol="CRUDEOILM",
        lot_size=10,
    )

    assert result == pytest.approx(0.05)


def test_mini_crude_future_tick_is_normalized():
    result = normalize_upstox_tick_size(
        100.0,
        instrument_type="FUT",
        trading_symbol="CRUDEOILM FUT 18 DEC 26",
        underlying_symbol="CRUDEOILM",
        lot_size=10,
    )

    assert result == pytest.approx(1.00)


def test_other_tick_values_remain_unchanged():
    result = normalize_upstox_tick_size(
        1.0,
        instrument_type="CE",
        trading_symbol="CRUDEOIL 7500 CE",
        underlying_symbol="CRUDEOIL",
        lot_size=100,
    )

    assert result == pytest.approx(1.0)


def test_unrelated_future_tick_remains_unchanged():
    result = normalize_upstox_tick_size(
        100.0,
        instrument_type="FUT",
        trading_symbol="GOLD FUT",
        underlying_symbol="GOLD",
        lot_size=100,
    )

    assert result == pytest.approx(100.0)


@pytest.mark.parametrize("invalid_tick", [0, -1, float("nan"), float("inf")])
def test_invalid_tick_size_is_rejected(invalid_tick):
    with pytest.raises(ValueError):
        normalize_upstox_tick_size(
            invalid_tick,
            instrument_type="CE",
            trading_symbol="CRUDEOIL 7500 CE",
            underlying_symbol="CRUDEOIL",
            lot_size=100,
        )
