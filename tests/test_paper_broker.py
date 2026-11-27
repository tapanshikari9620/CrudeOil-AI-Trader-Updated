
from datetime import datetime, timezone

import pytest

from paper_trading.paper_broker import PaperBroker


NOW = datetime.now(timezone.utc)


def test_buy_fill_uses_ask_plus_slippage():
    broker = PaperBroker(slippage_ticks=1)

    fill = broker.market_fill(
        "MCX:TEST",
        "BUY",
        100,
        100.0,
        100.5,
        0.5,
        NOW,
    )

    assert fill.price == 101.0
    assert fill.simulated is True


def test_sell_fill_uses_bid_minus_slippage():
    broker = PaperBroker(slippage_ticks=1)

    fill = broker.market_fill(
        "MCX:TEST",
        "SELL",
        100,
        100.0,
        100.5,
        0.5,
        NOW,
    )

    assert fill.price == 99.5
    assert fill.simulated is True


@pytest.mark.parametrize("value", [True, False, "100", None, float("nan"), float("inf"), 0, -1])
def test_market_fill_rejects_invalid_bid(value):
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill("MCX:TEST", "BUY", 100, value, 101.0, 0.5, NOW)


@pytest.mark.parametrize("value", [True, False, "101", None, float("nan"), float("inf"), 0, -1])
def test_market_fill_rejects_invalid_ask(value):
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill("MCX:TEST", "BUY", 100, 100.0, value, 0.5, NOW)


@pytest.mark.parametrize("value", [True, False, "0.5", None, float("nan"), float("inf"), 0, -0.5])
def test_market_fill_rejects_invalid_tick_size(value):
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill("MCX:TEST", "BUY", 100, 100.0, 101.0, value, NOW)


@pytest.mark.parametrize("quantity", [True, False, 0, -1, 1.5, "100", None])
def test_market_fill_rejects_invalid_quantity(quantity):
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill("MCX:TEST", "BUY", quantity, 100.0, 101.0, 0.5, NOW)


@pytest.mark.parametrize("slippage", [True, False, -1, 1.5, "1"])
def test_broker_rejects_invalid_slippage_ticks(slippage):
    with pytest.raises(ValueError):
        PaperBroker(slippage_ticks=slippage)


def test_market_fill_rejects_inverted_market():
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill("MCX:TEST", "BUY", 100, 101.0, 100.0, 0.5, NOW)


def test_market_fill_rejects_naive_timestamp():
    broker = PaperBroker()

    with pytest.raises(ValueError):
        broker.market_fill(
            "MCX:TEST",
            "BUY",
            100,
            100.0,
            101.0,
            0.5,
            datetime.now(),
        )
