"""Read-only integrity and provenance audit for the configured SQLite dataset."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

from config.settings import PROJECT_ROOT, settings


SNAPSHOT_TABLES = ("market_quote_snapshots", "option_snapshots")
SAFE_QUOTE_COLUMNS = {
    "instrument_key", "trading_symbol", "underlying_instrument_key", "expiry",
    "strike_price", "option_type", "timestamp", "response_timestamp",
    "last_trade_time", "quality_status", "last_price", "ltp", "bid_price",
    "bid_quantity", "ask_price", "ask_quantity", "open", "high", "low",
    "close", "previous_close", "volume", "open_interest", "source",
}
TEST_OR_MOCK_PATTERN = re.compile(r"(?:^|[|:_-])(test|mock|fixture|synthetic)(?:$|[|:_-])", re.I)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")


def configured_database_path(database_url: str = settings.database_url) -> Path:
    """Resolve the configured SQLite file without opening it or creating directories."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("Dataset audit supports only the configured SQLite database")
    database = url.database
    if not database or database == ":memory:":
        raise ValueError("Dataset audit requires a file-backed SQLite database")
    path = Path(database)
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Configured SQLite database does not exist: {path}")
    uri = path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _safe_diagnostic_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(access[_ -]?token|client[_ -]?secret|authorization)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        text,
    )
    return JWT_PATTERN.sub("[REDACTED_TOKEN]", text)


def _record_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _latest_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    limit: int = 20,
    *,
    exclude_options: bool = False,
) -> list[dict[str, Any]]:
    if "timestamp" not in columns:
        return []
    selected = sorted(columns & SAFE_QUOTE_COLUMNS)
    if "timestamp" not in selected:
        return []
    projection = ", ".join(f'q."{column}"' for column in selected)
    filters = []
    if exclude_options and "option_type" in columns:
        filters.append("COALESCE(UPPER(q.option_type), '') NOT IN ('CE', 'PE')")
    if exclude_options and "verified_instruments" in _tables(connection):
        instrument_columns = _columns(connection, "verified_instruments")
        if {"instrument_key", "instrument_type"}.issubset(instrument_columns):
            filters.append(
                "NOT EXISTS (SELECT 1 FROM verified_instruments vi "
                "WHERE vi.instrument_key = q.instrument_key "
                "AND UPPER(vi.instrument_type) IN ('CE', 'PE'))"
            )
    where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
    return [
        _record_dict(row)
        for row in connection.execute(
            f'SELECT {projection} FROM "{table}" AS q{where_clause} '
            'ORDER BY q."timestamp" DESC LIMIT ?',
            (limit,),
        )
    ]


def _duplicate_report(
    connection: sqlite3.Connection, table: str, columns: set[str]
) -> dict[str, Any]:
    if table == "market_candles":
        keys = ("instrument_key", "interval", "timestamp")
    elif table in SNAPSHOT_TABLES:
        keys = ("instrument_key", "timestamp")
    else:
        return {"duplicate_groups": 0, "duplicate_extra_rows": 0, "groups": []}
    if not set(keys).issubset(columns):
        return {"duplicate_groups": 0, "duplicate_extra_rows": 0, "groups": []}
    key_sql = ", ".join(f'"{key}"' for key in keys)
    groups = connection.execute(
        f'SELECT {key_sql}, COUNT(*) AS row_count FROM "{table}" '
        f"GROUP BY {key_sql} HAVING COUNT(*) > 1 ORDER BY row_count DESC LIMIT 100"
    ).fetchall()
    total_groups, total_rows = connection.execute(
        f'SELECT COUNT(*), COALESCE(SUM(row_count - 1), 0) FROM '
        f'(SELECT COUNT(*) AS row_count FROM "{table}" GROUP BY {key_sql} HAVING COUNT(*) > 1)'
    ).fetchone()
    return {
        "duplicate_groups": int(total_groups),
        "duplicate_extra_rows": int(total_rows),
        "groups": [
            {**{key: _json_value(row[key]) for key in keys}, "row_count": row["row_count"]}
            for row in groups
        ],
    }


def _verified_instruments(connection: sqlite3.Connection, table_names: set[str]) -> dict[str, dict[str, Any]]:
    if "verified_instruments" not in table_names:
        return {}
    if "instrument_key" not in _columns(connection, "verified_instruments"):
        return {}
    return {
        row["instrument_key"]: dict(row)
        for row in connection.execute('SELECT * FROM "verified_instruments"')
        if row["instrument_key"]
    }


def _mock_or_test_source(record: dict[str, Any]) -> bool:
    source = str(record.get("source") or "")
    key = str(record.get("instrument_key") or "")
    return (
        source.lower() not in {"upstox", "upstox_api"}
        or bool(TEST_OR_MOCK_PATTERN.search(key))
        or any(
            bool(TEST_OR_MOCK_PATTERN.search(str(record.get(field) or "")))
            for field in ("trading_symbol", "source")
        )
    )


