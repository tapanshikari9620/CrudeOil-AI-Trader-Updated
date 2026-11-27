import pytest
from config.settings import Settings


def test_default_mode_is_paper():
    assert Settings().trading_mode == "PAPER"


def test_live_mode_is_blocked_in_phase_one():
    with pytest.raises(ValueError, match="LIVE mode is disabled"):
        Settings(trading_mode="LIVE").validate()


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="TRADING_MODE"):
        Settings(trading_mode="UNKNOWN").validate()


def test_default_max_trades_per_day_is_five():
    assert Settings().max_trades_per_day == 5


def test_max_trades_per_day_cannot_exceed_five():
    with pytest.raises(ValueError, match="between 1 and 5"):
        Settings(max_trades_per_day=6).validate()


def test_default_mcx_product_is_mini():
    assert Settings().mcx_product == "CRUDEOILM"


def test_regular_crude_product_is_supported():
    assert Settings(mcx_product="CRUDEOIL").validate().mcx_product == "CRUDEOIL"


def test_mini_crude_product_is_supported():
    assert Settings(mcx_product="CRUDEOILM").validate().mcx_product == "CRUDEOILM"


def test_invalid_mcx_product_is_rejected():
    with pytest.raises(ValueError, match="MCX_PRODUCT"):
        Settings(mcx_product="UNKNOWN").validate()

def test_default_max_paper_lots_per_trade_is_one():
    assert Settings().max_paper_lots_per_trade == 1


@pytest.mark.parametrize("lots", [0, 2, 5])
def test_max_paper_lots_per_trade_must_be_exactly_one(lots):
    with pytest.raises(ValueError, match="exactly 1"):
        Settings(max_paper_lots_per_trade=lots).validate()


@pytest.mark.parametrize(
    "multiplier",
    [0, -1, float("nan"), float("inf")],
)
def test_invalid_atr_stop_multiplier_is_rejected(multiplier):
    with pytest.raises(ValueError, match="ATR_STOP_MULTIPLIER"):
        Settings(atr_stop_multiplier=multiplier).validate()


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (0, 0.35),
        (0.08, 0.08),
        (0.35, 0.08),
        (0.08, 1.0),
        (0.08, float("nan")),
    ],
)
def test_invalid_stop_risk_fractions_are_rejected(minimum, maximum):
    with pytest.raises(ValueError, match="Stop risk fractions"):
        Settings(
            min_stop_risk_fraction=minimum,
            max_stop_risk_fraction=maximum,
        ).validate()


def test_zero_max_trades_per_day_is_rejected():
    with pytest.raises(ValueError, match="between 1 and 5"):
        Settings(max_trades_per_day=0).validate()
