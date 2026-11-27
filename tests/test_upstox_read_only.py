from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from data_collector.upstox_read_only import QuoteValidationError, UpstoxReadOnlyService
from database.db_connection import Base
from database.models import MarketQuoteSnapshot, VerifiedInstrument
from database.repository import Repository
from execution.upstox_client import (
    UpstoxAPIError,
    UpstoxAuthenticationError,
    UpstoxPermissionError,
    UpstoxRateLimitError,
    UpstoxClient,
)

NOW = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
TOKEN = "test-access-token-do-not-log"


def instrument(kind="CE", key="MCX_FO|CRUDEOIL-CE"):
    return {
        "instrument_key": key,
        "trading_symbol": f"CRUDEOIL 7000 {kind}",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": kind,
        "expiry": "2026-11-19",
        "strike_price": 7000 if kind != "FUT" else 0,
        "lot_size": 100,
        "tick_size": 1,
        "underlying_key": "MCX_FO|CRUDEOIL-FUT",
        "name": "CRUDE OIL",
        "underlying_symbol": "CRUDEOIL",
    }


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_read_only_endpoints_send_get_and_never_expose_token():
    methods = []

    def handler(request):
        methods.append(request.method)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"status": "success", "data": {"broker": "UPSTOX", "user_name": "private"}})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"status": "success", "data": []})
        return httpx.Response(200, json={"status": "success", "data": {}})

    client = UpstoxClient(TOKEN, transport=httpx.MockTransport(handler))
    try:
        assert client.user_profile()["user_name"] == "private"
        client.search_instruments("CRUDE", instrument_types="CE")
        client.market_quotes(["MCX_FO|CRUDEOIL-CE"])
    finally:
        client.close()
    assert methods == ["GET", "GET", "GET"]


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, UpstoxAuthenticationError),
        (403, UpstoxPermissionError),
        (429, UpstoxRateLimitError),
        (503, UpstoxAPIError),
    ],
)
def test_http_errors_are_classified_without_response_body_or_token(status, error_type):
    client = UpstoxClient(
        TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"errors": [{"errorCode": "SAFE_CODE", "message": f"{TOKEN} private response"}]},
                headers={"x-request-id": "request-123"},
            )
        ),
    )
    try:
        with pytest.raises(error_type) as caught:
            client.user_profile()
        assert TOKEN not in str(caught.value)
        assert "private response" not in str(caught.value)
        assert caught.value.status_code == status
        if status == 403:
            assert caught.value.request_id == "request-123"
            assert caught.value.api_error_code == "SAFE_CODE"
    finally:
        client.close()


def test_market_quote_http_401_is_not_attributed_to_a_specific_cause():
    client = UpstoxClient(
        TOKEN,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="private body")),
    )
    try:
        with pytest.raises(UpstoxAuthenticationError) as caught:
            client.market_quotes(["MCX_FO|verified-key"])
        assert caught.value.status_code == 401
        assert "cause not determined" in str(caught.value)
        assert "private body" not in str(caught.value)
        assert caught.value.endpoint == "/v2/market-quote/quotes"
    finally:
        client.close()


def test_market_quote_http_403_preserves_only_safe_diagnostics():
    client = UpstoxClient(
        TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                json={"errors": [{"errorCode": "ACCESS_DENIED", "message": "private detail"}]},
                headers={"x-request-id": "quote-request-456"},
            )
        ),
    )
    try:
        with pytest.raises(UpstoxPermissionError) as caught:
            client.market_quotes(["MCX_FO|verified-key"])
        assert caught.value.status_code == 403
        assert caught.value.endpoint == "/v2/market-quote/quotes"
        assert caught.value.api_error_code == "ACCESS_DENIED"
        assert caught.value.request_id == "quote-request-456"
        assert TOKEN not in str(caught.value)
        assert "private detail" not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("transport_error", [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_and_network_errors_are_safe(transport_error):
    def handler(request):
        raise transport_error(TOKEN)

    client = UpstoxClient(TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstoxAPIError) as caught:
            client.user_profile()
        assert TOKEN not in str(caught.value)
    finally:
        client.close()


def test_instrument_search_filters_pages_and_persists_verified_metadata(session_factory):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["exchanges"] == "MCX"
        assert request.url.params["segments"] == "FO"
        assert request.url.params["instrument_types"] in {"FUT", "CE", "PE"}
        assert "expiry" not in request.url.params
        page = int(request.url.params["page_number"])
        kind = request.url.params["instrument_types"]
        contract = instrument(kind, f"MCX_FO|{kind}-{page}")
        if kind in {"CE", "PE"}:
            contract["expiry"] = "2026-12-16"
        rows = [contract] if page <= 2 else []
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": rows,
                "meta_data": {"page": {"page_number": page, "total_pages": 2}},
            },
        )

    client = UpstoxClient(TOKEN, transport=httpx.MockTransport(handler))
    service = UpstoxReadOnlyService(client, Repository(session_factory), max_quote_age_seconds=300)
    try:
        found = service.discover_crude_instruments()
    finally:
        client.close()
    assert {kind: len(rows) for kind, rows in found.items()} == {"FUT": 2, "CE": 2, "PE": 2}
    assert len(requests) == 6
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(VerifiedInstrument)) == 6


def test_unverified_instrument_metadata_is_rejected():
    broken = instrument("CE")
    broken.pop("lot_size")
    with pytest.raises(ValueError, match="lot_size"):
        UpstoxReadOnlyService._normalize_instrument(broken, "CE")


