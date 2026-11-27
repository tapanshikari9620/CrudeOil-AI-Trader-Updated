from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from data_collector.contract_selection import CrudeContractSelector
from data_collector.historical_collector import HistoricalCollector
from data_collector.instrument_collector import InstrumentCollector
from data_collector.market_data_normalizer import normalize_candle
from data_collector.upstox_read_only import QuoteValidationError, UpstoxReadOnlyService
from database.db_connection import Base
from database.db_connection import initialize_database
from database.models import MarketCandle, MarketQuoteSnapshot, OptionSnapshot, RawInstrumentMetadata, VerifiedInstrument
from database.repository import Repository
from execution.market_stream import MarketStream, MarketStreamUnavailable
from execution.upstox_client import UpstoxAPIError, UpstoxClient, UpstoxRateLimitError
from scripts import run_market_hours_validation


TODAY = date(2026, 11, 16)
STAMP = datetime(2026, 11, 13, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def verified_row(kind: str, key: str, *, underlying="CRUDEOIL", expiry="2026-12-16", strike=7000):
    return {
        "instrument_key": key,
        "trading_symbol": f"{underlying} {strike} {kind}",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": kind,
        "expiry": expiry,
        "strike_price": 0 if kind == "FUT" else strike,
        "lot_size": 10,
        "tick_size": 5,
        "underlying_key": "MCX_FO|underlying",
        "underlying_symbol": underlying,
        "name": "CRUDE OIL",
    }


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_analytics_token_configuration_loaded_without_display(monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "configured-token")
    settings = Settings()
    assert settings.upstox_access_token == "configured-token"


def test_normalize_historical_candle_enforces_timezone_and_ohlcv():
    candle = normalize_candle(
        "MCX_FO|verified-fut",
        "1_day",
        ["2026-11-13T09:15:00+05:30", 70, 72, 69, 71, 100, 25],
        {
            "instrument_key": "MCX_FO|verified-fut",
            "segment": "MCX_FO",
            "instrument_type": "FUT",
        },
    )

    assert candle["timestamp"].tzinfo == timezone.utc
    assert candle["timestamp"].hour == 3
    assert candle["open_interest"] == 25

    with pytest.raises(ValueError, match="timezone"):
        normalize_candle(
            "MCX_FO|verified-fut",
            "1_day",
            ["2026-11-13T09:15:00", 70, 72, 69, 71, 100],
            {
                "instrument_key": "MCX_FO|verified-fut",
                "segment": "MCX_FO",
                "instrument_type": "FUT",
            },
        )


def test_historical_collector_accepts_ist_midnight_candle(session_factory):
    key = "MCX_FO|verified-fut"

    class Client:
        def historical_candles(self, *args):
            return {
                "status": "success",
                "data": {
                    "candles": [
                        [
                            "2026-10-26T00:00:00+05:30",
                            70, 72, 69, 71, 100, 25,
                        ]
                    ]
                },
            }

    metadata = {
        "instrument_key": key,
        "segment": "MCX_FO",
        "instrument_type": "FUT",
    }

    collector = HistoricalCollector(
        Client(),
        Repository(session_factory),
        metadata,
    )

    result = collector.collect(
        key, "days", 1, "2026-10-26", "2026-10-26"
    )

    assert result["inserted"] == 1
    assert result["invalid"] == 0

def test_historical_collector_rejects_sunday_intraday_candle(session_factory):
    key = "MCX_FO|verified-fut"

    class Client:
        def historical_candles(self, *args):
            return {
                "status": "success",
                "data": {
                    "candles": [
                        [
                            "2026-11-08T14:20:00+05:30",
                            70, 72, 69, 71, 100, 25,
                        ],
                        [
                            "2026-11-09T09:15:00+05:30",
                            71, 73, 70, 72, 120, 26,
                        ],
                    ]
                },
            }

    metadata = {
        "instrument_key": key,
        "segment": "MCX_FO",
        "instrument_type": "FUT",
    }

    collector = HistoricalCollector(
        Client(),
        Repository(session_factory),
        metadata,
    )

    result = collector.collect(
        key, "minutes", 5, "2026-11-09", "2026-11-08"
    )

    assert result["inserted"] == 1
    assert result["invalid"] == 1

def test_historical_collector_persists_idempotently_and_rejects_bad_rows(session_factory):
    key = "MCX_FO|verified-fut"

    class Client:
        def historical_candles(self, *args):
            return {"status": "success", "data": {"candles": [
                ["2026-11-13T09:15:00+05:30", 70, 72, 69, 71, 100, 25],
                ["2026-11-13T09:15:00+05:30", 70, 72, 69, 71, 100, 25],
                ["not-a-time", 70, 72, 69, 71, 100],
            ]}}

    metadata = {"instrument_key": key, "segment": "MCX_FO", "instrument_type": "FUT"}
    collector = HistoricalCollector(Client(), Repository(session_factory), metadata)
    first = collector.collect(key, "days", 1, "2026-11-16", "2026-11-10")
    second = collector.collect(key, "days", 1, "2026-11-16", "2026-11-10")
    assert (first["inserted"], first["duplicate_response_rows"], first["invalid"]) == (1, 1, 1)
    assert second["duplicates"] == 1
    with session_factory() as session:
        rows = list(session.scalars(select(MarketCandle)))
        assert len(rows) == 1
        assert rows[0].instrument_type == "FUT"
        assert rows[0].open_interest == 25


def test_historical_collector_rejects_option_data_and_out_of_range_request(session_factory):
    class Client:
        def historical_candles(self, *args):
            pytest.fail("unsupported request must not reach Upstox")

    metadata = {"instrument_key": "MCX_FO|option", "segment": "MCX_FO", "instrument_type": "CE"}
    collector = HistoricalCollector(Client(), Repository(session_factory), metadata)
    with pytest.raises(ValueError, match="FUT"):
        collector.collect("MCX_FO|option", "days", 1, "2026-11-16")
    with pytest.raises(ValueError, match="one month"):
        HistoricalCollector(Client(), instrument_metadata={
            "instrument_key": "MCX_FO|fut", "segment": "MCX_FO", "instrument_type": "FUT"
        }).collect("MCX_FO|fut", "minutes", 1, "2026-11-16", "2026-10-01")


def test_raw_and_normalized_instrument_metadata_are_stored_separately(session_factory):
    repo = Repository(session_factory)
    raw = verified_row("FUT", "MCX_FO|fut")
    normalized = UpstoxReadOnlyService._normalize_instrument(raw, "FUT", metadata_source="test_master")
    repo.save_verified_instrument(normalized)
    with session_factory() as session:
        instrument = session.get(VerifiedInstrument, normalized["instrument_key"])
        raw_rows = list(session.scalars(select(RawInstrumentMetadata)))
        assert instrument.underlying_symbol == "CRUDEOIL"
        assert instrument.contract_status == "ACTIVE"
        assert len(raw_rows) == 1
        assert '"instrument_type":"FUT"' in raw_rows[0].raw_json
        assert raw_rows[0].source == "test_master"


def test_contract_selector_chooses_active_main_and_paired_options_not_expired():
    raw = [
        verified_row("FUT", "MCX_FO|expired", expiry="2026-11-15"),
        verified_row("FUT", "MCX_FO|mini", underlying="CRUDEOILM", expiry="2026-11-20"),
        verified_row("FUT", "MCX_FO|main", expiry="2026-11-20"),
        verified_row("CE", "MCX_FO|ce-7000", strike=7000),
        verified_row("PE", "MCX_FO|pe-7000", strike=7000),
        verified_row("CE", "MCX_FO|expired-ce", expiry="2026-11-15", strike=7000),
        verified_row("PE", "MCX_FO|other-underlying", underlying="CRUDEOILM", strike=7000),
    ]
    normalized = [
        UpstoxReadOnlyService._normalize_instrument(row, row["instrument_type"])
        for row in raw
    ]

    # Keep status consistent with this test's simulated date.
    for item in normalized:
        expiry = date.fromisoformat(str(item["expiry"])[:10])
        item["contract_status"] = "ACTIVE" if expiry >= TODAY else "EXPIRED"

    selected = CrudeContractSelector.select_from(
        normalized, today=TODAY, target_price=7000
    )
    assert selected["future"]["instrument_key"] == "MCX_FO|main"
    assert selected["call"]["instrument_key"] == "MCX_FO|ce-7000"
    assert selected["put"]["instrument_key"] == "MCX_FO|pe-7000"

def test_contract_selector_explicitly_selects_mini_and_matching_options():
    raw = [
        verified_row("FUT", "MCX_FO|regular-fut", underlying="CRUDEOIL"),
        verified_row("FUT", "MCX_FO|mini-fut", underlying="CRUDEOILM"),
        verified_row("CE", "MCX_FO|regular-ce", underlying="CRUDEOIL"),
        verified_row("PE", "MCX_FO|regular-pe", underlying="CRUDEOIL"),
        verified_row("CE", "MCX_FO|mini-ce", underlying="CRUDEOILM"),
        verified_row("PE", "MCX_FO|mini-pe", underlying="CRUDEOILM"),
    ]

    normalized = [
        UpstoxReadOnlyService._normalize_instrument(
            row, row["instrument_type"]
        )
        for row in raw
    ]

    for item in normalized:
        item["contract_status"] = "ACTIVE"

    selected = CrudeContractSelector.select_from(
        normalized,
        today=TODAY,
        target_price=7000,
        product="CRUDEOILM",
    )

    assert selected["future"]["instrument_key"] == "MCX_FO|mini-fut"
    assert selected["call"]["instrument_key"] == "MCX_FO|mini-ce"
    assert selected["put"]["instrument_key"] == "MCX_FO|mini-pe"

    assert selected["future"]["underlying_symbol"] == "CRUDEOILM"
    assert selected["call"]["underlying_symbol"] == "CRUDEOILM"
    assert selected["put"]["underlying_symbol"] == "CRUDEOILM"


def test_contract_selector_explicitly_selects_regular_crude():
    raw = [
        verified_row("FUT", "MCX_FO|regular-fut", underlying="CRUDEOIL"),
        verified_row("FUT", "MCX_FO|mini-fut", underlying="CRUDEOILM"),
        verified_row("CE", "MCX_FO|regular-ce", underlying="CRUDEOIL"),
        verified_row("PE", "MCX_FO|regular-pe", underlying="CRUDEOIL"),
        verified_row("CE", "MCX_FO|mini-ce", underlying="CRUDEOILM"),
        verified_row("PE", "MCX_FO|mini-pe", underlying="CRUDEOILM"),
    ]

    normalized = [
        UpstoxReadOnlyService._normalize_instrument(
            row, row["instrument_type"]
        )
        for row in raw
    ]

    for item in normalized:
        item["contract_status"] = "ACTIVE"

    selected = CrudeContractSelector.select_from(
        normalized,
        today=TODAY,
        target_price=7000,
        product="CRUDEOIL",
    )

    assert selected["future"]["instrument_key"] == "MCX_FO|regular-fut"
    assert selected["call"]["instrument_key"] == "MCX_FO|regular-ce"
    assert selected["put"]["instrument_key"] == "MCX_FO|regular-pe"


def test_contract_selector_does_not_fallback_from_mini_to_regular():
    raw = [
        verified_row("FUT", "MCX_FO|regular-fut", underlying="CRUDEOIL"),
        verified_row("CE", "MCX_FO|regular-ce", underlying="CRUDEOIL"),
        verified_row("PE", "MCX_FO|regular-pe", underlying="CRUDEOIL"),
    ]

    normalized = [
        UpstoxReadOnlyService._normalize_instrument(
            row, row["instrument_type"]
        )
        for row in raw
    ]

    for item in normalized:
        item["contract_status"] = "ACTIVE"

    with pytest.raises(ValueError, match="CRUDEOILM"):
        CrudeContractSelector.select_from(
            normalized,
            today=TODAY,
            product="CRUDEOILM",
        )


def test_zero_ltp_is_rejected_with_quote_evidence_not_persisted(session_factory):
    contract_row = verified_row("FUT", "MCX_FO|fut")
    contract = UpstoxReadOnlyService._normalize_instrument(contract_row, "FUT")
    quote = {
        "instrument_token": contract["instrument_key"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "last_trade_time": "0",
        "last_price": 0,
        "ohlc": {"open": 0, "high": 0, "low": 0, "close": 0},
        "volume": 0,
        "oi": 0,
        "depth": {"buy": [{"price": 0, "quantity": 0}], "sell": [{"price": 0, "quantity": 0}]},
    }

    class Client:
        def market_quotes(self, keys):
            return {"status": "success", "data": {"response-key": quote}}

    result = UpstoxReadOnlyService(Client(), Repository(session_factory)).fetch_and_persist_quotes([contract])
    assert result["accepted"] == []
    evidence = result["rejected"][0]["quote_evidence"]
    assert evidence["last_price"] == 0
    assert evidence["ohlc"]["close"] == 0
    assert evidence["last_trade_time"] == "0"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 0


def test_option_quote_persistence_writes_market_and_option_snapshots_atomically(session_factory):
    contract = UpstoxReadOnlyService._normalize_instrument(
        verified_row("CE", "MCX_FO|ce"), "CE"
    )
    quote = {
        "instrument_key": contract["instrument_key"],
        "timestamp": STAMP.astimezone(timezone.utc),
        "last_price": 8.5,
        "bid_price": 8.4,
        "ask_price": 8.6,
        "volume": 20,
        "open_interest": 100,
        "source": "upstox",
    }
    repo = Repository(session_factory)
    assert repo.add_market_quote(quote, instrument=contract)
    assert not repo.add_market_quote(quote, instrument=contract)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketQuoteSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(OptionSnapshot)) == 1


def test_market_stream_reports_missing_official_decoder_and_is_bounded():
    readiness = MarketStream.readiness(decoder=None)
    assert not readiness["decoder_callable"]
    stream = MarketStream("token", decoder=None, on_message=lambda _: None, max_reconnects=0)
    try:
        with pytest.raises(MarketStreamUnavailable, match="decoder_callable"):
            import asyncio
            asyncio.run(stream.run(["MCX_FO|verified-key"]))
    finally:
        stream.close()


def test_market_status_and_historical_endpoints_are_read_only_gets():
    observed = []

    def handler(request):
        observed.append((request.method, request.url.path))
        if "/market/status/" in request.url.path:
            return httpx.Response(200, json={"status": "success", "data": {"status": "NORMAL_CLOSE"}})
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})

    client = UpstoxClient("token", transport=httpx.MockTransport(handler))
    try:
        assert client.market_status("MCX")["data"]["status"] == "NORMAL_CLOSE"
        client.historical_candles("MCX_FO|verified", "days", 1, "2026-11-16", "2026-11-10")
    finally:
        client.close()
    assert observed == [
        ("GET", "/v2/market/status/MCX"),
        ("GET", "/v3/historical-candle/MCX_FO|verified/days/1/2026-11-16/2026-11-10"),
    ]


