from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import threading

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from data_collector.continuous_live_collector import (
    ContinuousMarketDataCollector,
    normalize_live_quote,
)
from data_collector.upstox_read_only import QuoteValidationError
from database.db_connection import Base
from database.models import CollectionQualityEvent, MarketQuoteSnapshot, OptionSnapshot
from database.repository import Repository
from execution.upstox_client import UpstoxAPIError, UpstoxClient, UpstoxRateLimitError


NOW = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)


def contract(kind: str = "FUT", key: str | None = None, expiry: str = "2026-12-16") -> dict:
    return {
        "instrument_key": key or f"MCX_FO|CRUDE-{kind}",
        "trading_symbol": f"CRUDEOIL {kind}",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": kind,
        "expiry": expiry,
        "strike_price": 7000 if kind in {"CE", "PE"} else 0,
        "lot_size": 100,
        "tick_size": 1,
        "underlying_key": "MCX_FO|CRUDE-UNDERLYING",
        "underlying_symbol": "CRUDEOIL",
        "contract_status": "ACTIVE",
    }


def raw_quote(item: dict, **overrides) -> dict:
    result = {
        "instrument_token": item["instrument_key"],
        "timestamp": NOW.isoformat(),
        "last_trade_time": str(int((NOW - timedelta(seconds=1)).timestamp() * 1000)),
        "last_price": 70.5,
        "depth": {
            "buy": [{"price": 70.4, "quantity": 3, "orders": 1}],
            "sell": [{"price": 70.6, "quantity": 4, "orders": 2}],
        },
        "ohlc": {"open": 70, "high": 71, "low": 69, "close": 68},
        "volume": 100,
        "oi": 200,
    }
    result.update(overrides)
    return result


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.parametrize("kind", ["FUT", "CE", "PE"])
def test_valid_futures_and_call_put_quotes_have_separate_timestamps(kind):
    item = contract(kind)
    normalized, status = normalize_live_quote(
        item, raw_quote(item), snapshot_at=NOW, freshness_seconds=60
    )
    assert status == "VALID_FRESH"
    assert normalized["last_price"] == 70.5
    assert normalized["timestamp"] == NOW
    assert normalized["response_timestamp"] == NOW
    assert normalized["last_trade_time"] < normalized["response_timestamp"]
    assert normalized["close"] == normalized["previous_close"] == 68
    assert normalized["bid_quantity"] == 3
    assert normalized["ask_quantity"] == 4
    assert normalized["option_type"] == (kind if kind in {"CE", "PE"} else None)


@pytest.mark.parametrize("ltp", [0, -0.01])
def test_zero_and_negative_ltp_are_invalid(ltp):
    item = contract()
    with pytest.raises(QuoteValidationError, match="last_price"):
        normalize_live_quote(
            item, raw_quote(item, last_price=ltp), snapshot_at=NOW, freshness_seconds=60
        )


def test_zero_ltp_is_logged_with_safe_quote_diagnostics(session_factory, tmp_path, caplog):
    client = QuoteClient()
    item = contract("FUT")
    client.quotes[item["instrument_key"]] = raw_quote(item, last_price=0)
    collector = _collector(client, session_factory, tmp_path)

    with caplog.at_level("WARNING", logger="crude_oil_trader.collector"):
        result = collector.run_cycle(NOW - timedelta(seconds=2))

    assert result["futures"]["invalid"] == 1
    diagnostic = next(record.message for record in caplog.records if "quote rejected" in record.message)
    assert "last_price must be finite and positive" in diagnostic
    assert "ltp=0" in diagnostic
    assert "response_timestamp=" in diagnostic
    assert "test-token-not-logged" not in diagnostic


