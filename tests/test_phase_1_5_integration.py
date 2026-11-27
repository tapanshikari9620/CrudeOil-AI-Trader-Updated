from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_trading.auto_controller import PaperTradingController
from risk_management.risk_validator import validate_trade
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from data_collector.historical_collector import HistoricalCollector
from data_collector.instrument_collector import InstrumentCollector
from database.db_connection import (
    Base,
    _assert_no_stuck_closing_positions,
    _migrate_paper_trade_columns,
)
from database.models import MarketCandle, PaperTrade, VerifiedInstrument
from database.repository import Repository, RepositoryIntegrityError
from execution.order_manager import LiveOrderExecutionDisabled, OrderManager
from execution.upstox_client import UpstoxClient
from paper_trading.paper_execution_engine import (
    DuplicateOpenPosition,
    PaperExecutionEngine,
    PaperPositionMonitor,
)

STAMP = datetime(2026, 1, 2, 3, 45, tzinfo=timezone.utc)
METADATA = {
    "instrument_key": "MCX_FO|CRUDEOIL26JANFUT",
    "segment": "MCX_FO",
    "instrument_type": "FUT",
}


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield factory
    engine.dispose()


class MockHistoricalClient:
    def __init__(self):
        self.rows = [["2026-01-02T09:15:00+05:30", 70, 72, 69, 71, 100, 25]]

    def historical_candles(self, *args):
        return {"data": {"candles": self.rows}}


def test_live_collector_import_is_safe_and_missing_token_fails_closed(monkeypatch):
    module = importlib.import_module("scripts.start_live_collector")
    assert callable(module.main)
    monkeypatch.setattr(
        "scripts.start_live_collector.settings",
        Settings(trading_mode="PAPER", upstox_access_token=None),
    )
    with pytest.raises(SystemExit, match="UPSTOX_ACCESS_TOKEN"):
        module.main()


def test_historical_metadata_resolves_from_exact_official_master_key(monkeypatch):
    master_row = {
        **METADATA,
        "trading_symbol": "CRUDEOIL26JANFUT",
        "lot_size": 100,
        "tick_size": 1.0,
        "expiry": "2026-01-19",
        "underlying_symbol": "CRUDEOIL",
    }
    monkeypatch.setattr(InstrumentCollector, "fetch", lambda self: [master_row])
    metadata = InstrumentCollector().find_contract(METADATA["instrument_key"])
    assert metadata["instrument_type"] == "FUT"
    assert metadata["instrument_key"] == METADATA["instrument_key"]


def test_all_project_modules_import_in_isolated_process():
    root = Path(__file__).resolve().parents[1]
    script = r'''
import importlib
from pathlib import Path
root = Path.cwd()
modules = set()
for path in root.rglob("*.py"):
    if any(part in {".venv", ".pytest_cache", "__pycache__", ".idea"} for part in path.parts):
        continue
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    modules.add(".".join(parts) if parts else "main")
for name in sorted(modules):
    importlib.import_module(name)
print(f"imported={len(modules)}")
'''
    with tempfile.TemporaryDirectory() as temp_dir:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'imports.sqlite').as_posix()}"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "imported=" in result.stdout


def test_historical_candle_is_persisted_with_verified_metadata(session_factory):
    collector = HistoricalCollector(
        MockHistoricalClient(),
        Repository(session_factory),
        instrument_metadata=METADATA,
    )

    result = collector.collect(METADATA["instrument_key"], "minutes", 1, "2026-01-02")
    assert result["received"] == 1
    assert result["inserted"] == 1
    assert result["duplicates"] == 0
    assert result["invalid"] == result["duplicate_response_rows"] == 0
    assert result["first_timestamp"] == result["last_timestamp"] == STAMP
    assert result["interval"] == "1_min"
    with session_factory() as session:
        candle = session.execute(select(MarketCandle)).scalar_one()
        assert candle.instrument_type == "FUT"
        assert candle.exchange == "MCX"
        assert candle.interval == "1_min"
        assert candle.timestamp.replace(tzinfo=timezone.utc) == STAMP
        assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (70, 72, 69, 71, 100)
        required = {"instrument_key", "exchange", "instrument_type", "interval", "timestamp", "open", "high", "low",
                    "close", "volume", "source"}
        assert required <= {column["name"] for column in
                            inspect(session_factory.kw["bind"]).get_columns("market_candles")}