def _metadata_reason(
    record: dict[str, Any],
    instruments: dict[str, dict[str, Any]],
    *,
    option: bool,
    today: date,
) -> str | None:
    key = record.get("instrument_key")
    if not isinstance(key, str) or not key.strip():
        return "missing instrument key"
    metadata = instruments.get(key)
    if metadata is None:
        return "instrument key has no verified metadata"
    if metadata.get("exchange") != "MCX" or metadata.get("segment") != "MCX_FO":
        return "verified metadata is not an MCX_FO contract"
    contract_status = str(metadata.get("contract_status") or "").upper()
    if contract_status != "ACTIVE":
        if contract_status not in {"INACTIVE", "EXPIRED", "DELISTED"}:
            return "contract status is unknown"
        return "contract metadata is not active"
    try:
        expiry = date.fromisoformat(str(metadata.get("expiry")))
    except (TypeError, ValueError):
        return "verified contract expiry is missing or malformed"
    if expiry < today:
        return "contract metadata is expired"
    kind = str(metadata.get("instrument_type") or "").upper()
    record_kind = str(record.get("option_type") or "").upper() if option else kind
    if option and kind not in {"CE", "PE"}:
        return "verified metadata does not identify a CE/PE contract"
    if option and record_kind != kind:
        return "stored option type does not match verified contract type"
    if not option and kind != "FUT":
        return "verified metadata does not identify a futures contract"
    if option:
        if not record.get("underlying_instrument_key"):
            return "option underlying instrument key is missing"
        if record.get("underlying_instrument_key") != metadata.get("underlying_key"):
            return "option underlying key does not match verified contract metadata"
        try:
            strike = float(record.get("strike_price"))
            verified_strike = float(metadata.get("strike_price"))
        except (TypeError, ValueError, OverflowError):
            return "option strike is missing or non-numeric"
        if not math.isfinite(strike) or strike <= 0 or strike != verified_strike:
            return "option strike does not match verified contract metadata"
        if str(record.get("expiry")) != str(metadata.get("expiry")):
            return "option expiry does not match verified contract metadata"
    return None


def _metadata_problem_is_invalid(reason: str) -> bool:
    return reason not in {
        "instrument key has no verified metadata",
        "verified contract expiry is missing or malformed",
        "contract status is unknown",
    }


def _classify_quote(
    record: dict[str, Any],
    *,
    option: bool,
    instruments: dict[str, dict[str, Any]],
    now: datetime,
    freshness_seconds: float,
) -> tuple[str, list[str], datetime | None]:
    reasons: list[str] = []
    timestamp = _as_datetime(record.get("timestamp"))
    response_timestamp = _as_datetime(record.get("response_timestamp"))
    last_trade = _as_datetime(record.get("last_trade_time"))
    ltp = record.get("ltp") if option else record.get("last_price")

    if ltp is None:
        reasons.append("missing LTP")
    else:
        try:
            numeric_ltp = float(ltp)
            if not math.isfinite(numeric_ltp) or numeric_ltp <= 0:
                reasons.append("LTP is zero, negative, or non-finite")
        except (TypeError, ValueError, OverflowError):
            reasons.append("LTP is not numeric")

    if timestamp is None:
        reasons.append("missing or malformed snapshot timestamp")
    if response_timestamp is None:
        reasons.append("missing or malformed API response timestamp")
    if last_trade is None:
        reasons.append("missing or malformed last-trade timestamp")

    for label, stamp in (
        ("snapshot", timestamp),
        ("API response", response_timestamp),
        ("last trade", last_trade),
    ):
        if stamp is not None and stamp > now:
            reasons.append(f"{label} timestamp is in the future")
    if timestamp is not None and response_timestamp is not None and response_timestamp > timestamp:
        reasons.append("API response timestamp is later than snapshot observation")
    if timestamp is not None and last_trade is not None and last_trade > timestamp:
        reasons.append("last-trade timestamp is later than snapshot observation")

    bid, ask = record.get("bid_price"), record.get("ask_price")
    for label, value in (("bid", bid), ("ask", ask)):
        if value is not None:
            try:
                numeric = float(value)
                if not math.isfinite(numeric) or numeric <= 0:
                    reasons.append(f"{label} price is non-positive or non-finite")
            except (TypeError, ValueError, OverflowError):
                reasons.append(f"{label} price is not numeric")
    if bid is not None and ask is not None:
        try:
            if float(bid) > float(ask):
                reasons.append("bid price exceeds ask price")
        except (TypeError, ValueError, OverflowError):
            pass
    for label, value in (("volume", record.get("volume")),
                         ("open interest", record.get("open_interest"))):
        if value is not None:
            try:
                numeric = float(value)
                if not math.isfinite(numeric) or numeric < 0:
                    reasons.append(f"{label} is negative or non-finite")
            except (TypeError, ValueError, OverflowError):
                reasons.append(f"{label} is not numeric")
    if not option:
        normalized_ohlc: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "previous_close"):
            value = record.get(field)
            if value is None:
                continue
            try:
                numeric = float(value)
                if not math.isfinite(numeric) or numeric < 0:
                    reasons.append(f"{field} is negative or non-finite")
                elif numeric > 0:
                    normalized_ohlc[field] = numeric
            except (TypeError, ValueError, OverflowError):
                reasons.append(f"{field} is not numeric")
        high, low = normalized_ohlc.get("high"), normalized_ohlc.get("low")
        if high is not None and low is not None and high < low:
            reasons.append("OHLC high is below low")
        if high is not None and normalized_ohlc.get("open") is not None:
            if high < normalized_ohlc["open"]:
                reasons.append("OHLC high is below open")
        if low is not None and normalized_ohlc.get("open") is not None:
            if low > normalized_ohlc["open"]:
                reasons.append("OHLC low is above open")
        if high is not None and normalized_ohlc.get("close") is not None:
            if high < normalized_ohlc["close"]:
                reasons.append("OHLC high is below close")
        if low is not None and normalized_ohlc.get("close") is not None:
            if low > normalized_ohlc["close"]:
                reasons.append("OHLC low is above close")

    quality = str(record.get("quality_status") or "").upper()
    if quality in {"INVALID", "API_ERROR"}:
        reasons.append(f"stored quality status is {quality}")

    if reasons:
        return "INVALID", reasons, last_trade

    today = now.astimezone(ZoneInfo(settings.timezone)).date()
    metadata_problem = _metadata_reason(record, instruments, option=option, today=today)
    if metadata_problem and _metadata_problem_is_invalid(metadata_problem):
        return "INVALID", [metadata_problem], last_trade

    if quality == "VALID_STALE" or (
        last_trade is not None and (now - last_trade).total_seconds() > freshness_seconds
    ):
        return "STALE", ["last-trade timestamp is older than freshness threshold"], last_trade

    provenance_reasons: list[str] = []
    if _mock_or_test_source(record):
        provenance_reasons.append("source or key indicates mock/test/unverified provenance")
    if metadata_problem:
        provenance_reasons.append(metadata_problem)
    if quality and quality != "VALID_FRESH":
        provenance_reasons.append(f"unrecognized stored quality status: {quality}")
    if provenance_reasons:
        return "UNVERIFIED", provenance_reasons, last_trade
    return "VALID", [], last_trade