@pytest.mark.parametrize(
    ("market_status", "evidence", "expected"),
    [
        ("NORMAL_CLOSE", {"last_price": 0, "volume": 0, "last_trade_time": "0"}, "MARKET_CLOSED"),
        ("NORMAL_OPEN", {"last_price": 0, "volume": 0, "last_trade_time": "0"}, "NO_TRADE"),
        ("UNKNOWN", {"last_price": 0, "volume": 12, "last_trade_time": "1760000000000"}, "INVALID_QUOTE"),
    ],
)
def test_market_hours_quote_classification_uses_status_and_quote_evidence(
    market_status, evidence, expected
):
    contract = verified_row("FUT", "MCX_FO|fut")
    quote_result = {
        "accepted": [],
        "rejected": [{
            "instrument_key": contract["instrument_key"],
            "quote_evidence": evidence,
        }],
    }
    assert run_market_hours_validation.classify_quote(contract, quote_result, market_status) == expected


@pytest.mark.parametrize("product", ["CRUDEOIL", "CRUDEOILM"])
def test_market_hours_runner_uses_only_mocked_read_only_data(
    monkeypatch,
    product,
):
    monkeypatch.setattr(
        run_market_hours_validation.settings,
        "mcx_product",
        product,
    )

    now = datetime.now(timezone.utc)
    expiry = (
        now.astimezone(ZoneInfo("Asia/Kolkata")).date()
        + timedelta(days=30)
    ).isoformat()

    contracts = [
        UpstoxReadOnlyService._normalize_instrument(
            verified_row(
                "FUT",
                "MCX_FO|fut",
                underlying=product,
                expiry=expiry,
            ),
            "FUT",
        ),
        UpstoxReadOnlyService._normalize_instrument(
            verified_row(
                "CE",
                "MCX_FO|ce",
                underlying=product,
                expiry=expiry,
            ),
            "CE",
        ),
        UpstoxReadOnlyService._normalize_instrument(
            verified_row(
                "PE",
                "MCX_FO|pe",
                underlying=product,
                expiry=expiry,
            ),
            "PE",
        ),
    ]

    class MemoryRepository:
        def __init__(self):
            self.instruments = contracts
            self.quotes = []
            self.candles = []

        def get_verified_instruments(self):
            return self.instruments

        def add_market_quote(self, quote, instrument=None):
            self.quotes.append(quote)
            return True

        def add_candle(self, candle):
            self.candles.append(candle)
            return True

    class MockClient:
        def market_status(self, exchange):
            assert exchange == "MCX"
            return {"status": "success", "data": [{"status": "NORMAL_CLOSE"}]}

        def market_quotes(self, keys):
            return {
                "status": "success",
                "data": {
                    key: {
                        "instrument_token": key,
                        "timestamp": now.isoformat(),
                        "last_trade_time": "0",
                        "last_price": 0,
                        "ohlc": {"open": 0, "high": 0, "low": 0, "close": 0},
                        "volume": 0,
                        "oi": 0,
                        "depth": {"buy": [], "sell": []},
                    }
                    for key in keys
                },
            }

        def historical_candles(self, key, unit, interval, to_date, from_date):
            return {
                "status": "success",
                "data": {"candles": [[
                    f"{to_date}T09:00:00+05:30", 70, 72, 69, 71, 100, 25
                ]]},
            }

    monkeypatch.setattr(run_market_hours_validation, initialize_database.__name__, lambda: None)
    repo = MemoryRepository()
    report = run_market_hours_validation.run_validation(repository=repo, client=MockClient())

    assert report["instrument_discovery"] == "PASS"
    assert report["market_quote_authentication"].startswith("CONFIRMED")
    assert {item["classification"] for item in report["quotes"]} == {"MARKET_CLOSED"}
    assert report["historical_data"] == "PASS"
    assert len(repo.candles) == 1
    assert repo.quotes == []


