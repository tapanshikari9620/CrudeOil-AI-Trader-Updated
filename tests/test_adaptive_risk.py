from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.db_connection import Base
from database.models import PaperTrade, VerifiedInstrument
from paper_trading.paper_execution_engine import PaperExecutionEngine
from risk_management.position_sizing import calculate_position_size
from risk_management.risk_validator import validate_trade
from risk_management.stoploss import stop_from_premium, trailing_stop_from_peak
from risk_management.target_calculator import target_from_risk


STAMP = datetime(2026, 1, 2, 3, 45, tzinfo=timezone.utc)
def register_verified_option(
    session_factory,
    instrument_key,
    option_type,
    lot_size=10,
    expiry="2099-12-31",
    contract_status="ACTIVE",
):
    with session_factory.begin() as session:
        session.add(
            VerifiedInstrument(
                instrument_key=instrument_key,
                trading_symbol=instrument_key.split("|")[-1],
                exchange="MCX",
                segment="MCX_FO",
                instrument_type=option_type,
                expiry=expiry,
                strike_price=7000.0,
                lot_size=lot_size,
                tick_size=0.1,
                contract_status=contract_status,
            )
        )


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_atr_based_stop_and_risk_multiple_target_are_adaptive():
    calm = stop_from_premium(100, underlying_atr_pct=0.04, atr_multiplier=1.5)
    volatile = stop_from_premium(100, underlying_atr_pct=0.12, atr_multiplier=1.5)
    assert volatile < calm < 100
    assert target_from_risk(100, calm, 2.0) > 100


def test_trailing_stop_never_moves_down():
    first = trailing_stop_from_peak(
        entry=100, initial_stop=85, current_stop=85, peak_bid=110, tick_size=0.05
    )
    second = trailing_stop_from_peak(
        entry=100, initial_stop=85, current_stop=first, peak_bid=125, tick_size=0.05
    )
    after_reversal = trailing_stop_from_peak(
        entry=100, initial_stop=85, current_stop=second, peak_bid=115, tick_size=0.05
    )
    assert first >= 85
    assert second >= first
    assert after_reversal == second


def test_position_sizing_uses_lot_ceiling_not_rupee_loss_limit():
    sizing = calculate_position_size(100, 80, 10, max_lots=1)
    assert sizing.allowed
    assert sizing.quantity == 10
    assert sizing.lots == 1
    assert sizing.estimated_loss > 0

def test_position_sizing_rejects_max_lots_above_one():
    with pytest.raises(ValueError, match="one lot maximum"):
        calculate_position_size(100, 80, 10, max_lots=2)


def test_legacy_position_sizing_never_exceeds_one_lot():
    sizing = calculate_position_size(
        entry=100,
        stop=80,
        lot_size=10,
        max_loss=1000,
    )

    assert sizing.allowed
    assert sizing.lots == 1
    assert sizing.quantity == 10


def test_legacy_position_sizing_blocks_when_one_lot_is_unaffordable():
    sizing = calculate_position_size(
        entry=100,
        stop=80,
        lot_size=10,
        max_loss=100,
    )

    assert not sizing.allowed
    assert sizing.lots == 0
    assert sizing.quantity == 0


def test_validator_keeps_structural_checks_and_daily_trade_cap_only():
    result = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=1,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        max_trades=2,
        max_positions=1,
        now=STAMP + timedelta(seconds=1),
    )
    assert result.allowed
    blocked = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=2,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        max_trades=2,
        max_positions=1,
        now=STAMP + timedelta(seconds=1),
    )
    assert not blocked.allowed
    assert "max_trades_reached" in blocked.reasons


