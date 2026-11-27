from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.db_connection import Base
from database.models import (
    CollectionQualityEvent,
    MarketCandle,
    MarketQuoteSnapshot,
    OptionSnapshot,
    RawInstrumentMetadata,
    VerifiedInstrument,
)
from scripts.verify_dataset import (
    audit_dataset,
    configured_database_path,
    render_markdown_report,
)


NOW = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


def _instrument(key: str, kind: str, *, underlying: str | None = None, expiry="2026-12-16"):
    return VerifiedInstrument(
        instrument_key=key,
        trading_symbol=f"CRUDEOIL {kind}",
        exchange="MCX",
        segment="MCX_FO",
        instrument_type=kind,
        expiry=expiry,
        strike_price=7000 if kind in {"CE", "PE"} else 0,
        lot_size=100,
        tick_size=1,
        underlying_key=underlying,
        underlying_symbol="CRUDEOIL",
        contract_status="ACTIVE",
    )


def _quote_times(stamp: datetime, *, trade_time: datetime | None = None):
    trade = trade_time or stamp - timedelta(seconds=1)
    return stamp.replace(tzinfo=None), stamp.replace(tzinfo=None), trade.replace(tzinfo=None)


@pytest.fixture
def audit_db(tmp_path):
    path = tmp_path / "audit.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        future_key = "MCX_FO|FUT-1"
        call_key = "MCX_FO|CALL-1"
        put_key = "MCX_FO|PUT-1"
        session.add_all(
            [
                _instrument(future_key, "FUT", underlying="MCX_COM|CRUDE"),
                _instrument(call_key, "CE", underlying=future_key),
                _instrument(put_key, "PE", underlying=future_key),
            ]
        )
        snapshot, response, trade = _quote_times(NOW - timedelta(seconds=3))
        session.add(
            MarketQuoteSnapshot(
                instrument_key=future_key,
                trading_symbol="CRUDEOIL FUT",
                expiry="2026-12-16",
                timestamp=snapshot,
                response_timestamp=response,
                last_trade_time=trade,
                quality_status="VALID_FRESH",
                last_price=7000,
                bid_price=6999,
                ask_price=7001,
                volume=10,
                open_interest=20,
                source="upstox",
            )
        )
        stale_snapshot = NOW - timedelta(minutes=10)
        stale_snapshot, stale_response, stale_trade = _quote_times(
            stale_snapshot, trade_time=NOW - timedelta(minutes=10)
        )
        session.add(
            MarketQuoteSnapshot(
                instrument_key=future_key,
                trading_symbol="CRUDEOIL FUT",
                expiry="2026-12-16",
                timestamp=stale_snapshot,
                response_timestamp=stale_response,
                last_trade_time=stale_trade,
                quality_status="VALID_FRESH",
                last_price=7000,
                bid_price=6999,
                ask_price=7001,
                source="upstox",
            )
        )
        for key, option_type in ((call_key, "CE"), (put_key, "PE")):
            session.add(
                OptionSnapshot(
                    instrument_key=key,
                    trading_symbol=f"CRUDEOIL 7000 {option_type}",
                    underlying_instrument_key=future_key,
                    expiry="2026-12-16",
                    strike_price=7000,
                    option_type=option_type,
                    timestamp=snapshot,
                    response_timestamp=response,
                    last_trade_time=trade,
                    quality_status="VALID_FRESH",
                    ltp=100,
                    bid_price=99,
                    ask_price=101,
                    volume=5,
                    open_interest=10,
                    source="upstox",
                )
            )
        session.add(
            MarketQuoteSnapshot(
                instrument_key=call_key,
                trading_symbol="CRUDEOIL 7000 CE",
                expiry="2026-12-16",
                option_type="CE",
                underlying_instrument_key=future_key,
                timestamp=snapshot,
                response_timestamp=response,
                last_trade_time=trade,
                quality_status="VALID_FRESH",
                last_price=100,
                bid_price=99,
                ask_price=101,
                source="upstox",
            )
        )
        session.add(
            MarketCandle(
                instrument_key=future_key,
                exchange="MCX",
                instrument_type="FUT",
                interval="1_day",
                timestamp=datetime(2026, 11, 13, tzinfo=timezone.utc).replace(tzinfo=None),
                open=6900,
                high=7100,
                low=6800,
                close=7000,
                volume=100,
                open_interest=30,
                source="upstox",
            )
        )
        session.add(
            CollectionQualityEvent(
                collector="OPTIONS",
                instrument_key=call_key,
                classification="INVALID",
                reason="test invalid quote",
                observed_at=NOW.replace(tzinfo=None),
                response_timestamp=response,
            )
        )
        session.add(
            RawInstrumentMetadata(
                instrument_key=future_key,
                payload_hash="a" * 64,
                raw_json="{}",
                source="official_instrument_master",
                captured_at=NOW.replace(tzinfo=None),
            )
        )
    yield path
    engine.dispose()