@pytest.mark.parametrize("field", ["timestamp", "last_trade_time"])
def test_missing_api_or_last_trade_timestamp_is_invalid(field):
    item = contract()
    payload = raw_quote(item)
    payload.pop(field)
    with pytest.raises(QuoteValidationError, match="timestamp"):
        normalize_live_quote(item, payload, snapshot_at=NOW, freshness_seconds=60)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("timestamp", "response timestamp"),
        ("last_trade_time", "last-trade timestamp"),
    ],
)
def test_future_api_or_last_trade_timestamp_is_rejected(field, message):
    item = contract()
    payload = raw_quote(
        item,
        **{
            field: (NOW + timedelta(seconds=6)).isoformat()
            if field == "timestamp"
                else str(int((NOW + timedelta(seconds=6)).timestamp() * 1000))
        },
    )
    with pytest.raises(QuoteValidationError, match=message):
        normalize_live_quote(
            item, payload, snapshot_at=NOW, freshness_seconds=60
        )


def test_small_upstox_response_clock_skew_is_tolerated():
    item = contract()
    payload = raw_quote(
        item,
        timestamp=(NOW + timedelta(seconds=1)).isoformat(),
        last_trade_time=str(
            int((NOW - timedelta(seconds=1)).timestamp() * 1000)
        ),
    )

    normalized, status = normalize_live_quote(
        item, payload, snapshot_at=NOW, freshness_seconds=60
    )

    assert status == "VALID_FRESH"
    assert normalized["response_timestamp"] == NOW + timedelta(seconds=1)
    assert normalized["last_trade_time"] < NOW


def test_future_last_trade_timestamp_is_rejected_even_with_valid_response_time():
    item = contract()
    payload = raw_quote(
        item,
        timestamp=NOW.isoformat(),
        last_trade_time=str(
            int((NOW + timedelta(seconds=1)).timestamp() * 1000)
        ),
    )

    with pytest.raises(QuoteValidationError, match="last-trade timestamp is in the future"):
        normalize_live_quote(
            item, payload, snapshot_at=NOW, freshness_seconds=60
        )


def test_stale_last_trade_is_classified_separately_from_response_timestamp():
    item = contract()
    payload = raw_quote(item, last_trade_time=str(int((NOW - timedelta(minutes=5)).timestamp() * 1000)))
    normalized, status = normalize_live_quote(
        item, payload, snapshot_at=NOW, freshness_seconds=60
    )
    assert status == "VALID_STALE"
    assert normalized["response_timestamp"] == NOW


def test_zero_optional_ohlc_values_are_preserved_as_unavailable_and_persistable(
    session_factory,
):
    item = contract("CE")
    normalized, status = normalize_live_quote(
        item,
        raw_quote(item, ohlc={"open": 0, "high": 71, "low": 0, "close": 0}),
        snapshot_at=NOW,
        freshness_seconds=60,
    )

    assert status == "VALID_FRESH"
    assert normalized["open"] is None
    assert normalized["high"] == 71
    assert normalized["low"] is None
    assert normalized["close"] is None
    assert Repository(session_factory).add_market_quote(normalized, instrument=item)


def test_negative_optional_ohlc_values_are_invalid():
    item = contract("FUT")
    with pytest.raises(QuoteValidationError, match="ohlc.open"):
        normalize_live_quote(
            item,
            raw_quote(item, ohlc={"open": -1}),
            snapshot_at=NOW,
            freshness_seconds=60,
        )


@pytest.mark.parametrize(
    "depth",
    [
        {"buy": [{"price": 71, "quantity": 2}], "sell": [{"price": 70, "quantity": 2}]},
        {"buy": [{"price": -1, "quantity": 2}], "sell": [{"price": 72, "quantity": 2}]},
        {"buy": [{"price": 70, "quantity": -2}], "sell": []},
    ],
)
def test_crossed_or_invalid_bid_ask_depth_is_rejected(depth):
    item = contract()
    with pytest.raises(QuoteValidationError):
        normalize_live_quote(
            item, raw_quote(item, depth=depth), snapshot_at=NOW, freshness_seconds=60
        )