def test_paper_engine_ratchets_stop_and_persists_peak(session_factory):
    register_verified_option(session_factory, "MCX_FO|TESTCE", "CE")
    engine = PaperExecutionEngine(session_factory=session_factory)
    trade_id = engine.open_long_option(
        "MCX_FO|TESTCE", 10, 10.0, 10.1, 0.1, 8.0, 15.0, STAMP, option_type="CE"
    )

    first = engine.process_quote({
        "instrument_key": "MCX_FO|TESTCE", "option_type": "CE",
        "timestamp": STAMP + timedelta(seconds=1), "bid_price": 11.0,
        "ask_price": 11.2, "ltp": 11.1, "tick_size": 0.1,
    }, now=STAMP + timedelta(seconds=1))
    first_stop = first[0]["stop_loss"]

    second = engine.process_quote({
        "instrument_key": "MCX_FO|TESTCE", "option_type": "CE",
        "timestamp": STAMP + timedelta(seconds=2), "bid_price": 13.0,
        "ask_price": 13.2, "ltp": 13.1, "tick_size": 0.1,
    }, now=STAMP + timedelta(seconds=2))
    second_stop = second[0]["stop_loss"]

    third = engine.process_quote({
        "instrument_key": "MCX_FO|TESTCE", "option_type": "CE",
        "timestamp": STAMP + timedelta(seconds=3), "bid_price": 12.0,
        "ask_price": 12.2, "ltp": 12.1, "tick_size": 0.1,
    }, now=STAMP + timedelta(seconds=3))
    third_stop = third[0]["stop_loss"]

    assert second_stop >= first_stop
    assert third_stop == second_stop
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "OPEN"
        assert trade.stop_loss == third_stop
        assert trade.peak_mark_price == pytest.approx(13.0)
        assert trade.initial_stop_loss == pytest.approx(8.0)

@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_paper_target_exit_and_net_pnl(session_factory, option_type):
    engine = PaperExecutionEngine(session_factory=session_factory)
    key = f"MCX_FO|TARGET_{option_type}"
    register_verified_option(session_factory, key, option_type)

    trade_id = engine.open_long_option(
        key, 10, 10.0, 10.1, 0.1, 8.0, 15.0, STAMP,
        option_type=option_type,
    )

    result = engine.process_quote({
        "instrument_key": key,
        "option_type": option_type,
        "timestamp": STAMP + timedelta(seconds=1),
        "bid_price": 15.1,
        "ask_price": 15.3,
        "ltp": 15.2,
        "tick_size": 0.1,
    }, now=STAMP + timedelta(seconds=1), charges=2.0)

    assert result[0]["status"] == "CLOSED"
    assert result[0]["exit_reason"] == "TARGET"
    assert result[0]["exit_price"] == pytest.approx(15.0)
    assert result[0]["gross_pnl"] == pytest.approx(48.0)
    assert result[0]["net_pnl"] == pytest.approx(46.0)

    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.option_type == option_type
        assert trade.net_pnl == pytest.approx(46.0)


@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_paper_stop_loss_exit_and_net_pnl(session_factory, option_type):
    engine = PaperExecutionEngine(session_factory=session_factory)
    key = f"MCX_FO|STOP_{option_type}"
    register_verified_option(session_factory, key, option_type)

    trade_id = engine.open_long_option(
        key, 10, 10.0, 10.1, 0.1, 8.0, 15.0, STAMP,
        option_type=option_type,
    )

    result = engine.process_quote({
        "instrument_key": key,
        "option_type": option_type,
        "timestamp": STAMP + timedelta(seconds=1),
        "bid_price": 7.9,
        "ask_price": 8.1,
        "ltp": 8.0,
        "tick_size": 0.1,
    }, now=STAMP + timedelta(seconds=1), charges=2.0)

    assert result[0]["status"] == "CLOSED"
    assert result[0]["exit_reason"] == "STOP_LOSS"
    assert result[0]["exit_price"] == pytest.approx(7.8)
    assert result[0]["gross_pnl"] == pytest.approx(-24.0)
    assert result[0]["net_pnl"] == pytest.approx(-26.0)

    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.option_type == option_type
        assert trade.net_pnl == pytest.approx(-26.0)