def test_audit_reports_rows_classification_candles_and_read_only(audit_db):
    database_before = audit_db.read_bytes()
    report = audit_dataset(
        database_path=audit_db,
        now=NOW,
        freshness_seconds=60,
        freshness_threshold_seconds=20,
    )

    assert report["database_read_only"] is True
    assert report["futures_quotes"]["records_by_status"] == {"VALID": 1, "STALE": 1}
    assert report["futures_quotes"]["excluded_option_rows"] == 1
    assert report["options_quotes"]["records_by_status"] == {"VALID": 2}
    assert report["futures_quotes"]["freshness_at_collection"]["fresh_at_observation"] == 2
    assert report["futures_quotes"]["records_by_status"]["STALE"] == 1
    assert report["options_quotes"]["option_coverage"]["records_by_option_type"] == {
        "CE": 1, "PE": 1
    }
    assert len(report["futures_quotes"]["latest_20"]) == 2
    assert {row["option_type"] for row in report["options_quotes"]["latest_20"]} == {"CE", "PE"}
    assert report["historical_futures_candles"]["futures_count"] == 1
    assert report["historical_futures_candles"]["invalid_fields"] == {}
    assert report["historical_futures_candles"]["earliest_timestamp"] is not None
    assert report["collection_quality_events_by_reason"][0]["reason"] == "test invalid quote"
    assert report["unique_trading_sessions"] == 2
    assert audit_db.read_bytes() == database_before

    with sqlite3.connect(f"file:{audit_db.as_posix()}?mode=ro", uri=True) as connection:
        assert connection.execute('SELECT COUNT(*) FROM "option_snapshots"').fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute('DELETE FROM "option_snapshots"')


def test_audit_marks_obvious_test_sources_and_future_rows_unverified_or_invalid(tmp_path):
    path = tmp_path / "untrusted.db"
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE market_quote_snapshots (instrument_key TEXT, timestamp TEXT, '
        'response_timestamp TEXT, last_trade_time TEXT, last_price REAL, bid_price REAL, '
        'ask_price REAL, quality_status TEXT, source TEXT, access_token TEXT)'
    )
    stamp = NOW - timedelta(seconds=1)
    connection.executemany(
        'INSERT INTO market_quote_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            ("MCX_FO|MOCK-FUT", stamp.isoformat(), stamp.isoformat(), stamp.isoformat(),
             50, 49, 51, "VALID_FRESH", "mock", "should-not-be-reported"),
            ("MCX_FO|FUTURE", (NOW + timedelta(days=1)).isoformat(),
             (NOW + timedelta(days=1)).isoformat(), (NOW + timedelta(days=1)).isoformat(),
             50, 49, 51, "VALID_FRESH", "upstox", None),
            ("MCX_FO|BAD-BID", stamp.isoformat(), stamp.isoformat(), stamp.isoformat(),
             50, 52, 51, "VALID_FRESH", "upstox", None),
            ("MCX_FO|ZERO-LTP", stamp.isoformat(), stamp.isoformat(), stamp.isoformat(),
             0, 0, 1, "VALID_FRESH", "upstox", None),
            ("MCX_FO|MISSING-TIME", None, None, None, 50, 49, 51,
             "VALID_FRESH", "upstox", None),
            ("MCX_FO|STALE", stamp.isoformat(), stamp.isoformat(),
             (NOW - timedelta(hours=1)).isoformat(), 50, 49, 51,
             "VALID_FRESH", "upstox", None),
        ],
    )
    connection.commit()
    connection.close()

    report = audit_dataset(
        database_path=path,
        now=NOW,
        freshness_seconds=60,
        freshness_threshold_seconds=20,
    )
    assert report["futures_quotes"]["records_by_status"] == {
        "UNVERIFIED": 1, "INVALID": 4, "STALE": 1
    }
    assert report["unique_trading_sessions"] == 0
    assert report["represented_market_local_dates"] == ["2026-11-16"]
    assert "cannot prove" in report["provenance_note"]
    assert all("access_token" not in row for row in report["futures_quotes"]["latest_20"])
    assert all("should-not-be-reported" not in json.dumps(row)
               for row in report["futures_quotes"]["latest_20"])


