from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text

from database.db_connection import engine


def quality_report() -> dict:
    market_today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())

        candle_count = (
            connection.execute(text("SELECT COUNT(*) FROM market_candles")).scalar_one()
            if "market_candles" in tables
            else 0
        )
        option_count = (
            connection.execute(text("SELECT COUNT(*) FROM option_snapshots")).scalar_one()
            if "option_snapshots" in tables
            else 0
        )
        latest_option_timestamp = (
            connection.execute(text("SELECT MAX(timestamp) FROM market_quote_snapshots")).scalar_one()
            if "market_quote_snapshots" in tables
            else None
        )
        trading_sessions = (
            connection.execute(text("SELECT COUNT(DISTINCT date(timestamp)) FROM market_candles")).scalar_one()
            if "market_candles" in tables
            else 0
        )
        instrument_count = (
            connection.execute(text("SELECT COUNT(*) FROM verified_instruments")).scalar_one()
            if "verified_instruments" in tables else 0
        )
        active_instruments = (
            connection.execute(text(
                "SELECT COUNT(*) FROM verified_instruments WHERE contract_status = 'ACTIVE'"
            )).scalar_one() if "verified_instruments" in tables else 0
        )
        expired_instruments = (
            connection.execute(text(
                "SELECT COUNT(*) FROM verified_instruments WHERE contract_status = 'EXPIRED'"
            )).scalar_one() if "verified_instruments" in tables else 0
        )
        unknown_status_instruments = (
            connection.execute(text(
                "SELECT COUNT(*) FROM verified_instruments "
                "WHERE contract_status = 'UNKNOWN' OR contract_status IS NULL"
            )).scalar_one() if "verified_instruments" in tables else 0
        )
        expiry_counts = (
            connection.execute(text(
                "SELECT SUM(CASE WHEN expiry < :today THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN expiry >= :today THEN 1 ELSE 0 END) "
                "FROM verified_instruments WHERE expiry GLOB '????-??-??'"
            ), {"today": market_today}).one()
            if "verified_instruments" in tables else (0, 0)
        )
        raw_instrument_metadata_count = (
            connection.execute(text("SELECT COUNT(*) FROM raw_instrument_metadata")).scalar_one()
            if "raw_instrument_metadata" in tables else 0
        )
        contract_counts = {}
        for kind in ("FUT", "CE", "PE"):
            contract_counts[kind.lower()] = (
                connection.execute(text(
                    "SELECT COUNT(*) FROM verified_instruments WHERE instrument_type = :kind"
                ), {"kind": kind}).scalar_one() if "verified_instruments" in tables else 0
            )
        market_quote_count = (
            connection.execute(text("SELECT COUNT(*) FROM market_quote_snapshots")).scalar_one()
            if "market_quote_snapshots" in tables else 0
        )
        option_quote_count = (
            connection.execute(text("SELECT COUNT(*) FROM option_snapshots")).scalar_one()
            if "option_snapshots" in tables else 0
        )
        quote_timestamp = (
            connection.execute(text("SELECT MAX(timestamp) FROM market_quote_snapshots")).scalar_one()
            if "market_quote_snapshots" in tables else None
        )
        quote_duplicates = (
            connection.execute(text(
                "SELECT COUNT(*) FROM (SELECT instrument_key, timestamp FROM market_quote_snapshots "
                "GROUP BY instrument_key, timestamp HAVING COUNT(*) > 1)"
            )).scalar_one() if "market_quote_snapshots" in tables else 0
        )
        candle_duplicates = (
            connection.execute(text(
                "SELECT COUNT(*) FROM (SELECT instrument_key, interval, timestamp FROM market_candles "
                "GROUP BY instrument_key, interval, timestamp HAVING COUNT(*) > 1)"
            )).scalar_one() if "market_candles" in tables else 0
        )
        invalid_quotes = (
            connection.execute(text(
                "SELECT COUNT(*) FROM market_quote_snapshots "
                "WHERE last_price <= 0 OR julianday(timestamp) > julianday(:now)"
            ), {"now": datetime.now(ZoneInfo("UTC")).isoformat()}).scalar_one()
            if "market_quote_snapshots" in tables else 0
        )
        future_quotes = (
            connection.execute(text(
                "SELECT COUNT(*) FROM market_quote_snapshots "
                "WHERE julianday(timestamp) > julianday(:now)"
            ), {"now": datetime.now(ZoneInfo("UTC")).isoformat()}).scalar_one()
            if "market_quote_snapshots" in tables else 0
        )
        latest_collection_times = []
        for table, field in (("market_candles", "created_at"), ("market_quote_snapshots", "created_at"),
                             ("option_snapshots", "created_at"), ("verified_instruments", "verified_at")):
            if table in tables:
                value = connection.execute(text(f"SELECT MAX({field}) FROM {table}")).scalar_one()
                if value:
                    latest_collection_times.append(value)
        latest_collection = max(latest_collection_times, default=None)

    return {
        "candle_records": candle_count,
        "quote_snapshots": market_quote_count,
        "trading_sessions": trading_sessions,
        "last_quote_timestamp": str(latest_option_timestamp) if latest_option_timestamp else None,
        "instrument_records": instrument_count,
        "active_instruments": active_instruments,
        "expired_instruments": expired_instruments,
        "unexpired_by_expiry": int(expiry_counts[1] or 0),
        "expired_by_expiry": int(expiry_counts[0] or 0),
        "unknown_status_instruments": unknown_status_instruments,
        "raw_instrument_metadata_records": raw_instrument_metadata_count,
        "futures_contracts": contract_counts["fut"],
        "ce_contracts": contract_counts["ce"],
        "pe_contracts": contract_counts["pe"],
        "market_quote_snapshots": market_quote_count,
        "option_snapshots": option_quote_count,
        "latest_market_quote_timestamp": str(quote_timestamp) if quote_timestamp else None,
        "duplicate_records": candle_duplicates + quote_duplicates,
        "invalid_records": invalid_quotes,
        "future_quote_records": future_quotes,
        "last_successful_collection": str(latest_collection) if latest_collection else None,
    }