def test_execution_rejects_unverified_contract_before_paper_fill(session_factory, monkeypatch):
    engine = PaperExecutionEngine(session_factory=session_factory)
    calls = []

    def unexpected_fill(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Paper fill must not run for an unverified contract")

    monkeypatch.setattr(engine.broker, "market_fill", unexpected_fill)

    with pytest.raises(ValueError, match="Unverified option contract"):
        engine.open_long_option(
            "MCX_FO|UNKNOWN",
            10, 10.0, 10.1, 0.1, 8.0, 15.0,
            STAMP,
            option_type="CE",
        )

    assert calls == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"contract_status": "EXPIRED"}, "not ACTIVE"),
        ({"exchange": "NSE"}, "MCX"),
        ({"segment": "NSE_FO"}, "MCX_FO"),
        ({"expiry": "invalid-date"}, "expiry"),
        ({"expiry": "2020-12-31"}, "expired"),
    ],
)
def test_execution_rejects_invalid_verified_contract(
    session_factory, overrides, message
):
    key = "MCX_FO|INVALID_CONTRACT"
    metadata = {
        "instrument_key": key,
        "trading_symbol": "INVALID_CONTRACT",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": "CE",
        "expiry": "2099-12-31",
        "strike_price": 7000.0,
        "lot_size": 10,
        "tick_size": 0.1,
        "contract_status": "ACTIVE",
    }
    metadata.update(overrides)

    with session_factory.begin() as session:
        session.add(VerifiedInstrument(**metadata))

    engine = PaperExecutionEngine(session_factory=session_factory)
    with pytest.raises(ValueError, match=message):
        engine.open_long_option(
            key, 10, 10.0, 10.1, 0.1, 8.0, 15.0,
            STAMP, option_type="CE",
        )


def test_execution_rejects_mismatched_option_type(session_factory):
    key = "MCX_FO|VERIFIED_PE"
    register_verified_option(session_factory, key, "PE")

    engine = PaperExecutionEngine(session_factory=session_factory)
    with pytest.raises(ValueError, match="option type mismatch"):
        engine.open_long_option(
            key, 10, 10.0, 10.1, 0.1, 8.0, 15.0,
            STAMP, option_type="CE",
        )


@pytest.mark.parametrize("quantity", [0, -10, 5, 20, True, 10.0, "10"])
def test_execution_rejects_quantity_other_than_exactly_one_lot(
    session_factory, quantity
):
    key = f"MCX_FO|BAD_QTY_{str(quantity)}"
    register_verified_option(session_factory, key, "CE", lot_size=10)

    engine = PaperExecutionEngine(session_factory=session_factory)
    with pytest.raises(ValueError, match="exactly one verified lot"):
        engine.open_long_option(
            key, quantity, 10.0, 10.1, 0.1, 8.0, 15.0,
            STAMP, option_type="CE",
        )


def test_execution_allows_exactly_one_verified_lot(session_factory):
    key = "MCX_FO|VALID_ONE_LOT"
    register_verified_option(session_factory, key, "CE", lot_size=10)

    engine = PaperExecutionEngine(session_factory=session_factory)
    trade_id = engine.open_long_option(
        key, 10, 10.0, 10.1, 0.1, 8.0, 15.0,
        STAMP, option_type="CE",
    )

    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade is not None
        assert trade.status == "OPEN"
        assert trade.quantity == 10
        assert trade.notes == "PAPER_ONLY"


@pytest.mark.parametrize(
    ("quantity", "lot_size", "expected_reason"),
    [
        (0, 10, "invalid_quantity"),
        (-10, 10, "invalid_quantity"),
        (15, 10, "invalid_quantity_or_lot_multiple"),
        (10, 0, "invalid_lot_size"),
        (10, -10, "invalid_lot_size"),
        (True, 10, "invalid_quantity"),
    ],
)
def test_validator_rejects_invalid_quantity_and_lot_size(
        quantity, lot_size, expected_reason
):
    result = validate_trade(
        quantity=quantity,
        entry=100,
        stop=80,
        lot_size=lot_size,
        trades_today=0,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        now=STAMP + timedelta(seconds=1),
    )
    assert not result.allowed
    assert expected_reason in result.reasons

@pytest.mark.parametrize(
    ("entry", "stop", "expected_reason"),
    [
        (0, 80, "invalid_option_prices"),
        (100, 0, "invalid_option_prices"),
        (100, 100, "invalid_option_prices"),
        (100, 110, "invalid_option_prices"),
        (float("nan"), 80, "non_finite_option_prices"),
        (100, float("inf"), "non_finite_option_prices"),
    ],
)
def test_validator_rejects_invalid_option_prices(
        entry, stop, expected_reason
):
    result = validate_trade(
        quantity=10,
        entry=entry,
        stop=stop,
        lot_size=10,
        trades_today=0,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        now=STAMP + timedelta(seconds=1),
    )
    assert not result.allowed
    assert expected_reason in result.reasons

