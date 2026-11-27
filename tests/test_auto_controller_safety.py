
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import paper_trading.auto_controller as controller_module
from paper_trading.auto_controller import PaperTradingController


STAMP = datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self, trades=None, verified=None, quote=None, quotes=None):
        self.trades = trades or []
        self.verified = verified or []
        self.quotes = dict(quotes or {})

        if quote is not None:
            self.quotes[quote.instrument_key] = quote

    def get_paper_trades(self):
        return self.trades

    def get_verified_instruments(self):
        return self.verified

    def get_latest_market_quote(self, instrument_key):
        return self.quotes.get(instrument_key)


class FakeExecution:
    def __init__(self):
        self.processed_quotes = []

    def process_quote(self, quote, **kwargs):
        self.processed_quotes.append((quote, kwargs))
        return []


class FakeCollector:
    def __init__(self):
        self.options = []
        self.quote_requests = []

    def _collect_quotes(self, collector, instruments, observed_at):
        self.quote_requests.append((collector, instruments, observed_at))
        return {
            "valid": len(instruments),
            "persisted": len(instruments),
        }


def make_open_trade(product):
    return SimpleNamespace(
        id=1,
        instrument_key=f"MCX_FO|{product}26JAN7000CE",
        status="OPEN",
        option_type="CE",
        opened_at=STAMP,
    )


def make_verified_option(product):
    return SimpleNamespace(
        instrument_key=f"MCX_FO|{product}26JAN7000CE",
        trading_symbol=f"{product}26JAN7000CE",
        exchange="MCX",
        segment="MCX_FO",
        instrument_type="CE",
        expiry="2026-01-19",
        strike_price=7000.0,
        lot_size=10 if product == "CRUDEOILM" else 100,
        tick_size=0.1,
        underlying_key=f"MCX_FO|{product}26JANFUT",
        underlying_symbol=product,
        contract_status="ACTIVE",
    )


def make_quote_snapshot(
    instrument_key,
    timestamp=STAMP,
    quality_status="VALID_FRESH",
):
    return SimpleNamespace(
        instrument_key=instrument_key,
        response_timestamp=timestamp,
        quality_status=quality_status,
        bid_price=10.0,
        ask_price=10.2,
        last_price=10.1,
    )


@pytest.mark.parametrize(
    "open_product,selected_product",
    [
        ("CRUDEOIL", "CRUDEOILM"),
        ("CRUDEOILM", "CRUDEOIL"),
    ],
)
def test_open_position_is_monitored_after_product_switch(
    open_product, selected_product
):
    trade = make_open_trade(open_product)
    verified = make_verified_option(open_product)
    quote = make_quote_snapshot(trade.instrument_key)

    repository = FakeRepository(
        trades=[trade],
        verified=[verified],
        quote=quote,
    )
    execution = FakeExecution()
    collector = FakeCollector()

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    controller._monitor_open_positions(STAMP)

    assert len(collector.quote_requests) == 1

    collector_name, requested, observed_at = collector.quote_requests[0]

    assert collector_name == "OPEN_POSITIONS"
    assert requested[0]["instrument_key"] == trade.instrument_key
    assert observed_at == STAMP

    assert len(execution.processed_quotes) == 1

    processed_quote, kwargs = execution.processed_quotes[0]

    assert processed_quote["instrument_key"] == trade.instrument_key
    assert processed_quote["option_type"] == "CE"
    assert processed_quote["bid_price"] == 10.0
    assert processed_quote["ask_price"] == 10.2
    assert processed_quote["tick_size"] == 0.1
    assert kwargs["now"] == STAMP


def test_open_position_without_verified_metadata_is_not_processed():
    trade = make_open_trade("CRUDEOIL")

    repository = FakeRepository(trades=[trade])
    execution = FakeExecution()
    collector = FakeCollector()

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    controller._monitor_open_positions(STAMP)

    assert collector.quote_requests == []
    assert execution.processed_quotes == []


def test_open_position_blocks_new_entry(monkeypatch):
    trade = make_open_trade("CRUDEOIL")

    repository = FakeRepository(trades=[trade])

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.config = SimpleNamespace()
    controller._last_signal_candle = None

    candles = pd.DataFrame({"close": [70.0] * 60})

    featured = pd.DataFrame(
        [{
            "timestamp": STAMP,
            "close": 70.0,
            "atr14": 1.0,
            "momentum": 0.5,
        }]
    )

    monkeypatch.setattr(
        controller,
        "_fetch_closed_futures_candles",
        lambda now: candles,
    )
    monkeypatch.setattr(
        controller_module,
        "build_features",
        lambda frame: featured,
    )
    monkeypatch.setattr(
        controller_module,
        "generate_signal",
        lambda row: SimpleNamespace(direction="BULLISH", valid=True),
    )
    monkeypatch.setattr(
        controller_module,
        "evaluate_entry_timing",
        lambda signal, frame: SimpleNamespace(
            action="ENTER",
            option_type="CE",
            setup_type="TEST",
            score=90,
            extension_atr=0.0,
            reasons=[],
        ),
    )

    def unexpected_option_selection(*args, **kwargs):
        pytest.fail(
            "Controller attempted a new entry despite an open position"
        )

    monkeypatch.setattr(
        controller,
        "_select_option",
        unexpected_option_selection,
    )

    controller._evaluate_new_signal(STAMP)