def test_http_429_and_server_errors_are_bounded_by_configured_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"status": "error"})

    client = UpstoxClient("token", transport=httpx.MockTransport(handler), retry_count=2,
                          retry_backoff_seconds=0)
    try:
        with pytest.raises(UpstoxRateLimitError):
            client.market_status("MCX")
    finally:
        client.close()
    assert len(calls) == 3


def crude_option_instrument(
        instrument_type="PE",
        underlying_symbol="CRUDEOIL",
        lot_size=100,
        tick_size=10.0,
):
    return {
        "instrument_key": f"MCX_FO|test-{underlying_symbol}-{instrument_type}",
        "trading_symbol": f"{underlying_symbol} 7500 {instrument_type} 16 DEC 26",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": instrument_type,
        "expiry": "2026-12-16",
        "strike_price": 7500,
        "lot_size": lot_size,
        "tick_size": tick_size,
        "underlying_symbol": underlying_symbol,
        "underlying_key": "MCX_FO|test-underlying",
        "contract_status": "ACTIVE",
        "raw_metadata": {"tick_size": tick_size},
        "metadata_source": "official_instrument_master",
    }

@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_repository_accepts_verified_crude_option_tick_correction(
        session_factory, option_type
):
    repo = Repository(session_factory)
    instrument = crude_option_instrument(instrument_type=option_type)

    # Simulate an existing record with the incorrect stored tick size.
    repo.save_verified_instrument(instrument)

    corrected = dict(instrument)
    corrected["tick_size"] = 0.10

    # The narrowly verified correction should be accepted.
    repo.save_verified_instrument(corrected)

    with session_factory() as session:
        stored = session.get(
            VerifiedInstrument,
            instrument["instrument_key"],
        )
        raw_rows = list(session.scalars(select(RawInstrumentMetadata)))

        assert stored.tick_size == pytest.approx(0.10)
        assert len(raw_rows) == 1
        assert '"tick_size":10.0' in raw_rows[0].raw_json