def test_historical_futures_normalization_uses_verified_contract(session_factory):
    metadata = {**METADATA, "instrument_type": "FUT"}
    collector = HistoricalCollector(
        MockHistoricalClient(), Repository(session_factory), instrument_metadata=metadata
    )
    assert collector.collect(metadata["instrument_key"], "minutes", 1, "2026-01-02")["inserted"] == 1
    with session_factory() as session:
        assert session.scalar(select(MarketCandle.instrument_type)) == "FUT"


@pytest.mark.parametrize("instrument_type", ["CE", "PE"])
def test_historical_futures_collector_rejects_option_contracts(session_factory, instrument_type):
    metadata = {**METADATA, "instrument_type": instrument_type}
    collector = HistoricalCollector(
        MockHistoricalClient(), Repository(session_factory), instrument_metadata=metadata
    )
    with pytest.raises(ValueError, match="only accepts verified FUT"):
        collector.collect(metadata["instrument_key"], "minutes", 1, "2026-01-02")


def test_historical_duplicate_candle_is_reported_without_second_row(session_factory):
    collector = HistoricalCollector(
        MockHistoricalClient(), Repository(session_factory), instrument_metadata=METADATA
    )
    key = METADATA["instrument_key"]
    assert collector.collect(key, "minutes", 1, "2026-01-02")["inserted"] == 1
    result = collector.collect(key, "minutes", 1, "2026-01-02")
    assert result["received"] == 1
    assert result["inserted"] == 0
    assert result["duplicates"] == 1
    assert result["invalid"] == result["duplicate_response_rows"] == 0


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {"instrument_key": METADATA["instrument_key"], "segment": "MCX_FO"},
        {**METADATA, "instrument_key": "MCX_FO|OTHER"},
        {**METADATA, "segment": "NSE_FO"},
        {**METADATA, "instrument_type": "UNKNOWN"},
    ],
)
def test_historical_collection_rejects_unverified_or_invalid_metadata(session_factory, metadata):
    collector = HistoricalCollector(MockHistoricalClient(), Repository(session_factory), metadata)
    with pytest.raises(ValueError, match="metadata|segment|instrument_type"):
        collector.collect(METADATA["instrument_key"], "minutes", 1, "2026-01-02")
    with session_factory() as session:
        assert session.scalar(select(MarketCandle.id)) is None


def test_repository_validates_required_candle_fields(session_factory):
    with pytest.raises(ValueError, match="missing required fields"):
        Repository(session_factory).add_candle({"instrument_key": METADATA["instrument_key"]})


def test_repository_rolls_back_unexpected_integrity_error(session_factory):
    with session_factory.begin() as session:
        session.execute(text(
            "CREATE TRIGGER reject_candle BEFORE INSERT ON market_candles "
            "BEGIN SELECT RAISE(ABORT, 'forced integrity rejection'); END"
        ))
    candle = {
        "instrument_key": METADATA["instrument_key"],
        "exchange": "MCX",
        "instrument_type": "FUT",
        "interval": "1m",
        "timestamp": STAMP,
        "open": 70.0,
        "high": 72.0,
        "low": 69.0,
        "close": 71.0,
        "volume": 100.0,
        "source": "upstox",
    }
    repository = Repository(session_factory)
    with pytest.raises(RepositoryIntegrityError, match="transaction rolled back"):
        repository.add_candle(candle)
    with session_factory.begin() as session:
        session.execute(text("DROP TRIGGER reject_candle"))
    assert repository.add_candle(candle)