def test_expired_contract_and_invalid_metadata_are_rejected():
    expired = contract(expiry=(NOW.date() - timedelta(days=1)).isoformat())
    with pytest.raises(QuoteValidationError, match="expired"):
        normalize_live_quote(
            expired, raw_quote(expired), snapshot_at=NOW, freshness_seconds=60
        )
    malformed = contract()
    malformed["instrument_key"] = ""
    with pytest.raises(QuoteValidationError, match="metadata"):
        normalize_live_quote(
            malformed, raw_quote(malformed), snapshot_at=NOW, freshness_seconds=60
        )


class QuoteClient:
    def __init__(self, status="NORMAL_OPEN", *, failure=None):
        self.status = status
        self.failure = failure
        self.calls = []
        self.quotes = {}

    def market_status(self, exchange):
        self.calls.append(("market_status", exchange))
        return {"status": "success", "data": {"status": self.status}}

    def market_quotes(self, keys):
        self.calls.append(("market_quotes", tuple(keys)))
        if self.failure:
            raise self.failure
        return {
            "status": "success",
            "data": {f"quote-{index}": self.quotes[key] for index, key in enumerate(keys) if key in self.quotes},
        }


def _collector(client, session_factory, tmp_path, *, max_age=60, status="PAPER"):
    config = Settings(
        trading_mode=status,
        upstox_access_token="test-token-not-logged",
        mcx_market_open_time="00:00",
        mcx_market_close_time="23:59",
        live_quote_freshness_seconds=max_age,
    )
    collector = ContinuousMarketDataCollector(
        client,
        Repository(session_factory),
        config=config,
        health_path=tmp_path / "live_collection_health.json",
        clock=lambda: 100.0,
        utc_clock=lambda: NOW,
    )
    collector._last_refresh = 100.0
    collector.future = contract("FUT")
    collector.options = [contract("CE"), contract("PE")]
    return collector


def test_valid_futures_and_ce_pe_snapshots_persist_once(session_factory, tmp_path):
    client = QuoteClient()
    future, call, put = contract("FUT"), contract("CE"), contract("PE")
    client.quotes = {item["instrument_key"]: raw_quote(item) for item in (future, call, put)}
    collector = _collector(client, session_factory, tmp_path)

    future_result = collector.run_cycle(NOW)
    option_result = collector.collect_options_cycle(NOW)
    with session_factory() as session:
        reasons = [event.reason for event in session.scalars(select(CollectionQualityEvent))]
    assert future_result["futures"]["persisted"] == 1, (future_result, reasons)
    assert option_result["persisted"] == 2
    assert collector.collect_options_cycle(NOW)["persisted"] == 0
    with session_factory() as session:
        futures = list(session.scalars(select(MarketQuoteSnapshot).where(MarketQuoteSnapshot.option_type.is_(None))))
        options = list(session.scalars(select(OptionSnapshot)))
        assert len(futures) == 1
        assert len(options) == 2
        assert {item.option_type for item in options} == {"CE", "PE"}
        assert all(item.quality_status == "VALID_FRESH" for item in options)
        assert all(item.underlying_instrument_key == call["underlying_key"] for item in options)


def test_quote_response_timestamp_after_request_start_is_validated_at_receipt_time(
    session_factory, tmp_path
):
    client = QuoteClient()
    future = contract("FUT")
    client.quotes[future["instrument_key"]] = raw_quote(future)
    collector = _collector(client, session_factory, tmp_path)

    result = collector.run_cycle(NOW - timedelta(seconds=2))

    assert result["futures"]["persisted"] == 1
    with session_factory() as session:
        snapshot = session.scalar(select(MarketQuoteSnapshot))
        assert snapshot.timestamp == NOW.replace(tzinfo=None)
        assert snapshot.response_timestamp == NOW.replace(tzinfo=None)
        assert snapshot.last_trade_time < snapshot.response_timestamp


def test_stale_or_invalid_quotes_are_events_not_snapshots(session_factory, tmp_path):
    client = QuoteClient()
    item = contract()
    client.quotes[item["instrument_key"]] = raw_quote(
        item, last_trade_time=str(int((NOW - timedelta(minutes=10)).timestamp() * 1000))
    )
    collector = _collector(client, session_factory, tmp_path, max_age=60)
    result = collector.run_cycle(NOW - timedelta(seconds=2))
    assert result["futures"]["stale"] == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 0
        event = session.scalar(select(CollectionQualityEvent))
        assert event.classification == "VALID_STALE"