def test_duplicate_report_handles_tables_without_constraints(tmp_path):
    path = tmp_path / "duplicates.db"
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE market_quote_snapshots (instrument_key TEXT, timestamp TEXT, '
        'last_price REAL, source TEXT)'
    )
    connection.executemany(
        'INSERT INTO market_quote_snapshots VALUES (?, ?, ?, ?)',
        [
            ("MCX_FO|FUT-1", NOW.isoformat(), 70, "upstox"),
            ("MCX_FO|FUT-1", NOW.isoformat(), 70, "upstox"),
        ],
    )
    connection.commit()
    connection.close()

    report = audit_dataset(database_path=path, now=NOW)
    assert report["futures_quotes"]["duplicates"]["duplicate_groups"] == 1
    assert report["futures_quotes"]["duplicates"]["duplicate_extra_rows"] == 1


def test_option_contract_metadata_mismatch_is_invalid(tmp_path):
    path = tmp_path / "option-mismatch.db"
    stamp = NOW - timedelta(seconds=1)
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE verified_instruments (instrument_key TEXT, trading_symbol TEXT, '
        'exchange TEXT, segment TEXT, instrument_type TEXT, expiry TEXT, strike_price REAL, '
        'underlying_key TEXT, contract_status TEXT)'
    )
    connection.execute(
        'INSERT INTO verified_instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ("MCX_FO|CALL-1", "CRUDEOIL CE", "MCX", "MCX_FO", "CE",
         "2026-12-16", 7000, "MCX_FO|FUT-1", "ACTIVE"),
    )
    connection.execute(
        'CREATE TABLE option_snapshots (instrument_key TEXT, trading_symbol TEXT, '
        'underlying_instrument_key TEXT, expiry TEXT, strike_price REAL, option_type TEXT, '
        'timestamp TEXT, response_timestamp TEXT, last_trade_time TEXT, quality_status TEXT, '
        'ltp REAL, bid_price REAL, ask_price REAL, source TEXT)'
    )
    connection.execute(
        'INSERT INTO option_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ("MCX_FO|CALL-1", "CRUDEOIL PE", "MCX_FO|FUT-1", "2026-12-16", 7000,
         "PE", stamp.isoformat(), stamp.isoformat(), stamp.isoformat(), "VALID_FRESH",
         50, 49, 51, "upstox"),
    )
    connection.commit()
    connection.close()

    report = audit_dataset(database_path=path, now=NOW)
    assert report["options_quotes"]["records_by_status"] == {"INVALID": 1}
    assert "stored option type does not match verified contract type" in (
        report["options_quotes"]["invalid_or_unverified_reasons"]
    )


def test_configured_database_path_uses_sqlite_configuration():
    path = configured_database_path("sqlite:///data/example.db")
    assert path.name == "example.db"
    assert path.parent.name == "data"

    with pytest.raises(ValueError, match="only"):
        configured_database_path("postgresql://localhost/example")


def test_report_json_is_serializable(audit_db):
    report = audit_dataset(database_path=audit_db, now=NOW)
    serialized = json.dumps(report)
    assert "futures_quotes" in serialized
    assert "access_token" not in serialized
    alignment = report["options_quotes"]["futures_alignment"]
    assert alignment["option_observations_fresh_at_collection"] == 2
    assert alignment["matched_to_same_day_futures"] == 2
    assert alignment["within_futures_poll_interval"] == 2
    markdown = render_markdown_report(report)
    assert "READ ONLY" in markdown
    assert "Futures observation alignment" in markdown
    assert "Weekend candle dates" in markdown
    assert "No model was trained" in markdown