def test_paper_trade_schema_migration_preserves_existing_rows():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE paper_trades ("
            "id INTEGER PRIMARY KEY, instrument_key VARCHAR(160) NOT NULL, side VARCHAR(4) NOT NULL, "
            "quantity INTEGER NOT NULL, entry_price FLOAT NOT NULL, exit_price FLOAT, stop_loss FLOAT NOT NULL, "
            "target FLOAT NOT NULL, status VARCHAR(12) NOT NULL, gross_pnl FLOAT NOT NULL, charges FLOAT NOT NULL, "
            "net_pnl FLOAT NOT NULL, opened_at DATETIME NOT NULL, closed_at DATETIME, notes TEXT NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO paper_trades (id, instrument_key, side, quantity, entry_price, stop_loss, target, "
            "status, gross_pnl, charges, net_pnl, opened_at, notes) "
            "VALUES (1, 'MCX|CE', 'BUY', 10, 70, 60, 90, 'OPEN', 0, 0, 0, '2026-01-02 03:45:00', 'legacy')"
        ))

    _migrate_paper_trade_columns(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT id, instrument_key, notes, created_at FROM paper_trades")).one()
        columns = {column["name"] for column in inspect(connection).get_columns("paper_trades")}

        indexes = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'paper_trades'"
                )
            ).fetchall()
        }

    assert row.id == 1 and row.instrument_key == "MCX|CE" and row.notes == "legacy"
    assert row.created_at is not None
    assert {"option_type", "last_mark_price", "unrealized_pnl", "last_quote_at", "exit_reason", "created_at"} <= columns
    assert "uq_open_paper_trade_instrument" in indexes
    assert "uq_single_open_paper_trade" in indexes
    engine.dispose()


def test_startup_guard_rejects_stuck_closing_trade_without_modifying_it():
    engine = create_engine("sqlite:///:memory:")

    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE paper_trades ("
                "id INTEGER PRIMARY KEY, status VARCHAR(12) NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO paper_trades (id, status) VALUES (1, 'CLOSING')"
            ))

        with pytest.raises(RuntimeError, match="stuck in CLOSING"):
            _assert_no_stuck_closing_positions(engine)

        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM paper_trades WHERE id = 1")
            ).scalar_one()

        assert status == "CLOSING"

    finally:
        engine.dispose()


def test_startup_guard_allows_open_and_closed_trades():
    engine = create_engine("sqlite:///:memory:")

    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE paper_trades ("
                "id INTEGER PRIMARY KEY, status VARCHAR(12) NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO paper_trades (id, status) "
                "VALUES (1, 'OPEN'), (2, 'CLOSED')"
            ))

        _assert_no_stuck_closing_positions(engine)

    finally:
        engine.dispose()


def register_verified_option(session_factory, instrument_key, option_type):
    """Register deterministic, active MCX option metadata for paper-entry tests."""
    with session_factory.begin() as session:
        session.add(
            VerifiedInstrument(
                instrument_key=instrument_key,
                trading_symbol=instrument_key.split("|")[-1],
                exchange="MCX",
                segment="MCX_FO",
                instrument_type=option_type,
                expiry="2099-12-31",
                strike_price=7000.0,
                lot_size=10,
                tick_size=0.1,
                contract_status="ACTIVE",
            )
        )


def create_position(session_factory, *, instrument_key="MCX_FO|CRUDEOIL26JAN7000CE", option_type="CE"):
    register_verified_option(session_factory, instrument_key, option_type)
    engine = PaperExecutionEngine(session_factory=session_factory)
    trade_id = engine.open_long_option(
        instrument_key, 10, 10.0, 10.1, 0.1, 8.0, 12.0, STAMP, option_type=option_type
    )
    return engine, trade_id


def option_quote(*, key="MCX_FO|CRUDEOIL26JAN7000CE", ltp=10.5, bid=10.4, ask=10.6, timestamp=STAMP):
    return {
        "instrument_key": key,
        "instrument_type": "CE",
        "timestamp": timestamp,
        "ltp": ltp,
        "bid_price": bid,
        "ask_price": ask,
        "tick_size": 0.1,
    }


def test_quote_marks_position_and_updates_unrealized_pnl(session_factory):
    engine, trade_id = create_position(session_factory)
    monitor = PaperPositionMonitor(engine)
    result = monitor.process_quote(option_quote(), now=STAMP)
    assert result[0]["status"] == "OPEN"
    assert result[0]["mark_price"] == 10.3
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "OPEN"
        assert trade.unrealized_pnl == pytest.approx(1.0)
        assert trade.last_quote_at.replace(tzinfo=timezone.utc) == STAMP