def test_api_rate_limit_is_recorded_and_cycle_continues(session_factory, tmp_path):
    client = QuoteClient(failure=UpstoxRateLimitError("rate limit", status_code=429))
    collector = _collector(client, session_factory, tmp_path)
    result = collector.run_cycle(NOW)
    assert result["futures"]["api_error"] == 1
    with session_factory() as session:
        event = session.scalar(select(CollectionQualityEvent))
        assert event.classification == "API_ERROR"
        assert "rate limit" in event.reason


def test_rest_client_retries_timeout_using_only_full_quote_get():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if len(requests) == 1:
            raise httpx.ReadTimeout("temporary timeout")
        key = "MCX_FO|CRUDE-FUT"
        return httpx.Response(200, json={
            "status": "success",
            "data": {"MCX_FO:CRUDE-FUT": raw_quote(contract("FUT", key))},
        })

    client = UpstoxClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        retry_count=1,
        retry_backoff_seconds=0,
    )
    try:
        assert client.market_quotes(["MCX_FO|CRUDE-FUT"])["status"] == "success"
    finally:
        client.close()
    assert requests == [
        ("GET", "/v2/market-quote/quotes"),
        ("GET", "/v2/market-quote/quotes"),
    ]


def test_option_contract_request_uses_documented_underlying_query_parameter():
    observed = []
    underlying_key = "MCX_FO|CRUDE-FUT"

    def handler(request):
        observed.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"status": "success", "data": []})

    client = UpstoxClient("test-token", transport=httpx.MockTransport(handler))
    try:
        client.option_contracts(underlying_key)
    finally:
        client.close()

    assert observed == [
        ("GET", "/v2/option/contract", {"instrument_key": underlying_key})
    ]


def test_option_contract_http_400_uses_verified_instrument_master_fallback(
    session_factory, tmp_path
):
    future = contract("FUT")
    call = contract("CE", key="MCX_FO|MASTER-CE")
    put = contract("PE", key="MCX_FO|MASTER-PE")
    call["underlying_key"] = put["underlying_key"] = future["instrument_key"]
    requested = []

    class UnsupportedOptionContractClient(QuoteClient):
        def option_contracts(self, underlying_key):
            requested.append(underlying_key)
            raise UpstoxAPIError("HTTP 400", status_code=400)

    collector = _collector(UnsupportedOptionContractClient(), session_factory, tmp_path)
    collector._from_master = lambda kinds: [
        item for item in (call, put) if item["instrument_type"] in kinds
    ]

    selected = collector._discover_options(
        future, {"FUT": [future], "CE": [], "PE": []}, date.today()
    )

    assert requested == [future["instrument_key"]]
    assert [item["instrument_key"] for item in selected] == [
        call["instrument_key"], put["instrument_key"]
    ]


def test_market_closed_skips_quote_requests_and_persistence(session_factory, tmp_path):
    client = QuoteClient(status="NORMAL_CLOSE")
    collector = _collector(client, session_factory, tmp_path)
    result = collector.run_cycle(NOW)
    assert result["market_open"] is False
    assert [call[0] for call in client.calls] == ["market_status"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(CollectionQualityEvent)) == 0
    assert json.loads(collector.health_path.read_text(encoding="utf-8"))["status"] == "MARKET_CLOSED"


def test_direct_option_cycle_also_obeys_market_closed_gate(session_factory, tmp_path):
    client = QuoteClient(status="NORMAL_CLOSE")
    collector = _collector(client, session_factory, tmp_path)
    result = collector.collect_options_cycle(NOW)
    assert result["persisted"] == 0
    assert [call[0] for call in client.calls] == ["market_status"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OptionSnapshot)) == 0