def test_repository_rejects_tick_correction_for_crude_future(session_factory):
    repo = Repository(session_factory)

    instrument = crude_option_instrument(
        instrument_type="FUT",
        lot_size=100,
    )
    instrument["strike_price"] = 0

    repo.save_verified_instrument(instrument)

    corrected = dict(instrument)
    corrected["tick_size"] = 0.10

    with pytest.raises(ValueError, match="metadata changed"):
        repo.save_verified_instrument(corrected)

@pytest.mark.parametrize(
    "underlying_symbol, lot_size, trading_symbol",
    [
        ("CRUDEOIL", 100, "CRUDEOILFUT 16 DEC 26"),
        ("CRUDEOILM", 10, "CRUDEOILMFUT 16 DEC 26"),
    ],
)
def test_repository_accepts_verified_crude_future_tick_correction(
        session_factory,
        underlying_symbol,
        lot_size,
        trading_symbol,
):
    repo = Repository(session_factory)

    instrument = crude_option_instrument(
        instrument_type="FUT",
        underlying_symbol=underlying_symbol,
        lot_size=lot_size,
        tick_size=100.0,
    )
    instrument["trading_symbol"] = trading_symbol
    instrument["strike_price"] = 0
    instrument["raw_metadata"] = {"tick_size": 100.0}

    # Save the original instrument metadata.
    repo.save_verified_instrument(instrument)

    # Apply the verified normalized tick-size correction.
    corrected = dict(instrument)
    corrected["tick_size"] = 1.0
    repo.save_verified_instrument(corrected)

    # Confirm normalized tick size changed but raw metadata did not.
    with session_factory() as session:
        stored = session.get(
            VerifiedInstrument,
            instrument["instrument_key"],
        )
        raw_rows = list(
            session.scalars(
                select(RawInstrumentMetadata).where(
                    RawInstrumentMetadata.instrument_key
                    == instrument["instrument_key"]
                )
            )
        )

        assert stored is not None
        assert stored.tick_size == pytest.approx(1.0)
        assert len(raw_rows) == 1
        assert '"tick_size":100.0' in raw_rows[0].raw_json