def test_missing_quote_generates_monitoring_warning(caplog):
    trade = make_open_trade("CRUDEOIL")

    repository = FakeRepository(trades=[trade])
    execution = FakeExecution()
    collector = FakeCollector()

    collector.options = [{
        "instrument_key": trade.instrument_key,
        "instrument_type": "CE",
        "tick_size": 0.1,
    }]

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    with caplog.at_level(
        "WARNING",
        logger="crude_oil_trader.paper_controller",
    ):
        controller._monitor_open_positions(STAMP)

    assert "no market quote available" in caplog.text.lower()
    assert execution.processed_quotes == []
@pytest.mark.parametrize(
    "quality_status",
    ["VALID_STALE", "INVALID", None],
)
def test_invalid_quote_quality_is_not_processed(quality_status, caplog):
    trade = make_open_trade("CRUDEOIL")

    snapshot = make_quote_snapshot(
        trade.instrument_key,
        quality_status=quality_status,
    )

    repository = FakeRepository(
        trades=[trade],
        quote=snapshot,
    )
    execution = FakeExecution()
    collector = FakeCollector()

    collector.options = [{
        "instrument_key": trade.instrument_key,
        "instrument_type": "CE",
        "tick_size": 0.1,
    }]

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    with caplog.at_level(
        "WARNING",
        logger="crude_oil_trader.paper_controller",
    ):
        controller._monitor_open_positions(STAMP)

    assert execution.processed_quotes == []
    assert "quote quality rejected" in caplog.text.lower()

def test_stale_quote_generates_monitoring_warning(caplog):
    trade = make_open_trade("CRUDEOIL")

    stale_quote = make_quote_snapshot(
        trade.instrument_key,
        timestamp=STAMP - timedelta(seconds=31),
    )

    class StaleExecution(FakeExecution):
        def process_quote(self, quote, **kwargs):
            raise ValueError("Stale market data")

    repository = FakeRepository(
        trades=[trade],
        quote=stale_quote,
    )
    execution = StaleExecution()
    collector = FakeCollector()

    collector.options = [{
        "instrument_key": trade.instrument_key,
        "instrument_type": "CE",
        "tick_size": 0.1,
    }]

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    with caplog.at_level(
        "WARNING",
        logger="crude_oil_trader.paper_controller",
    ):
        controller._monitor_open_positions(STAMP)

    assert "quote rejected" in caplog.text.lower()
    assert "stale market data" in caplog.text.lower()


def test_one_position_value_error_does_not_block_another():
    first_trade = make_open_trade("CRUDEOIL")
    second_trade = make_open_trade("CRUDEOILM")
    second_trade.id = 2

    quotes = {
        first_trade.instrument_key: make_quote_snapshot(
            first_trade.instrument_key
        ),
        second_trade.instrument_key: make_quote_snapshot(
            second_trade.instrument_key
        ),
    }

    class SelectiveExecution(FakeExecution):
        def process_quote(self, quote, **kwargs):
            self.processed_quotes.append((quote, kwargs))

            if quote["instrument_key"] == first_trade.instrument_key:
                raise ValueError("Invalid quote")

            return []

    repository = FakeRepository(
        trades=[first_trade, second_trade],
        quotes=quotes,
    )
    execution = SelectiveExecution()
    collector = FakeCollector()

    collector.options = [
        {
            "instrument_key": first_trade.instrument_key,
            "instrument_type": "CE",
            "tick_size": 0.1,
        },
        {
            "instrument_key": second_trade.instrument_key,
            "instrument_type": "CE",
            "tick_size": 0.1,
        },
    ]

    controller = PaperTradingController.__new__(PaperTradingController)
    controller.repository = repository
    controller.execution = execution
    controller.collector = collector

    controller._monitor_open_positions(STAMP)

    assert len(execution.processed_quotes) == 2
    assert (
        execution.processed_quotes[1][0]["instrument_key"]
        == second_trade.instrument_key
    )
def test_position_monitoring_runs_when_options_collection_fails():
    controller = PaperTradingController.__new__(PaperTradingController)

    controller._last_options_poll = 0.0
    controller.config = SimpleNamespace(
        live_options_poll_seconds=15,
    )

    class FailingCollector:
        def collect_options_cycle(self, now):
            raise RuntimeError("Simulated Upstox API failure")

    controller.collector = FailingCollector()

    monitored = []

    def fake_monitor(now):
        monitored.append(now)

    controller._monitor_open_positions = fake_monitor

    controller._poll_options_and_monitor(
        STAMP,
        monotonic_now=20.0,
    )

    assert monitored == [STAMP]
    assert controller._last_options_poll == 20.0

    def test_trade_day_uses_ist_timezone():
        from paper_trading.auto_controller import PaperTradingController

        trade = SimpleNamespace(
            opened_at=datetime(2026, 1, 2, 18, 45, tzinfo=timezone.utc)
        )

        assert PaperTradingController._trade_day(trade).isoformat() == "2026-01-03"