def test_target_is_not_triggered_by_last_price_without_executable_bid(session_factory):
    engine, trade_id = create_position(session_factory)
    result = engine.process_quote(
        option_quote(ltp=12.1, bid=11.8, ask=12.2), now=STAMP
    )
    assert result[0]["status"] == "OPEN"
    with session_factory() as session:
        assert session.get(PaperTrade, trade_id).status == "OPEN"


@pytest.mark.parametrize(
    ("ltp", "bid", "ask", "reason", "exit_price"),
    [
        (7.8, 7.5, 7.8, "STOP_LOSS", 7.4),
        (12.2, 12.0, 12.2, "TARGET", 11.9),
    ],
)
def test_stop_loss_and_target_trigger_conservative_paper_exit(
        session_factory, ltp, bid, ask, reason, exit_price
):
    engine, trade_id = create_position(session_factory)
    result = engine.process_quote(
        option_quote(ltp=ltp, bid=bid, ask=ask), now=STAMP, charges=2.0
    )
    assert result[0]["status"] == "CLOSED"
    assert result[0]["exit_reason"] == reason
    assert result[0]["exit_price"] == pytest.approx(exit_price)
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_reason == reason
        assert trade.exit_price == pytest.approx(exit_price)
        assert trade.charges == 2.0
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - 2.0)