@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_repository_accepts_verified_mini_crude_option_tick_correction(
        session_factory, option_type,
):
    repo = Repository(session_factory)

    instrument = crude_option_instrument(
        instrument_type=option_type,
        underlying_symbol="CRUDEOILM",
        lot_size=10,
        tick_size=5.0,
    )
    instrument["raw_metadata"] = {"tick_size": 5.0}

    repo.save_verified_instrument(instrument)

    corrected = dict(instrument)
    corrected["tick_size"] = 0.05

    repo.save_verified_instrument(corrected)

    with session_factory() as session:
        stored = session.get(
            VerifiedInstrument,
            instrument["instrument_key"],
        )
        raw_rows = list(
            session.scalars(
                select(RawInstrumentMetadata).where(
                    RawInstrumentMetadata.instrument_key
                    == instrument["instrument_key"]
                )
            )
        )

        assert stored.tick_size == pytest.approx(0.05)
        assert len(raw_rows) == 1
        assert '"tick_size":5.0' in raw_rows[0].raw_json


def test_repository_rejects_tick_correction_for_mini_crude_future(
        session_factory,
):
    repo = Repository(session_factory)

    instrument = crude_option_instrument(
        instrument_type="FUT",
        underlying_symbol="CRUDEOILM",
        lot_size=10,
        tick_size=1.0,
    )
    instrument["strike_price"] = 0
    instrument["raw_metadata"] = {"tick_size": 1.0}

    repo.save_verified_instrument(instrument)

    corrected = dict(instrument)
    corrected["tick_size"] = 0.05

    with pytest.raises(ValueError, match="metadata changed"):
        repo.save_verified_instrument(corrected)