def _analyze_quote_table(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    *,
    instruments: dict[str, dict[str, Any]],
    now: datetime,
    freshness_seconds: float,
) -> tuple[dict[str, int], dict[str, int], list[dict[str, Any]], dict[str, Any]]:
    is_option = table == "option_snapshots"
    counts: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    grouped: Counter[tuple[str, str]] = Counter()
    timeline: list[dict[str, Any]] = []
    timestamp_values: dict[str, list[datetime]] = defaultdict(list)
    collection_freshness: Counter[str] = Counter()
    collection_ages: list[float] = []
    collection_fresh_dates: set[str] = set()
    field_missing: Counter[str] = Counter()
    zero_ohlc: Counter[str] = Counter()
    metadata_quality: Counter[str] = Counter()
    contract_counts: Counter[str] = Counter()
    contract_keys: dict[str, set[str]] = {"CE": set(), "PE": set()}
    strikes: set[float] = set()
    expiries: set[str] = set()
    cycle_counts: dict[datetime, Counter[str]] = defaultdict(Counter)
    latest = _latest_rows(connection, table, columns, exclude_options=not is_option)
    excluded_option_rows = 0
    for row in connection.execute(f'SELECT * FROM "{table}"'):
        record = dict(row)
        metadata_type = str(
            instruments.get(record.get("instrument_key"), {}).get("instrument_type") or ""
        ).upper()
        stored_type = str(record.get("option_type") or "").upper()
        if not is_option and (stored_type in {"CE", "PE"} or metadata_type in {"CE", "PE"}):
            excluded_option_rows += 1
            continue
        for field in ("timestamp", "response_timestamp", "last_trade_time", "created_at"):
            parsed = _as_datetime(record.get(field))
            if parsed is not None:
                timestamp_values[field].append(parsed)
            elif field in {"timestamp", "last_trade_time", "response_timestamp"}:
                field_missing[field] += 1
        required_fields = [
            "instrument_key", "trading_symbol", "expiry",
            "ltp" if is_option else "last_price",
            "bid_price", "ask_price", "volume", "open_interest",
        ]
        if is_option:
            required_fields.extend(("option_type", "strike_price", "underlying_instrument_key"))
        for field in required_fields:
            if record.get(field) is None:
                field_missing[field] += 1
        if not is_option:
            for field in ("open", "high", "low", "close", "previous_close"):
                value = record.get(field)
                if value is None:
                    field_missing[field] += 1
                    continue
                try:
                    if float(value) == 0:
                        zero_ohlc[field] += 1
                except (TypeError, ValueError, OverflowError):
                    pass

        stamp = _as_datetime(record.get("timestamp"))
        trade_time = _as_datetime(record.get("last_trade_time"))
        response_time = _as_datetime(record.get("response_timestamp"))
        if stamp is not None and trade_time is not None:
            collection_age = (stamp - trade_time).total_seconds()
            if collection_age < 0:
                collection_freshness["future_trade_vs_observation"] += 1
            else:
                collection_ages.append(collection_age)
                collection_freshness[
                    "fresh_at_observation" if collection_age <= freshness_seconds
                    else "stale_at_observation"
                ] += 1
        elif trade_time is None:
            collection_freshness["missing_trade_timestamp"] += 1
        if response_time is None:
            collection_freshness["missing_response_timestamp"] += 1

        if is_option:
            option_type = stored_type if stored_type in {"CE", "PE"} else "UNKNOWN"
            contract_counts[option_type] += 1
            if record.get("instrument_key"):
                contract_keys.setdefault(option_type, set()).add(record["instrument_key"])
            try:
                strikes.add(float(record["strike_price"]))
            except (TypeError, ValueError, OverflowError):
                pass
            if record.get("expiry"):
                expiries.add(str(record["expiry"]))

        metadata_problem = _metadata_reason(
            record,
            instruments,
            option=is_option,
            today=now.astimezone(ZoneInfo(settings.timezone)).date(),
        )
        metadata_quality[metadata_problem or "verified contract metadata match"] += 1

        status, reasons, trade_time = _classify_quote(
            record,
            option=is_option,
            instruments=instruments,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        counts[status] += 1
        for reason in reasons:
            invalid_reasons[reason] += 1
        if stamp is not None and stamp <= now:
            market_date = stamp.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
            instrument_type = (
                str(record.get("option_type") or "UNKNOWN").upper()
                if is_option else str(instruments.get(record.get("instrument_key"), {}).get(
                    "instrument_type", "UNKNOWN"
                )).upper()
            )
            grouped[(market_date, instrument_type)] += 1
            if is_option and stored_type in {"CE", "PE"}:
                cycle_counts[stamp][stored_type] += 1
            at_collection, _, _ = _classify_quote(
                record,
                option=is_option,
                instruments=instruments,
                now=stamp,
                freshness_seconds=freshness_seconds,
            )
            if at_collection == "VALID":
                collection_fresh_dates.add(market_date)
            if status != "UNVERIFIED":
                timeline.append({
                    "timestamp": stamp,
                    "instrument_key": record.get("instrument_key"),
                    "status": status,
                    "status_at_collection": at_collection,
                    "instrument_type": (
                        stored_type if is_option else metadata_type
                    ),
                    "last_trade_time": trade_time,
                })
    for item in latest:
        status, reasons, _ = _classify_quote(
            item,
            option=is_option,
            instruments=instruments,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        item["audit_status"] = status
        item["audit_reasons"] = reasons
    grouped_rows = [
        {"collection_date": day, "instrument_type": kind, "records": count}
        for (day, kind), count in sorted(grouped.items())
    ]
    age_values = sorted(collection_ages)
    temporal = {
        field: {
            "earliest": min(values).isoformat() if values else None,
            "latest": max(values).isoformat() if values else None,
        }
        for field, values in timestamp_values.items()
    }
    freshness_at_collection = dict(collection_freshness)
    freshness_at_collection["age_seconds"] = {
        "minimum": age_values[0] if age_values else None,
        "median": age_values[len(age_values) // 2] if age_values else None,
        "maximum": age_values[-1] if age_values else None,
    }
    observation_dates = sorted({
        stamp.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
        for stamp in timestamp_values.get("timestamp", [])
        if stamp <= now
    })
    unique_cycle_times = sorted(cycle_counts)
    cycle_intervals = [
        (after - before).total_seconds()
        for before, after in zip(unique_cycle_times, unique_cycle_times[1:])
        if before.astimezone(ZoneInfo(settings.timezone)).date()
        == after.astimezone(ZoneInfo(settings.timezone)).date()
    ]
    option_coverage = None
    if is_option:
        option_coverage = {
            "records_by_option_type": dict(contract_counts),
            "unique_contracts_by_option_type": {
                kind: len(keys) for kind, keys in contract_keys.items()
            },
            "unique_strikes": len(strikes),
            "expiries": sorted(expiries),
            "observation_cycles": len(unique_cycle_times),
            "cycles_missing_ce": sum(not cycle.get("CE") for cycle in cycle_counts.values()),
            "cycles_missing_pe": sum(not cycle.get("PE") for cycle in cycle_counts.values()),
            "median_cycle_interval_seconds": (
                sorted(cycle_intervals)[len(cycle_intervals) // 2]
                if cycle_intervals else None
            ),
            "snapshots_per_cycle": {
                "minimum": min((sum(cycle.values()) for cycle in cycle_counts.values()), default=0),
                "maximum": max((sum(cycle.values()) for cycle in cycle_counts.values()), default=0),
            },
        }
    return dict(counts), dict(invalid_reasons), latest, {
        "grouped": grouped_rows,
        "timeline": timeline,
        "excluded_option_rows": excluded_option_rows,
        "timestamp_ranges": temporal,
        "freshness_at_collection": freshness_at_collection,
        "fresh_collection_dates": sorted(collection_fresh_dates),
        "observation_dates": observation_dates,
        "missing_fields": dict(field_missing),
        "zero_ohlc_values_treated_as_unavailable": dict(zero_ohlc),
        "contract_metadata_quality": dict(metadata_quality),
        "option_coverage": option_coverage,
    }


def _candle_audit(
    connection: sqlite3.Connection,
    columns: set[str],
    now: datetime,
) -> dict[str, Any]:
    if not {"instrument_type", "timestamp"}.issubset(columns):
        return {"futures_count": 0, "earliest_timestamp": None, "latest_timestamp": None,
                "unique_sessions": 0, "invalid_records": 0, "duplicates": {}}
    optional_fields = {
        "id", "timestamp", "open", "high", "low", "close", "volume",
        "open_interest", "interval", "source",
    }
    selected = sorted(columns & optional_fields)
    projection = ", ".join(f'"{field}"' for field in selected)
    ordering = '"id"' if "id" in columns else '"timestamp"'
    values = list(connection.execute(
        f'SELECT {projection} FROM "market_candles" '
        f'WHERE instrument_type = ? ORDER BY {ordering}',
        ("FUT",),
    ))
    timestamps = [_as_datetime(row["timestamp"]) for row in values]
    valid_timestamps = [stamp for stamp in timestamps if stamp is not None and stamp <= now]
    invalid = sum(stamp is None or stamp > now for stamp in timestamps)
    timezone = ZoneInfo(settings.timezone)
    session_dates = sorted({
        stamp.astimezone(timezone).date().isoformat()
        for stamp in valid_timestamps
    })
    weekend_session_dates = [
        day for day in session_dates if date.fromisoformat(day).weekday() >= 5
    ]
    missing_fields: Counter[str] = Counter()
    invalid_fields: Counter[str] = Counter()
    for row in values:
        for field in ("open", "high", "low", "close", "volume"):
            if field not in columns or row[field] is None:
                missing_fields[field] += 1
                continue
            try:
                number = float(row[field])
                if not math.isfinite(number) or number <= 0 and field != "volume":
                    invalid_fields[field] += 1
                elif field == "volume" and number < 0:
                    invalid_fields[field] += 1
            except (TypeError, ValueError, OverflowError):
                invalid_fields[field] += 1
        if {"open", "high", "low", "close"}.issubset(columns):
            try:
                open_price, high, low, close = (
                    float(row[field]) for field in ("open", "high", "low", "close")
                )
                if high < max(open_price, low, close) or low > min(open_price, high, close):
                    invalid_fields["ohlc_consistency"] += 1
            except (TypeError, ValueError, OverflowError):
                invalid_fields["ohlc_consistency"] += 1
    ordered_timestamps = [_as_datetime(row["timestamp"]) for row in values]
    valid_ordered = [stamp for stamp in ordered_timestamps if stamp is not None]
    chronological = all(
        before <= after
        for before, after in zip(valid_ordered, valid_ordered[1:])
    )
    interval_counts = Counter(
        str(row["interval"]) for row in values if "interval" in columns and row["interval"]
    )
    source_counts = Counter(
        str(row["source"]) for row in values if "source" in columns and row["source"]
    )
    weekday_gaps: list[str] = []
    if session_dates:
        start = date.fromisoformat(session_dates[0])
        end = date.fromisoformat(session_dates[-1])
        represented = {date.fromisoformat(value) for value in session_dates}
        candidate = start
        while candidate <= end:
            if candidate.weekday() < 5 and candidate not in represented:
                weekday_gaps.append(candidate.isoformat())
            candidate += timedelta(days=1)
    return {
        "futures_count": len(values),
        "earliest_timestamp": min(valid_timestamps).isoformat() if valid_timestamps else None,
        "latest_timestamp": max(valid_timestamps).isoformat() if valid_timestamps else None,
        "unique_sessions": len(session_dates),
        "session_dates": session_dates,
        "weekend_session_dates": weekend_session_dates,
        "interval_counts": dict(interval_counts),
        "source_counts": dict(source_counts),
        "missing_fields": dict(missing_fields),
        "invalid_fields": dict(invalid_fields),
        "chronological_in_insertion_order": chronological,
        "weekday_dates_without_candles": weekday_gaps,
        "weekday_gap_calendar_caveat": (
            "Weekday gaps are candidates only; an MCX holiday calendar was not available."
        ),
        "invalid_records": invalid,
        "duplicates": _duplicate_report(connection, "market_candles", columns),
    }


def _quality_event_summary(
    connection: sqlite3.Connection, table_names: set[str]
) -> list[dict[str, Any]]:
    if "collection_quality_events" not in table_names:
        return []
    columns = _columns(connection, "collection_quality_events")
    if not {"classification", "reason"}.issubset(columns):
        return []
    collector_expr = '"collector"' if "collector" in columns else "'UNKNOWN' AS collector"
    rows = connection.execute(
        f'SELECT {collector_expr}, classification, reason, COUNT(*) AS records '
        'FROM "collection_quality_events" '
        "GROUP BY collector, classification, reason ORDER BY records DESC, reason"
    )
    return [
        {**dict(row), "reason": _safe_diagnostic_text(row["reason"])}
        for row in rows
    ]


def _timeline_gaps(
    timeline: Iterable[dict[str, Any]],
    threshold: float,
    timezone_name: str,
) -> list[dict[str, Any]]:
    stamps = sorted({item["timestamp"] for item in timeline if item.get("timestamp")})
    gaps = []
    for before, after in zip(stamps, stamps[1:]):
        local_tz = ZoneInfo(timezone_name)
        if before.astimezone(local_tz).date() != after.astimezone(local_tz).date():
            continue
        seconds = (after - before).total_seconds()
        if seconds > threshold:
            gaps.append({
                "from": before.isoformat(),
                "to": after.isoformat(),
                "gap_seconds": seconds,
            })
    return gaps


def audit_dataset(
    *,
    database_path: Path | None = None,
    now: datetime | None = None,
    freshness_seconds: float = settings.live_quote_freshness_seconds,
    freshness_threshold_seconds: float | None = None,
) -> dict[str, Any]:
    """Audit the configured database using a SQLite read-only connection."""
    path = (database_path or configured_database_path()).resolve()
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    threshold = (
        freshness_threshold_seconds
        if freshness_threshold_seconds is not None
        else max(settings.live_futures_poll_seconds, settings.live_options_poll_seconds) * 2
    )
    if not math.isfinite(freshness_seconds) or freshness_seconds <= 0:
        raise ValueError("freshness_seconds must be finite and positive")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("freshness_threshold_seconds must be finite and positive")

    with _read_only_connection(path) as connection:
        names = _tables(connection)
        name_set = set(names)
        columns = {table: _columns(connection, table) for table in names}
        table_counts = {table: _table_count(connection, table) for table in names}
        instruments = _verified_instruments(connection, name_set)
        quote_tables: dict[str, Any] = {}
        all_timeline: list[dict[str, Any]] = []
        quote_timeline: list[dict[str, Any]] = []
        grouped: Counter[tuple[str, str]] = Counter()
        for table in SNAPSHOT_TABLES:
            if table not in name_set:
                continue
            statuses, reasons, latest, extra = _analyze_quote_table(
                connection,
                table,
                columns[table],
                instruments=instruments,
                now=current_time,
                freshness_seconds=freshness_seconds,
            )
            grouped_rows = extra["grouped"]
            timeline = extra["timeline"]
            all_timeline.extend(timeline)
            quote_timeline.extend(timeline)
            for row in grouped_rows:
                grouped[(row["collection_date"], row["instrument_type"])] += row["records"]
            quote_tables[table] = {
                "records_by_status": statuses,
                "invalid_or_unverified_reasons": reasons,
                "duplicates": _duplicate_report(connection, table, columns[table]),
                "latest_20": latest,
                "excluded_option_rows": extra["excluded_option_rows"],
                "timestamp_ranges": extra["timestamp_ranges"],
                "freshness_at_collection": extra["freshness_at_collection"],
                "fresh_collection_dates": extra["fresh_collection_dates"],
                "observation_dates": extra["observation_dates"],
                "missing_fields": extra["missing_fields"],
                "zero_ohlc_values_treated_as_unavailable": (
                    extra["zero_ohlc_values_treated_as_unavailable"]
                ),
                "contract_metadata_quality": extra["contract_metadata_quality"],
                "option_coverage": extra["option_coverage"],
            }

        candle_report = (
            _candle_audit(connection, columns["market_candles"], current_time)
            if "market_candles" in name_set
            else {"futures_count": 0, "earliest_timestamp": None, "latest_timestamp": None,
                  "invalid_records": 0, "duplicates": {}}
        )

        # Historical candle dates count as represented sessions only when not future-dated.
        if "market_candles" in name_set and "instrument_type" in columns["market_candles"]:
            for row in connection.execute(
                'SELECT timestamp, instrument_type FROM "market_candles" '
                'WHERE instrument_type = "FUT"'
            ):
                stamp = _as_datetime(row["timestamp"])
                if stamp is None or stamp > current_time:
                    continue
                day = stamp.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
                grouped[(day, "FUT")] += 1
                all_timeline.append({
                    "timestamp": stamp,
                    "instrument_key": None,
                    "status": "UNVERIFIED",
                    "last_trade_time": None,
                })

        sessions = sorted({row["timestamp"].astimezone(ZoneInfo(settings.timezone)).date().isoformat()
                           for row in all_timeline})
        qualified_session_dates = set(candle_report.get("session_dates", []))
        for section in quote_tables.values():
            qualified_session_dates.update(section.get("fresh_collection_dates", []))
        futures_report = quote_tables.get("market_quote_snapshots", {
            "records_by_status": {}, "invalid_or_unverified_reasons": {},
            "duplicates": {"duplicate_groups": 0, "duplicate_extra_rows": 0, "groups": []},
            "latest_20": [], "excluded_option_rows": 0, "timestamp_ranges": {},
            "freshness_at_collection": {}, "fresh_collection_dates": [],
            "observation_dates": [], "missing_fields": {},
        })
        options_report = quote_tables.get("option_snapshots", {
            "records_by_status": {}, "invalid_or_unverified_reasons": {},
            "duplicates": {"duplicate_groups": 0, "duplicate_extra_rows": 0, "groups": []},
            "latest_20": [], "timestamp_ranges": {}, "freshness_at_collection": {},
            "fresh_collection_dates": [], "observation_dates": [], "missing_fields": {},
            "option_coverage": {},
        })
        market_timezone = ZoneInfo(settings.timezone)
        futures_observations = [
            item for item in all_timeline
            if item.get("instrument_type") == "FUT"
            and item.get("status_at_collection") == "VALID"
        ]
        options_observations = [
            item for item in all_timeline
            if item.get("instrument_type") in {"CE", "PE"}
            and item.get("status_at_collection") == "VALID"
        ]
        alignment_deltas = []
        for option_observation in options_observations:
            option_time = option_observation["timestamp"]
            matching_futures = [
                item["timestamp"] for item in futures_observations
                if item["timestamp"].astimezone(market_timezone).date()
                == option_time.astimezone(market_timezone).date()
            ]
            if matching_futures:
                alignment_deltas.append(
                    min(abs((option_time - future_time).total_seconds())
                        for future_time in matching_futures)
                )
        options_report["futures_alignment"] = {
            "option_observations_fresh_at_collection": len(options_observations),
            "matched_to_same_day_futures": len(alignment_deltas),
            "within_futures_poll_interval": sum(
                delta <= settings.live_futures_poll_seconds for delta in alignment_deltas
            ),
            "nearest_delta_seconds": {
                "median": statistics.median(alignment_deltas) if alignment_deltas else None,
                "maximum": max(alignment_deltas) if alignment_deltas else None,
            },
            "matching_rule": (
                "Nearest recorded futures observation on the same market-local date; "
                "not proof of simultaneous exchange execution."
            ),
        }
        candle_ready = (
            candle_report["futures_count"] >= 20
            and not candle_report.get("missing_fields")
            and not candle_report.get("invalid_fields")
            and candle_report.get("invalid_records", 0) == 0
            and candle_report.get("duplicates", {}).get("duplicate_groups", 0) == 0
        )
        quote_session_count = len(options_report.get("fresh_collection_dates", []))
        report = {
            "generated_at": current_time.isoformat(),
            "database_path": str(path),
            "database_read_only": True,
            "freshness_seconds": freshness_seconds,
            "timestamp_storage_note": (
                "Project collectors persist timezone-aware UTC datetimes; SQLite's DATETIME "
                "columns are timezone-naive, so stored values are interpreted as UTC."
            ),
            "tables": [
                {"table": table, "record_count": table_counts[table]}
                for table in names
            ],
            "futures_quotes": futures_report,
            "options_quotes": options_report,
            "historical_futures_candles": candle_report,
            "collection_quality_events_by_reason": _quality_event_summary(connection, name_set),
            "collection_date_and_instrument_type": [
                {"collection_date": day, "instrument_type": kind, "records": count}
                for (day, kind), count in sorted(grouped.items())
            ],
            "unique_trading_sessions": len(qualified_session_dates),
            "trading_session_dates": sorted(qualified_session_dates),
            "represented_market_local_dates": sessions,
            "session_count_note": (
                "Qualified dates combine historical futures candle dates with dates having "
                "quotes fresh at observation time. Dates are not independently validated "
                "against an MCX holiday/session calendar."
            ),
            "dataset_readiness": {
                "historical_futures_analysis": {
                    "status": "PARTIALLY READY" if candle_ready else "NOT READY",
                    "reason": (
                        f"{candle_report['futures_count']} daily candles are available; "
                        "their source cannot be independently authenticated from SQLite."
                    ),
                },
                "options_price_prediction": {
                    "status": "PARTIALLY READY" if quote_session_count >= 5 else "NOT READY",
                    "reason": (
                        f"Only {quote_session_count} fresh quote session(s) are represented; "
                        "there are no labeled historical option outcomes."
                    ),
                },
                "ce_pe_directional_prediction": {
                    "status": "PARTIALLY READY" if quote_session_count >= 5 else "NOT READY",
                    "reason": (
                        "CE/PE observations exist, but session coverage and matched future "
                        "outcome labels are insufficient."
                    ),
                },
                "future_outcome_target_generation": {
                    "status": "PARTIALLY READY" if candle_ready else "NOT READY",
                    "reason": (
                        "Forward futures labels can be derived chronologically from daily "
                        "candles; this dataset is too small and provenance is unverified."
                    ),
                },
                "chronological_train_validation_test_split": {
                    "status": "PARTIALLY READY" if candle_ready else "NOT READY",
                    "reason": (
                        "A chronological split is technically possible for the candle series "
                        "but too small for stable evaluation; options have one fresh session."
                    ),
                },
                "leakage_prevention": {
                    "status": "NOT READY",
                    "reason": (
                        "No explicit feature/label alignment, purge/embargo, or point-in-time "
                        "contract-universe dataset is present."
                    ),
                },
                "backtesting": {
                    "status": "NOT READY",
                    "reason": (
                        "Quote history is short, currently stale, partially missing, and not "
                        "independently authenticated; transaction-cost and execution models "
                        "are not validated by this dataset."
                    ),
                },
            },
            "collection_timeline_gaps": {
                "threshold_seconds": threshold,
                "gaps": _timeline_gaps(quote_timeline, threshold, settings.timezone),
            },
            "provenance_note": (
                "SQLite records alone cannot prove whether rows originated from real broker "
                "responses or test fixtures. Rows with unknown source, mock/test-like keys, "
                "or missing verified instrument metadata are classified UNVERIFIED and are "
                "not represented as confirmed genuine market data."
            ),
        }
        return report


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# CrudeOil-AI-Trader Dataset Verification",
        "",
        f"- Generated (UTC): `{report['generated_at']}`",
        f"- Database: `{report['database_path']}`",
        f"- Database access: **READ ONLY**",
        f"- Freshness threshold: **{report['freshness_seconds']} seconds**",
        "",
        "## Database tables",
        "",
        "| Table | Records |",
        "|---|---:|",
    ]
    lines.extend(f"| `{row['table']}` | {row['record_count']} |" for row in report["tables"])
    for label, key in (("Futures", "futures_quotes"), ("Options", "options_quotes")):
        section = report[key]
        lines.extend([
            "",
            f"## {label} quote quality",
            "",
            f"- Current status counts: `{json.dumps(section['records_by_status'], sort_keys=True)}`",
            f"- Fresh at collection: `{json.dumps(section['freshness_at_collection'], sort_keys=True)}`",
            f"- Timestamp ranges (UTC): `{json.dumps(section['timestamp_ranges'], sort_keys=True)}`",
            f"- Contract metadata checks: `{json.dumps(section.get('contract_metadata_quality', {}), sort_keys=True)}`",
            f"- Missing fields: `{json.dumps(section['missing_fields'], sort_keys=True)}`",
            f"- Zero OHLC values treated as unavailable: "
            f"`{json.dumps(section.get('zero_ohlc_values_treated_as_unavailable', {}), sort_keys=True)}`",
            f"- Rejection reasons: `{json.dumps(section['invalid_or_unverified_reasons'], sort_keys=True)}`",
            f"- Duplicate groups: **{section['duplicates']['duplicate_groups']}**",
        ])
        if label == "Futures":
            lines.append(
                f"- Option rows excluded from mixed quote table: **{section['excluded_option_rows']}**"
            )
        else:
            lines.append(
                f"- Contract/session coverage: `{json.dumps(section.get('option_coverage', {}), sort_keys=True)}`"
            )
            lines.append(
                f"- Futures observation alignment: "
                f"`{json.dumps(section.get('futures_alignment', {}), sort_keys=True)}`"
            )
    candle = report["historical_futures_candles"]
    lines.extend([
        "",
        "## Historical futures candles",
        "",
        f"- Count / unique dates: **{candle['futures_count']} / {candle.get('unique_sessions', 0)}**",
        f"- Timestamp range (UTC): `{candle['earliest_timestamp']}` to `{candle['latest_timestamp']}`",
        f"- Intervals / sources: `{json.dumps(candle.get('interval_counts', {}), sort_keys=True)}` / "
        f"`{json.dumps(candle.get('source_counts', {}), sort_keys=True)}`",
        f"- Missing fields: `{json.dumps(candle.get('missing_fields', {}), sort_keys=True)}`",
        f"- Invalid fields: `{json.dumps(candle.get('invalid_fields', {}), sort_keys=True)}`",
        f"- Chronological in insertion order: **{candle.get('chronological_in_insertion_order')}**",
        f"- Weekend candle dates (verify against MCX calendar): "
        f"`{json.dumps(candle.get('weekend_session_dates', []))}`",
        f"- Duplicate groups: **{candle.get('duplicates', {}).get('duplicate_groups', 0)}**",
        f"- Candidate weekday gaps: `{json.dumps(candle.get('weekday_dates_without_candles', []))}`",
        "",
        "## Collection sessions and readiness",
        "",
        f"- Qualified candidate session dates: **{report['unique_trading_sessions']}** — "
        f"`{', '.join(report['trading_session_dates'])}`",
        f"- All represented market-local dates (including invalid/stale): **"
        f"{len(report['represented_market_local_dates'])}**",
        f"- {report['session_count_note']}",
        "",
        "| Assessment | Status | Evidence / limitation |",
        "|---|---|---|",
    ])
    lines.extend(
        f"| {name.replace('_', ' ').title()} | **{item['status']}** | {item['reason']} |"
        for name, item in report["dataset_readiness"].items()
    )
    lines.extend([
        "",
        "## Collection rejection diagnostics",
        "",
        "| Collector | Classification | Reason | Count |",
        "|---|---|---|---:|",
    ])
    lines.extend(
        f"| {row.get('collector', 'UNKNOWN')} | {row['classification']} | "
        f"{row['reason']} | {row['records']} |"
        for row in report["collection_quality_events_by_reason"]
    )
    lines.extend([
        "",
        "## Provenance and interpretation",
        "",
        report["provenance_note"],
        "",
        "The `source` label and verified instrument-master metadata are supporting records, "
        "not independent proof of broker origin. The collector stores valid-fresh quotes only; "
        "the audit reclassifies them relative to its own generation time, so a quote can be "
        "fresh at collection and stale now. Existing rows marked `VALID_FRESH` are not rewritten.",
        "",
        "No model was trained. These results do not imply or guarantee a trading return.",
        "",
    ])
    return "\n".join(lines)


def print_report(report: dict[str, Any]) -> None:
    print(f"Dataset audit: {report['database_path']} (READ ONLY)")
    print("\nTables:")
    for item in report["tables"]:
        print(f"  {item['table']}: {item['record_count']}")
    for label, key in (("Futures quotes", "futures_quotes"), ("Options quotes", "options_quotes")):
        section = report[key]
        print(f"\n{label}: statuses={section['records_by_status']}")
        print(f"  duplicates: {section['duplicates']['duplicate_groups']} duplicate groups")
        for row in section["latest_20"]:
            print("  " + json.dumps(row, ensure_ascii=True, sort_keys=True))
    candle = report["historical_futures_candles"]
    print(
        "\nHistorical futures candles: "
        f"{candle['futures_count']}, earliest={candle['earliest_timestamp']}, "
        f"latest={candle['latest_timestamp']}, invalid={candle['invalid_records']}"
    )
    print("\nCollection quality events by reason:")
    for item in report["collection_quality_events_by_reason"]:
        print("  " + json.dumps(item, ensure_ascii=True, sort_keys=True))
    print(f"\nUnique trading sessions represented: {report['unique_trading_sessions']}")
    print("Collection timeline gaps:")
    for item in report["collection_timeline_gaps"]["gaps"]:
        print("  " + json.dumps(item, ensure_ascii=True, sort_keys=True))
    if not report["collection_timeline_gaps"]["gaps"]:
        print("  none above configured gap threshold")
    print("\n" + report["provenance_note"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=settings.data_dir / "dataset_audit_final.json",
        help="JSON report output path",
    )
    parser.add_argument(
        "--markdown-report",
        type=Path,
        default=settings.data_dir / "dataset_audit_final.md",
        help="human-readable Markdown report output path",
    )
    parser.add_argument(
        "--freshness-seconds",
        type=float,
        default=settings.live_quote_freshness_seconds,
        help="maximum last-trade age for a fresh quote",
    )
    args = parser.parse_args(argv)
    report = audit_dataset(freshness_seconds=args.freshness_seconds)
    print_report(report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=True, default=_json_value) + "\n",
        encoding="utf-8",
    )
    print(f"\nJSON report saved to: {args.report.resolve()}")
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.write_text(render_markdown_report(report), encoding="utf-8")
    print(f"Markdown report saved to: {args.markdown_report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