def test_official_master_epoch_expiry_is_normalized_without_guessing():
    row = instrument("FUT")
    row["expiry"] = 1797445799000
    result = UpstoxReadOnlyService._normalize_instrument(row, "FUT")
    assert result["expiry"] == "2026-12-16"


def test_non_mcx_or_wrong_instrument_type_is_not_discovered():
    non_mcx = {**instrument(), "exchange": "NSE"}
    assert UpstoxReadOnlyService._is_crude_row(non_mcx)
    with pytest.raises(ValueError, match="not MCX_FO"):
        UpstoxReadOnlyService._normalize_instrument(non_mcx, "CE")
    with pytest.raises(ValueError, match="does not match"):
        UpstoxReadOnlyService._normalize_instrument(instrument("PE"), "CE")
    unrelated = {
        **instrument(),
        "name": "GOLD",
        "trading_symbol": "GOLD FUT",
        "underlying_symbol": "GOLD",
    }
    assert not UpstoxReadOnlyService._is_crude_row(unrelated)


def quote_payload(key, *, price=51.25, timestamp=None):
    return {
        "instrument_token": key,
        "timestamp": timestamp or NOW.isoformat(),
        "last_trade_time": str(int((NOW - timedelta(seconds=1)).timestamp() * 1000)),
        "last_price": price,
        "depth": {
            "buy": [{"price": 51.2, "quantity": 20}],
            "sell": [{"price": 51.3, "quantity": 10}],
        },
        "volume": 100,
        "oi": 250,
    }


def test_fresh_valid_quotes_are_persisted_additively_and_duplicate_is_idempotent(session_factory):
    key = "MCX_FO|CRUDEOIL-CE"
    response = {
        "status": "success",
        "data": {"MCX_FO:CRUDEOIL-CE": quote_payload(key)},
    }

    class FakeClient:
        def market_quotes(self, keys):
            assert keys == [key]
            return response

    service = UpstoxReadOnlyService(FakeClient(), Repository(session_factory))
    contract = UpstoxReadOnlyService._normalize_instrument(instrument(), "CE")
    first = service.fetch_and_persist_quotes([contract], now=NOW)
    second = service.fetch_and_persist_quotes([contract], now=NOW)
    assert first["accepted"][0]["persisted"] is True
    assert second["accepted"][0]["persisted"] is False
    assert first["accepted"][0]["bid_price"] == 51.2
    assert first["accepted"][0]["ask_price"] == 51.3
    assert first["accepted"][0]["response_timestamp"] == NOW
    assert first["accepted"][0]["last_trade_time"] < NOW
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 1


def test_stale_and_incomplete_quotes_are_rejected_without_persistence(session_factory):
    key = "MCX_FO|CRUDEOIL-CE"
    stale = quote_payload(
        key,
        timestamp=NOW.isoformat(),
    )
    stale["last_trade_time"] = str(
        int((NOW - timedelta(minutes=10)).timestamp() * 1000)
    )
    incomplete = {"instrument_token": "MCX_FO|CRUDEOIL-PE", "timestamp": NOW.isoformat()}

    class FakeClient:
        def market_quotes(self, keys):
            data = {
                "crude-call": stale,
                "crude-put": incomplete,
            }
            return {"status": "success", "data": data}

    service = UpstoxReadOnlyService(FakeClient(), Repository(session_factory), max_quote_age_seconds=60)
    contracts = [
        UpstoxReadOnlyService._normalize_instrument(instrument("CE", key), "CE"),
        UpstoxReadOnlyService._normalize_instrument(
            instrument("PE", "MCX_FO|CRUDEOIL-PE"), "PE"
        ),
    ]
    result = service.fetch_and_persist_quotes(contracts, now=NOW)
    assert len(result["rejected"]) == 2
    assert "last trade is stale" in result["rejected"][0]["reason"]
    assert "last_price" in result["rejected"][1]["reason"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"instrument_token": "MCX_FO|CRUDEOIL-CE", "last_price": 0, "timestamp": NOW.isoformat()},
        {"instrument_token": "MCX_FO|CRUDEOIL-CE", "last_price": 1, "timestamp": "invalid"},
        {"instrument_token": "MCX_FO|OTHER", "last_price": 1, "timestamp": NOW.isoformat()},
        {
            "instrument_token": "MCX_FO|CRUDEOIL-CE",
            "last_price": 1,
            "timestamp": (NOW + timedelta(seconds=1)).isoformat(),
        },
    ],
)
def test_invalid_quote_data_is_rejected(payload):
    with pytest.raises(QuoteValidationError):
        UpstoxReadOnlyService.normalize_quote(
            instrument(), payload, now=NOW, max_age_seconds=60
        )


def test_profile_check_is_separate_from_market_data_quote_authentication(session_factory):
    class FakeClient:
        def user_profile(self):
            raise UpstoxAuthenticationError(
                "Profile HTTP 401", endpoint="/v2/user/profile", status_code=401
            )

        def market_quotes(self, keys):
            return {
                "status": "success",
                "data": {"contract": quote_payload(keys[0])},
            }

    service = UpstoxReadOnlyService(FakeClient(), Repository(session_factory))
    with pytest.raises(UpstoxAuthenticationError):
        service.check_profile_api()
    contract = UpstoxReadOnlyService._normalize_instrument(instrument(), "CE")
    result = service.fetch_and_persist_quotes([contract], now=NOW)
    assert len(result["accepted"]) == 1