def test_contract_selector_rejects_unknown_future_contract():
    raw = [
        verified_row("FUT", "MCX_FO|unknown-future"),
        verified_row("CE", "MCX_FO|ce-7000"),
        verified_row("PE", "MCX_FO|pe-7000"),
    ]

    normalized = [
        UpstoxReadOnlyService._normalize_instrument(
            row, row["instrument_type"]
        )
        for row in raw
    ]

    normalized[0]["contract_status"] = "UNKNOWN"

    with pytest.raises(ValueError, match="No unexpired verified"):
        CrudeContractSelector.select_from(normalized, today=TODAY)


def test_contract_selector_rejects_unknown_options():
    raw = [
        verified_row("FUT", "MCX_FO|active-future"),
        verified_row("CE", "MCX_FO|unknown-ce"),
        verified_row("PE", "MCX_FO|unknown-pe"),
    ]

    normalized = [
        UpstoxReadOnlyService._normalize_instrument(
            row, row["instrument_type"]
        )
        for row in raw
    ]

    normalized[1]["contract_status"] = "UNKNOWN"
    normalized[2]["contract_status"] = "UNKNOWN"

    selected = CrudeContractSelector.select_from(
        normalized, today=TODAY
    )

    assert selected["future"]["instrument_key"] == "MCX_FO|active-future"
    assert selected["call"] is None
    assert selected["put"] is None