def test_validator_rejects_stale_market_data():
    result = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=0,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        now=STAMP + timedelta(seconds=31),
    )
    assert not result.allowed
    assert "stale_market_data" in result.reasons

def test_validator_rejects_future_market_data():
    result = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=0,
        open_positions=0,
        data_timestamp=STAMP + timedelta(seconds=5),
        max_data_age_seconds=30,
        now=STAMP,
    )
    assert not result.allowed
    assert "future_market_data" in result.reasons

def test_validator_rejects_naive_market_timestamp():
    result = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=0,
        open_positions=0,
        data_timestamp=datetime(2026, 1, 2, 3, 45),
        max_data_age_seconds=30,
        now=STAMP,
    )
    assert not result.allowed
    assert "timezone_required" in result.reasons

def test_validator_rejects_maximum_open_positions():
    result = validate_trade(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        trades_today=0,
        open_positions=1,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        max_positions=1,
        now=STAMP + timedelta(seconds=1),
    )
    assert not result.allowed
    assert "max_positions_reached" in result.reasons

@pytest.mark.parametrize(
    ("entry", "stop"),
    [
        (None, 80),
        (100, None),
        ("invalid", 80),
        (100, "invalid"),
    ],
)
def test_validator_rejects_non_numeric_option_prices(entry, stop):
    result = validate_trade(
        quantity=10,
        entry=entry,
        stop=stop,
        lot_size=10,
        trades_today=0,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        now=STAMP + timedelta(seconds=1),
    )

    assert not result.allowed
    assert "invalid_option_prices" in result.reasons
@pytest.mark.parametrize(
    "lot_size",
    [True, False, 10.5, "10", None, 0, -10],
)
def test_position_sizing_rejects_invalid_lot_size(lot_size):
    with pytest.raises(ValueError):
        calculate_position_size(
            entry=100,
            stop=80,
            lot_size=lot_size,
            max_lots=1,
        )


@pytest.mark.parametrize(
    "max_lots",
    [True, False, 0, -1, 1.5, "1", None],
)
def test_position_sizing_rejects_invalid_max_lots(max_lots):
    with pytest.raises(ValueError):
        calculate_position_size(
            entry=100,
            stop=80,
            lot_size=10,
            max_lots=max_lots,
        )

def test_daily_limit_allows_fourth_trade_but_blocks_sixth():
    common = dict(
        quantity=10,
        entry=100,
        stop=80,
        lot_size=10,
        open_positions=0,
        data_timestamp=STAMP,
        max_data_age_seconds=30,
        max_trades=5,
        max_positions=1,
        now=STAMP + timedelta(seconds=1),
    )

    fifth_trade = validate_trade(trades_today=4, **common)
    sixth_trade = validate_trade(trades_today=5, **common)

    assert fifth_trade.allowed
    assert not sixth_trade.allowed
    assert "max_trades_reached" in sixth_trade.reasons

    @pytest.mark.parametrize(
        "risk_fraction",
        [True, False, "0.1", float("nan"), float("inf"), 0, -0.1, 1],
    )
    def test_initial_stop_rejects_invalid_risk_fraction(risk_fraction):
        with pytest.raises(ValueError):
            stop_from_premium(100, risk_fraction=risk_fraction)

    @pytest.mark.parametrize(
        "atr_pct",
        [True, False, "0.04", float("nan"), float("inf"), 0, -0.01, None],
    )
    def test_initial_stop_rejects_invalid_atr_percentage(atr_pct):
        with pytest.raises(ValueError):
            stop_from_premium(100, underlying_atr_pct=atr_pct)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            (field, value)
            for field in (
                "entry",
                "initial_stop",
                "current_stop",
                "peak_bid",
                "tick_size",
        )
            for value in (True, False, "10", float("nan"), float("inf"), None)
        ],
    )
    def test_trailing_stop_rejects_invalid_numeric_inputs(field, value):
        values = {
            "entry": 100,
            "initial_stop": 85,
            "current_stop": 85,
            "peak_bid": 110,
            "tick_size": 0.05,
        }
        values[field] = value

        with pytest.raises(ValueError):
            trailing_stop_from_peak(**values)