def test_invalid_and_stale_quotes_are_rejected(session_factory):
    engine, _ = create_position(session_factory)
    invalid = option_quote(bid=11.0, ask=10.0)
    with pytest.raises(ValueError, match="ask_price"):
        engine.process_quote(invalid, now=STAMP)
    stale = option_quote(timestamp=STAMP - timedelta(seconds=6))
    with pytest.raises(ValueError, match="Stale"):
        engine.process_quote(stale, now=STAMP)
    future = option_quote(timestamp=STAMP + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future"):
        engine.process_quote(future, now=STAMP)


def test_out_of_order_quote_is_rejected_for_open_position(session_factory):
    engine, _ = create_position(session_factory)
    engine.process_quote(option_quote(), now=STAMP)
    older = option_quote(timestamp=STAMP - timedelta(seconds=1))
    with pytest.raises(ValueError, match="Out-of-order"):
        engine.process_quote(older, now=STAMP)


def test_futures_quote_cannot_be_used_as_an_option_premium(session_factory):
    engine, _ = create_position(session_factory)
    quote = option_quote()
    quote["instrument_type"] = "FUT"
    with pytest.raises(ValueError, match="CE or PE"):
        engine.process_quote(quote, now=STAMP)


def test_missing_position_returns_no_updates(session_factory):
    engine = PaperExecutionEngine(session_factory=session_factory)
    assert engine.process_quote(option_quote(), now=STAMP) == []


def test_duplicate_open_position_is_rejected(session_factory):
    engine, _ = create_position(session_factory)
    with pytest.raises(DuplicateOpenPosition):
        engine.open_long_option(
            "MCX_FO|CRUDEOIL26JAN7000CE", 10, 10, 10.1, 0.1, 8, 12, STAMP, option_type="CE"
        )


def test_global_open_position_limit_rejects_a_different_option_contract(session_factory):
    """The one-position cap applies across all option instruments, not per symbol."""
    engine, first_trade_id = create_position(session_factory)
    second_key = "MCX_FO|CRUDEOIL26JAN7100PE"
    register_verified_option(session_factory, second_key, "PE")

    with pytest.raises(DuplicateOpenPosition, match="global open paper position"):
        engine.open_long_option(
            second_key,
            10,
            10.0,
            10.1,
            0.1,
            8.0,
            12.0,
            STAMP,
            option_type="PE",
        )

    with session_factory() as session:
        open_trades = session.scalars(
            select(PaperTrade).where(PaperTrade.status == "OPEN")
        ).all()
        assert len(open_trades) == 1
        assert open_trades[0].id == first_trade_id
        assert open_trades[0].instrument_key == "MCX_FO|CRUDEOIL26JAN7000CE"


def test_paper_option_entry_requires_verified_ce_or_pe_type(session_factory):
    engine = PaperExecutionEngine(session_factory=session_factory)
    with pytest.raises(ValueError, match="option_type CE or PE"):
        engine.open_long_option("MCX_FO|CONTRACT", 10, 10, 10.1, 0.1, 8, 12, STAMP)


def test_repeated_exit_quote_does_not_close_or_book_twice(session_factory):
    engine, trade_id = create_position(session_factory)
    quote = option_quote(ltp=7.8, bid=7.5, ask=7.8)
    first = engine.process_quote(quote, now=STAMP, charges=1.0)
    second = engine.process_quote(quote, now=STAMP, charges=1.0)
    assert first[0]["exit_reason"] == "STOP_LOSS"
    assert second == []
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.charges == 1.0


def test_closed_or_missing_trade_cannot_be_closed_again(session_factory):
    engine, trade_id = create_position(session_factory)
    engine.close_trade(trade_id, 10.0, 10.1, 0.1, timestamp=STAMP)
    with pytest.raises(ValueError, match="Open paper trade not found"):
        engine.close_trade(trade_id, 10.0, 10.1, 0.1, timestamp=STAMP + timedelta(seconds=1))
    with pytest.raises(ValueError, match="Open paper trade not found"):
        engine.close_trade(999, 10.0, 10.1, 0.1, timestamp=STAMP)


def test_no_live_order_endpoint_is_called(monkeypatch):
    calls = []
    monkeypatch.setattr(UpstoxClient, "_get", lambda self, path, params=None: calls.append(path))
    with pytest.raises(LiveOrderExecutionDisabled):
        OrderManager().place_order(instrument_key="MCX_FO|OPTION", quantity=10)
    assert calls == []

@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_manual_exit_closes_from_fresh_exact_contract_quote(session_factory, option_type):
    key = f"MCX_FO|CRUDEOIL26JAN7000{option_type}"
    engine, trade_id = create_position(session_factory, instrument_key=key, option_type=option_type)
    quote = option_quote(key=key, timestamp=STAMP + timedelta(seconds=1))
    quote["instrument_type"] = option_type
    now = STAMP + timedelta(seconds=1)

    realized = engine.close_trade_from_quote(
        trade_id, quote, now=now, max_age_seconds=5, charges=1.0, exit_reason="MANUAL"
    )

    # BUY fill was 10.2; SELL uses bid 10.4 less one 0.1 tick = 10.3.
    assert realized == pytest.approx((10.3 - 10.2) * 10 - 1.0)
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_reason == "MANUAL"
        assert trade.exit_price == pytest.approx(10.3)
        assert trade.charges == pytest.approx(1.0)


def test_manual_exit_rejects_stale_quote_without_closing(session_factory):
    engine, trade_id = create_position(session_factory)
    stale = option_quote(timestamp=STAMP - timedelta(seconds=6))
    with pytest.raises(ValueError, match="Stale"):
        engine.close_trade_from_quote(trade_id, stale, now=STAMP, max_age_seconds=5)
    with session_factory() as session:
        assert session.get(PaperTrade, trade_id).status == "OPEN"


def test_manual_exit_rejects_quote_for_different_contract(session_factory):
    engine, trade_id = create_position(session_factory)
    wrong = option_quote(key="MCX_FO|DIFFERENT_CONTRACT")
    with pytest.raises(ValueError, match="does not match"):
        engine.close_trade_from_quote(trade_id, wrong, now=STAMP)
    with session_factory() as session:
        assert session.get(PaperTrade, trade_id).status == "OPEN"


def test_manual_exit_cannot_close_after_automatic_exit(session_factory):
    engine, trade_id = create_position(session_factory)
    stop_quote = option_quote(ltp=7.8, bid=7.5, ask=7.8)
    result = engine.process_quote(stop_quote, now=STAMP)
    assert result[0]["exit_reason"] == "STOP_LOSS"
    with pytest.raises(ValueError, match="Open paper trade not found"):
        engine.close_trade(trade_id, 10.0, 10.1, 0.1, timestamp=STAMP + timedelta(seconds=1))
    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_reason == "STOP_LOSS"

def test_exit_transaction_failure_rolls_back_to_open(session_factory, monkeypatch):
    engine, trade_id = create_position(session_factory)

    original_close = PaperExecutionEngine._close_with_fill

    def fail_after_database_updates(session, trade, fill, charges, reason):
        original_close(session, trade, fill, charges, reason)
        raise RuntimeError("simulated failure before transaction commit")

    monkeypatch.setattr(
        PaperExecutionEngine,
        "_close_with_fill",
        staticmethod(fail_after_database_updates),
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        engine.close_trade(
            trade_id,
            10.4,
            10.6,
            0.1,
            timestamp=STAMP + timedelta(seconds=1),
        )

    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "OPEN"
        assert trade.exit_price is None
        assert trade.closed_at is None
        assert trade.exit_reason is None


def test_new_execution_engine_recovers_existing_open_position(session_factory):
    original_engine, trade_id = create_position(session_factory)

    restarted_engine = PaperExecutionEngine(session_factory=session_factory)

    result = restarted_engine.process_quote(
        option_quote(timestamp=STAMP + timedelta(seconds=1)),
        now=STAMP + timedelta(seconds=1),
    )

    assert len(result) == 1
    assert result[0]["trade_id"] == trade_id
    assert result[0]["status"] == "OPEN"

    with session_factory() as session:
        trade = session.get(PaperTrade, trade_id)
        assert trade.status == "OPEN"
        assert trade.last_quote_at.replace(tzinfo=timezone.utc) == STAMP + timedelta(seconds=1)
        assert trade.last_mark_price == pytest.approx(10.3)

def test_daily_trade_count_persists_after_controller_restart(session_factory):
    execution = PaperExecutionEngine(session_factory=session_factory)

    # Create four completed PAPER trades in the same IST trading day.
    for index in range(4):
        instrument_key = f"MCX_FO|TEST{index}26JAN7000CE"

        # The execution engine correctly requires verified contract metadata.
        register_verified_option(session_factory, instrument_key, "CE")

        trade_id = execution.open_long_option(
            instrument_key,
            10,
            10.0,
            10.1,
            0.1,
            8.0,
            12.0,
            STAMP,
            option_type="CE",
        )

        execution.close_trade(
            trade_id,
            10.0,
            10.1,
            0.1,
            timestamp=STAMP + timedelta(seconds=1),
        )

    now = STAMP + timedelta(seconds=2)
    market_tz = ZoneInfo("Asia/Kolkata")
    today = now.astimezone(market_tz).date()

    # First controller instance reads the persisted database.
    first_controller = PaperTradingController.__new__(
        PaperTradingController
    )
    first_controller.repository = Repository(session_factory)

    first_trades = first_controller.repository.get_paper_trades()
    first_count = sum(
        1
        for trade in first_trades
        if first_controller._trade_day(trade) == today
    )

    assert first_count == 4

    # Simulate an application/controller restart.
    restarted_controller = PaperTradingController.__new__(
        PaperTradingController
    )
    restarted_controller.repository = Repository(session_factory)

    persisted_trades = restarted_controller.repository.get_paper_trades()

    persisted_count = sum(
        1
        for trade in persisted_trades
        if restarted_controller._trade_day(trade) == today
    )

    assert persisted_count == 4

    # Fifth trade must be allowed.
    fifth_trade = validate_trade(
        quantity=10,
        entry=10.2,
        stop=8.0,
        lot_size=10,
        trades_today=persisted_count,
        open_positions=0,
        data_timestamp=now,
        max_data_age_seconds=30,
        max_trades=5,
        max_positions=1,
        now=now,
    )

    assert fifth_trade.allowed

    # Sixth trade must be rejected.
    sixth_trade = validate_trade(
        quantity=10,
        entry=10.2,
        stop=8.0,
        lot_size=10,
        trades_today=persisted_count + 1,
        open_positions=0,
        data_timestamp=now,
        max_data_age_seconds=30,
        max_trades=5,
        max_positions=1,
        now=now,
    )

    assert not sixth_trade.allowed
    assert "max_trades_reached" in sixth_trade.reasons

    # The daily count must reset on the next IST calendar day.
    next_day = datetime(2026, 1, 2, 18, 45, tzinfo=timezone.utc)
    next_ist_date = next_day.astimezone(market_tz).date()

    next_day_count = sum(
        1
        for trade in persisted_trades
        if restarted_controller._trade_day(trade) == next_ist_date
    )

    assert next_day_count == 0