def test_paper_mode_enforced_before_any_api_call(session_factory, tmp_path):
    client = QuoteClient()
    collector = _collector(client, session_factory, tmp_path, status="LIVE")
    with pytest.raises(RuntimeError, match="PAPER"):
        collector.prepare()
    assert client.calls == []


def test_prepare_discovers_contracts_and_verifies_quote_connectivity(session_factory, tmp_path):
    future = contract("FUT")
    call = contract("CE", key="MCX_FO|DISCOVERED-CE")
    put = contract("PE", key="MCX_FO|DISCOVERED-PE", expiry="2027-01-20")

    class DiscoveryClient(QuoteClient):
        def search_instruments(self, query, **params):
            kind = params["instrument_types"]
            return {
                "status": "success",
                "data": [future] if kind == "FUT" else [],
                "meta_data": {"page": {"page_number": 1, "total_pages": 1}},
            }

        def option_contracts(self, underlying_key):
            assert underlying_key == future["instrument_key"]
            return {"status": "success", "data": [call, put]}

    client = DiscoveryClient()
    client.quotes[future["instrument_key"]] = raw_quote(future)
    config = Settings(
        trading_mode="PAPER",
        upstox_access_token="token-not-logged",
        mcx_product="CRUDEOIL",
        mcx_market_open_time="00:00",
        mcx_market_close_time="23:59",
    )
    collector = ContinuousMarketDataCollector(
        client,
        Repository(session_factory),
        config=config,
        health_path=tmp_path / "live_collection_health.json",
        clock=lambda: 100.0,
    )
    collector.prepare()
    assert collector.future["instrument_key"] == future["instrument_key"]
    assert {item["expiry"] for item in collector.options} == {call["expiry"], put["expiry"]}
    assert collector.options[0]["instrument_type"] == "CE"
    assert collector.options[1]["instrument_type"] == "PE"
    assert [call[0] for call in client.calls] == ["market_status", "market_quotes"]
    assert json.loads(collector.health_path.read_text(encoding="utf-8"))["status"] == "READY"


def test_graceful_shutdown_and_read_only_client_surface(session_factory, tmp_path):
    client = QuoteClient()
    collector = _collector(client, session_factory, tmp_path)
    stop = threading.Event()
    stop.set()
    collector.run_forever(stop)
    assert json.loads(collector.health_path.read_text(encoding="utf-8"))["status"] == "STOPPED"
    assert not any("order" in call[0] for call in client.calls)


def test_options_keep_independent_expiry_and_exact_underlying_relation(
    session_factory, tmp_path
):
    future_expiry = (date.today() + timedelta(days=40)).isoformat()
    future = contract("FUT", expiry=future_expiry)
    options_underlying = contract(
        "FUT", key="MCX_FO|NEXT-CRUDE-FUT",
        expiry=(date.today() + timedelta(days=70)).isoformat(),
    )
    call = contract("CE", key="MCX_FO|CE", expiry=(date.today() + timedelta(days=20)).isoformat())
    put = contract("PE", key="MCX_FO|PE", expiry=(date.today() + timedelta(days=25)).isoformat())
    call["underlying_key"] = put["underlying_key"] = options_underlying["instrument_key"]
    unrelated = contract("CE", key="MCX_FO|OTHER", expiry=call["expiry"])
    unrelated["underlying_key"] = "MCX_FO|WRONG"

    class DiscoveryClient(QuoteClient):
        def option_contracts(self, underlying_key):
            assert underlying_key == future["instrument_key"]
            return {"status": "success", "data": []}

    client = DiscoveryClient()
    collector = _collector(client, session_factory, tmp_path)
    collector._from_master = lambda kinds: [
        item for item in (options_underlying, call, put, unrelated)
        if item["instrument_type"] in kinds
    ]
    selected = collector._discover_options(
        future, {"FUT": [future], "CE": [], "PE": []}, date.today(),
        related_futures=[future, options_underlying],
    )
    assert [item["instrument_key"] for item in selected] == [call["instrument_key"], put["instrument_key"]]
    assert selected[0]["expiry"] != selected[1]["expiry"]
