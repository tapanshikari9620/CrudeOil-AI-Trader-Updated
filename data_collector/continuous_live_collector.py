from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config.settings import Settings, settings
from data_collector.contract_selection import CrudeContractSelector
from data_collector.instrument_collector import InstrumentCollector
from data_collector.upstox_read_only import QuoteValidationError, UpstoxReadOnlyService
from database.repository import Repository
from execution.upstox_client import UpstoxAPIError

logger = logging.getLogger("crude_oil_trader.collector")
MARKET_TZ = ZoneInfo("Asia/Kolkata")
OPEN_STATUSES = {"NORMAL_OPEN", "OPEN"}
CLOSED_STATUSES = {"NORMAL_CLOSE", "CLOSED", "CLOSED_FOR_THE_DAY", "MARKET_CLOSED"}
MAX_FUTURE_TIMESTAMP_SKEW_SECONDS = 5


def _status_values(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status.upper() not in {"SUCCESS", "ERROR"}:
            found.add(status.upper())
        for value in payload.values():
            found.update(_status_values(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_status_values(value))
    return found


def _parse_market_datetime(value: Any, label: str) -> datetime:
    if value in (None, ""):
        raise QuoteValidationError(f"Quote {label} is missing")
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError
            result = datetime.fromtimestamp(number / 1000, tz=timezone.utc)
        else:
            text = str(value).strip()
            if text.isdigit():
                number = float(text)
                if not math.isfinite(number) or number <= 0:
                    raise ValueError
                result = datetime.fromtimestamp(number / 1000, tz=timezone.utc)
            else:
                result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise QuoteValidationError(f"Quote {label} is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise QuoteValidationError(f"Quote {label} must include a timezone")
    return result.astimezone(timezone.utc)


def _optional_number(value: Any, label: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise QuoteValidationError(f"Quote {label} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuoteValidationError(f"Quote {label} is invalid") from exc
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        qualifier = "positive" if positive else "non-negative"
        raise QuoteValidationError(f"Quote {label} must be finite and {qualifier}")
    return number


def _optional_ohlc_value(value: Any, label: str) -> float | None:
    number = _optional_number(value, label)
    return None if number == 0 else number


def _best_depth(levels: Any, side: str) -> tuple[float | None, float | None]:
    if levels is None:
        return None, None
    if not isinstance(levels, list):
        raise QuoteValidationError(f"Quote depth {side} must be a list")
    for level in levels:
        if not isinstance(level, dict):
            raise QuoteValidationError(f"Quote depth {side} contains a malformed level")
        price_raw, quantity_raw = level.get("price"), level.get("quantity")
        if price_raw is None and quantity_raw is None:
            continue
        if isinstance(price_raw, bool) or isinstance(quantity_raw, bool):
            raise QuoteValidationError(f"Quote depth {side} contains invalid price or quantity")
        try:
            price = float(price_raw)
            quantity = float(quantity_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise QuoteValidationError(f"Quote depth {side} contains invalid price or quantity") from exc
        if not math.isfinite(price) or not math.isfinite(quantity) or quantity < 0:
            raise QuoteValidationError(f"Quote depth {side} contains invalid price or quantity")
        if price == 0 and quantity == 0:
            continue
        if price <= 0:
            raise QuoteValidationError(f"Quote depth {side} price must be positive")
        return price, quantity
    return None, None


def normalize_live_quote(
    instrument: dict[str, Any],
    quote: dict[str, Any],
    *,
    snapshot_at: datetime,
    freshness_seconds: float,
) -> tuple[dict[str, Any], str]:
    """Validate an Upstox Full Market Quote without treating response time as trade time."""
    required_metadata = (
        "instrument_key", "trading_symbol", "instrument_type", "expiry",
        "exchange", "segment", "underlying_symbol",
    )
    if not isinstance(instrument, dict) or any(
        not isinstance(instrument.get(key), str) or not instrument[key].strip()
        for key in required_metadata
    ):
        raise QuoteValidationError("Verified instrument metadata is incomplete")
    if instrument["exchange"] != "MCX" or instrument["segment"] != "MCX_FO":
        raise QuoteValidationError("Instrument metadata is not an MCX_FO contract")
    if instrument["instrument_type"] not in {"FUT", "CE", "PE"}:
        raise QuoteValidationError("Instrument metadata has an unsupported type")
    try:
        expiry = date.fromisoformat(str(instrument["expiry"]))
    except ValueError as exc:
        raise QuoteValidationError("Instrument metadata expiry is invalid") from exc
    if expiry < snapshot_at.astimezone(MARKET_TZ).date():
        raise QuoteValidationError("Instrument contract has expired")
    if instrument.get("contract_status") != "ACTIVE":
        raise QuoteValidationError("Instrument contract is not verified active")
    if instrument["instrument_type"] in {"CE", "PE"}:
        if not instrument.get("underlying_key"):
            raise QuoteValidationError("Option metadata has no verified underlying instrument key")
        strike = _optional_number(instrument.get("strike_price"), "strike_price", positive=True)
        if strike is None:
            raise QuoteValidationError("Option metadata strike price is missing")
    if not isinstance(quote, dict):
        raise QuoteValidationError("Quote payload must be an object")
    if quote.get("instrument_token") != instrument["instrument_key"]:
        raise QuoteValidationError("Quote instrument token does not match verified metadata")

    ltp = _optional_number(quote.get("last_price"), "last_price", positive=True)
    if ltp is None:
        raise QuoteValidationError("Quote last_price is missing")
    response_timestamp = _parse_market_datetime(quote.get("timestamp"), "response timestamp")
    last_trade_time = _parse_market_datetime(quote.get("last_trade_time"), "last-trade timestamp")
    current_time = snapshot_at.astimezone(timezone.utc)
    if (response_timestamp - current_time).total_seconds() > MAX_FUTURE_TIMESTAMP_SKEW_SECONDS:
        raise QuoteValidationError("Quote response timestamp is in the future")

    if last_trade_time > current_time:
        raise QuoteValidationError("Quote last-trade timestamp is in the future")

    depth = quote.get("depth")
    if depth is not None and not isinstance(depth, dict):
        raise QuoteValidationError("Quote depth must be an object")
    depth = depth or {}
    bid, bid_quantity = _best_depth(depth.get("buy"), "buy")
    ask, ask_quantity = _best_depth(depth.get("sell"), "sell")
    if bid is not None and ask is not None and bid > ask:
        raise QuoteValidationError("Quote has a crossed bid/ask market")
    volume = _optional_number(quote.get("volume"), "volume")
    open_interest = _optional_number(quote.get("oi"), "open_interest")

    ohlc = quote.get("ohlc")
    if ohlc is not None and not isinstance(ohlc, dict):
        raise QuoteValidationError("Quote OHLC must be an object")
    ohlc = ohlc or {}
    open_price = _optional_ohlc_value(ohlc.get("open"), "ohlc.open")
    high = _optional_ohlc_value(ohlc.get("high"), "ohlc.high")
    low = _optional_ohlc_value(ohlc.get("low"), "ohlc.low")
    previous_close = _optional_ohlc_value(ohlc.get("close"), "ohlc.close")
    if high is not None and low is not None and high < low:
        raise QuoteValidationError("Quote OHLC high is below low")
    if high is not None and any(value is not None and high < value for value in (open_price, low)):
        raise QuoteValidationError("Quote OHLC values are inconsistent")
    if low is not None and any(value is not None and low > value for value in (open_price, high)):
        raise QuoteValidationError("Quote OHLC values are inconsistent")

    age = (current_time - last_trade_time).total_seconds()
    status = "VALID_STALE" if age > freshness_seconds else "VALID_FRESH"
    result = {
        "instrument_key": instrument["instrument_key"],
        "trading_symbol": instrument["trading_symbol"],
        "expiry": str(instrument["expiry"]),
        "option_type": instrument["instrument_type"] if instrument["instrument_type"] in {"CE", "PE"} else None,
        "underlying_instrument_key": instrument.get("underlying_key"),
        "timestamp": current_time,
        "response_timestamp": response_timestamp,
        "last_trade_time": last_trade_time,
        "quality_status": status,
        "last_price": ltp,
        "bid_price": bid,
        "bid_quantity": bid_quantity,
        "ask_price": ask,
        "ask_quantity": ask_quantity,
        "open": open_price,
        "high": high,
        "low": low,
        "close": previous_close,
        "previous_close": previous_close,
        "volume": volume,
        "open_interest": open_interest,
        "source": "upstox",
    }
    return result, status


class ContinuousMarketDataCollector:
    """Continuously polls Upstox MCX futures and options using read-only REST calls."""

    def __init__(
        self,
        client: Any,
        repository: Repository,
        *,
        config: Settings = settings,
        health_path: Path | None = None,
        instrument_collector: InstrumentCollector | None = None,
        clock=time.monotonic,
        utc_clock=lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.repository = repository
        self.config = config
        self.service = UpstoxReadOnlyService(
            client,
            repository,
            max_quote_age_seconds=config.live_quote_freshness_seconds,
        )
        self.health_path = health_path or config.data_dir / "live_collection_health.json"
        self.instrument_collector = instrument_collector or InstrumentCollector()
        self.clock = clock
        self.utc_clock = utc_clock
        self.future: dict[str, Any] | None = None
        self.options: list[dict[str, Any]] = []
        self._master_rows: list[dict[str, Any]] | None = None
        self._master_loaded_on: date | None = None
        self._last_refresh = 0.0
        self._last_market_status: tuple[float, bool] | None = None
        self._last_success: str | None = None
        self._last_health: dict[str, Any] = {}
        self._cycle_counts: dict[str, int] = {}
        self._logged_rejections: dict[str, str] = {}

    def prepare(self) -> None:
        if self.config.trading_mode != "PAPER":
            raise RuntimeError("Live market-data collection requires TRADING_MODE=PAPER")
        if not self.config.upstox_access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is not configured")
        self._refresh_contracts()
        if self.future is None:
            raise RuntimeError("No verified active MCX crude futures contract was discovered")
        status_payload = self.client.market_status("MCX")
        if not isinstance(status_payload, dict) or status_payload.get("status") != "success":
            raise ValueError("Upstox MCX market-status response has an unexpected schema")
        quote_payload = self.client.market_quotes([self.future["instrument_key"]])
        quote_data = self._validate_quotes_response(quote_payload)
        self._find_quote(quote_data, self.future["instrument_key"])
        logger.info(
            "Live collector ready: future=%s expiry=%s calls=%d puts=%d; market-data GET succeeded",
            self.future["trading_symbol"], self.future["expiry"],
            sum(row["instrument_type"] == "CE" for row in self.options),
            sum(row["instrument_type"] == "PE" for row in self.options),
        )
        self._write_health("READY", market_status=next(iter(_status_values(status_payload)), "UNKNOWN"))

    @staticmethod
    def _active(instrument: dict[str, Any], today: date) -> bool:
        try:
            expiry = date.fromisoformat(str(instrument["expiry"]))
        except (KeyError, TypeError, ValueError):
            return False
        return expiry >= today and instrument.get("contract_status") == "ACTIVE"

    def _from_master(self, kinds: set[str]) -> list[dict[str, Any]]:
        today = datetime.now(MARKET_TZ).date()
        if self._master_rows is None or self._master_loaded_on != today:
            self._master_rows = self.instrument_collector.fetch()
            self._master_loaded_on = today
        rows = self.instrument_collector.crude_contracts(
            self._master_rows,
            product=self.config.mcx_product,
        )
        result = []
        for row in rows:
            kind = row.get("instrument_type")
            if kind not in kinds or row.get("exchange", "MCX") != "MCX":
                continue
            contract = self.service._normalize_instrument(
                row, kind, metadata_source="official_instrument_master"
            )
            result.append(contract)
        return result


    def _discover_instruments(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        product = self.config.mcx_product.upper().strip()

        if product not in {"CRUDEOIL", "CRUDEOILM"}:
            raise ValueError(
                f"Unsupported MCX product configured: {product}"
            )

        discovered: dict[str, list[dict[str, Any]]] = {
            kind: [] for kind in ("FUT", "CE", "PE")
        }

        search_error: Exception | None = None

        def matches_product(item: Any) -> bool:
            underlying = str(
                item.get("underlying_symbol", "")
                if isinstance(item, dict)
                else getattr(item, "underlying_symbol", "")
            ).upper().strip()

            return underlying == product

        try:
            discovered = self.service.discover_crude_instruments()
        except (UpstoxAPIError, ValueError, OSError, httpx.HTTPError) as exc:
            search_error = exc
            logger.warning(
                "Instrument search failed for %s; trying verified metadata fallback: %s",
                product,
                exc,
            )

        today = datetime.now(MARKET_TZ).date()

        # Source 1: Upstox instrument discovery.
        futures = [
            item
            for item in discovered.get("FUT", [])
            if self._active(item, today) and matches_product(item)
        ]

        # Source 2: Official Upstox instrument master.
        if not futures:
            try:
                futures = [
                    item
                    for item in self._from_master({"FUT"})
                    if self._active(item, today) and matches_product(item)
                ]
            except (UpstoxAPIError, ValueError, OSError, httpx.HTTPError) as exc:
                logger.warning(
                    "Instrument-master fallback failed for %s: %s",
                    product,
                    exc,
                )

        # Source 3: Existing verified database metadata.
        if not futures:
            verified = self.repository.get_verified_instruments()

            futures = [
                self._instrument_dict(row)
                for row in verified
                if getattr(row, "instrument_type", None) == "FUT"
                and getattr(row, "exchange", None) == "MCX"
                and getattr(row, "segment", None) == "MCX_FO"
                and matches_product(row)
                and self._active(
                    {
                        "expiry": getattr(row, "expiry", ""),
                        "contract_status": getattr(row, "contract_status", None),
                    },
                    today,
                )
            ]

        if not futures:
            message = (
                f"No active verified MCX {product} futures contract "
                "was found in Upstox discovery, instrument master, "
                "or existing verified metadata"
            )

            if search_error is not None:
                raise RuntimeError(message) from search_error

            raise ValueError(message)

        # Select only the explicitly configured product.
        future = CrudeContractSelector.select_from(
            futures,
            today=today,
            product=product,
        )["future"]

        if not isinstance(future, dict):
            future = self._instrument_dict(future)

        if str(future.get("underlying_symbol", "")).upper().strip() != product:
            raise RuntimeError(
                f"Selected futures contract does not match configured product {product}"
            )

        # Gather related futures only for the selected underlying.
        related_futures = list(futures)

        try:
            related_futures.extend(
                item
                for item in self._from_master({"FUT"})
                if self._active(item, today)
                and matches_product(item)
            )
        except (UpstoxAPIError, ValueError, OSError, httpx.HTTPError) as exc:
            logger.warning(
                "Unable to load additional %s futures metadata: %s",
                product,
                exc,
            )

        related_futures = list(
            {
                item["instrument_key"]: item
                for item in related_futures
                if item.get("instrument_key")
                and str(item.get("underlying_symbol", "")).upper().strip()
                == product
            }.values()
        )

        # Discover options linked to the selected futures underlying.
        options = self._discover_options(
            future,
            discovered,
            today,
            related_futures=related_futures,
        )

        if not options:
            raise ValueError(
                f"No active {product} CE/PE option contracts "
                "match the selected futures underlying"
            )

        # Final product-isolation check before persistence.
        for instrument in options:
            if (
                str(instrument.get("underlying_symbol", "")).upper().strip()
                != product
            ):
                raise RuntimeError(
                    f"Rejected mismatched option contract for {product}"
                )

        for instrument in [future, *options]:
            self.repository.save_verified_instrument(instrument)

        logger.info(
            "Selected MCX product=%s future=%s expiry=%s options=%d",
            product,
            future["trading_symbol"],
            future["expiry"],
            len(options),
        )

        return future, options


    @staticmethod
    def _instrument_dict(instrument: Any) -> dict[str, Any]:
        keys = (
            "instrument_key", "trading_symbol", "exchange", "segment", "instrument_type",
            "expiry", "strike_price", "lot_size", "tick_size", "underlying_key",
            "underlying_symbol", "contract_status",
        )
        if isinstance(instrument, dict):
            return {key: instrument.get(key) for key in keys}
        return {key: getattr(instrument, key, None) for key in keys}

    def _discover_options(
        self,
        future: dict[str, Any],
        search_results: dict[str, list[dict[str, Any]]],
        today: date,
        *,
        related_futures: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        # MCX option rows link their underlying_key to the specific futures contract,
        # not to the commodity root key carried by that futures contract.
        requested_underlying = future["instrument_key"]
        related_keys = {
            key for key in (requested_underlying, future["instrument_key"], future.get("underlying_key"))
            if key
        }
        related_keys.update(
            item["instrument_key"]
            for item in related_futures or []
            if item.get("instrument_key")
            and str(item.get("underlying_symbol", "")).upper()
            == str(future["underlying_symbol"]).upper()
        )
        option_contracts: list[dict[str, Any]] = []
        try:
            payload = self.client.option_contracts(requested_underlying)
            if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
                raise ValueError("Unexpected Upstox option-contract response schema")
            for row in payload["data"]:
                if not isinstance(row, dict) or row.get("instrument_type") not in {"CE", "PE"}:
                    continue
                contract = self.service._normalize_instrument(
                    row, row["instrument_type"], metadata_source="option_contract_api"
                )
                if (
                    contract.get("underlying_key") in related_keys
                    and contract["underlying_symbol"].upper() == future["underlying_symbol"].upper()
                    and self._active(contract, today)
                ):
                    option_contracts.append(contract)
        except (UpstoxAPIError, ValueError, OSError, httpx.HTTPError) as exc:
            logger.warning(
                "Option-contract discovery failed for underlying=%s; "
                "using instrument metadata fallback: %s",
                requested_underlying,
                exc,
            )

        if not option_contracts:
            option_contracts = [
                item for kind in ("CE", "PE") for item in search_results[kind]
                if item.get("underlying_key") in related_keys
                and item.get("underlying_symbol", "").upper() == future["underlying_symbol"].upper()
                and self._active(item, today)
            ]
        if not option_contracts:
            option_contracts = [
                item for item in self._from_master({"CE", "PE"})
                if item.get("underlying_key") in related_keys
                and item.get("underlying_symbol", "").upper() == future["underlying_symbol"].upper()
                and self._active(item, today)
            ]

        unique = {item["instrument_key"]: item for item in option_contracts}
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item["expiry"], float(item["strike_price"]),
                0 if item["instrument_type"] == "CE" else 1,
                item["trading_symbol"],
            ),
        )
        limit = self.config.live_max_contracts_per_cycle
        return ordered[:limit]

    def _refresh_contracts(self) -> None:
        future, options = self._discover_instruments()
        self.future, self.options = future, options
        self._last_refresh = self.clock()
        logger.info(
            "Contracts refreshed: future=%s expiry=%s CE=%d PE=%d",
            future["trading_symbol"], future["expiry"],
            sum(item["instrument_type"] == "CE" for item in options),
            sum(item["instrument_type"] == "PE" for item in options),
        )

    @staticmethod
    def _validate_quotes_response(payload: Any) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "success"
            or not isinstance(payload.get("data"), dict)
        ):
            raise ValueError("Unexpected Upstox Full Market Quotes response schema")
        return payload["data"]

    def _market_open(self, now: datetime) -> tuple[bool, str]:
        local_now = now.astimezone(MARKET_TZ)
        start = datetime_time.fromisoformat(self.config.mcx_market_open_time)
        end = datetime_time.fromisoformat(self.config.mcx_market_close_time)
        if local_now.weekday() >= 5 or not start <= local_now.time().replace(tzinfo=None) <= end:
            return False, "OUTSIDE_CONFIGURED_MCX_HOURS"

        monotonic_now = self.clock()
        if self._last_market_status and monotonic_now - self._last_market_status[0] < 60:
            return self._last_market_status[1], "CACHED_MARKET_STATUS"
        payload = self.client.market_status("MCX")
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Unexpected Upstox MCX market-status response schema")
        values = _status_values(payload)
        is_open = bool(values & OPEN_STATUSES)
        self._last_market_status = (monotonic_now, is_open)
        status = sorted(values)[0] if values else "UNKNOWN"
        return is_open, status

    @staticmethod
    def _find_quote(data: dict[str, Any], instrument_key: str) -> dict[str, Any]:
        for quote in data.values():
            if isinstance(quote, dict) and quote.get("instrument_token") == instrument_key:
                return quote
        direct = data.get(instrument_key)
        if isinstance(direct, dict) and direct.get("instrument_token") == instrument_key:
            return direct
        raise QuoteValidationError("Quote response omitted the matching instrument")

    def _record_quality(
        self,
        collector: str,
        instrument_key: str | None,
        classification: str,
        reason: str,
        observed_at: datetime,
        response_timestamp: datetime | None = None,
    ) -> None:
        self.repository.add_collection_quality_event(
            collector=collector,
            instrument_key=instrument_key,
            classification=classification,
            reason=reason,
            observed_at=observed_at,
            response_timestamp=response_timestamp,
        )

    def _collect_quotes(
        self,
        collector: str,
        instruments: list[dict[str, Any]],
        observed_at: datetime,
    ) -> dict[str, int]:
        counts = {"valid": 0, "stale": 0, "invalid": 0, "api_error": 0, "persisted": 0}
        for offset in range(0, len(instruments), 500):
            batch = instruments[offset:offset + 500]
            try:
                payload = self.client.market_quotes([item["instrument_key"] for item in batch])
                data = self._validate_quotes_response(payload)
                batch_observed_at = self.utc_clock()
                if (
                    not isinstance(batch_observed_at, datetime)
                    or batch_observed_at.tzinfo is None
                    or batch_observed_at.utcoffset() is None
                ):
                    raise ValueError("UTC clock must return a timezone-aware datetime")
                batch_observed_at = batch_observed_at.astimezone(timezone.utc)
                logger.info("%s Full Market Quotes GET succeeded for %d contracts", collector, len(batch))
            except (UpstoxAPIError, ValueError, OSError) as exc:
                counts["api_error"] += len(batch)
                logger.warning(
                    "%s quote request failed for %d contracts (status=%s request_id=%s): %s",
                    collector,
                    len(batch),
                    getattr(exc, "status_code", None),
                    getattr(exc, "request_id", None),
                    exc,
                )
                for item in batch:
                    self._record_quality(
                        collector, item["instrument_key"], "API_ERROR", str(exc), observed_at
                    )
                continue

            for instrument in batch:
                raw = None
                try:
                    raw = self._find_quote(data, instrument["instrument_key"])
                    quote, classification = normalize_live_quote(
                        instrument,
                        raw,
                        snapshot_at=batch_observed_at,
                        freshness_seconds=self.config.live_quote_freshness_seconds,
                    )
                    if classification == "VALID_STALE":
                        counts["stale"] += 1
                        self._record_quality(
                            collector,
                            instrument["instrument_key"],
                            classification,
                            "Last trade is older than the configured freshness threshold",
                            batch_observed_at,
                            quote["response_timestamp"],
                        )
                        self._log_quote_rejection(
                            collector, instrument, "Last trade is older than the configured "
                            "freshness threshold", raw,
                        )
                        continue
                    inserted = self.repository.add_market_quote(quote, instrument=instrument)
                    self._logged_rejections.pop(instrument["instrument_key"], None)
                    counts["valid"] += 1
                    counts["persisted"] += int(inserted)
                except QuoteValidationError as exc:
                    counts["invalid"] += 1
                    self._record_quality(
                        collector, instrument["instrument_key"], "INVALID", str(exc), batch_observed_at
                    )
                    self._log_quote_rejection(collector, instrument, str(exc), raw)
                except (KeyError, TypeError, ValueError) as exc:
                    counts["invalid"] += 1
                    self._record_quality(
                        collector, instrument["instrument_key"], "INVALID",
                        f"Malformed quote: {exc}", batch_observed_at,
                    )
                    self._log_quote_rejection(
                        collector, instrument, f"Malformed quote: {exc}",
                        raw,
                    )
        logger.info(
            "%s cycle: requested=%d fresh=%d stale=%d invalid=%d api_error=%d persisted=%d",
            collector, len(instruments), counts["valid"], counts["stale"],
            counts["invalid"], counts["api_error"], counts["persisted"],
        )
        return counts

    def _log_quote_rejection(
        self,
        collector: str,
        instrument: dict[str, Any],
        reason: str,
        quote: Any,
    ) -> None:
        key = str(instrument.get("instrument_key", ""))
        if self._logged_rejections.get(key) == reason:
            return
        if len(self._logged_rejections) >= 1000:
            self._logged_rejections.clear()
        self._logged_rejections[key] = reason

        quote = quote if isinstance(quote, dict) else {}
        depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}

        def first_level(side: str) -> dict[str, Any] | None:
            levels = depth.get(side)
            if not isinstance(levels, list):
                return None
            for level in levels:
                if isinstance(level, dict):
                    return {
                        field: level.get(field)
                        for field in ("price", "quantity")
                        if isinstance(level.get(field), (str, int, float))
                        and not isinstance(level.get(field), bool)
                    }
            return None

        ohlc = quote.get("ohlc") if isinstance(quote.get("ohlc"), dict) else {}
        logger.warning(
            "%s quote rejected: key=%s type=%s reason=%s ltp=%r "
            "response_timestamp=%r last_trade_time=%r quote_key=%r "
            "bid=%r ask=%r open=%r high=%r low=%r close=%r volume=%r oi=%r",
            collector,
            key,
            instrument.get("instrument_type"),
            reason,
            quote.get("last_price"),
            quote.get("timestamp"),
            quote.get("last_trade_time"),
            quote.get("instrument_token"),
            first_level("buy"),
            first_level("sell"),
            ohlc.get("open"),
            ohlc.get("high"),
            ohlc.get("low"),
            ohlc.get("close"),
            quote.get("volume"),
            quote.get("oi"),
        )

    def run_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.future is None:
            raise RuntimeError("Collector must be prepared before polling")
        if self.clock() - self._last_refresh >= self.config.live_contract_refresh_seconds:
            try:
                self._refresh_contracts()
            except (UpstoxAPIError, ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
                logger.exception("Contract refresh failed; retaining last verified metadata: %s", exc)

        try:
            is_open, market_status = self._market_open(observed_at)
        except (UpstoxAPIError, ValueError, OSError) as exc:
            logger.warning("Unable to verify MCX market status; skipping this cycle: %s", exc)
            self._write_health("MARKET_STATUS_ERROR", error_type=type(exc).__name__)
            return {"market_open": False, "reason": "MARKET_STATUS_ERROR"}
        if not is_open:
            self._write_health("MARKET_CLOSED", market_status=market_status)
            return {"market_open": False, "reason": market_status}

        today = observed_at.astimezone(MARKET_TZ).date()
        if not self._active(self.future, today):
            raise RuntimeError("Selected futures contract has expired; refusing to quote it")
        counts = self._collect_quotes("FUTURES", [self.future], observed_at)
        self._cycle_counts = {"futures_" + key: value for key, value in counts.items()}
        if counts["valid"]:
            self._last_success = observed_at.isoformat()
        self._write_health("COLLECTING", market_status=market_status, counts=self._cycle_counts)
        return {"market_open": True, "futures": counts, "market_status": market_status}

    def collect_options_cycle(self, now: datetime | None = None) -> dict[str, int]:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.options:
            raise RuntimeError("No active option contracts are available")
        try:
            is_open, market_status = self._market_open(observed_at)
        except (UpstoxAPIError, ValueError, OSError) as exc:
            logger.warning("Unable to verify MCX market status for options; skipping cycle: %s", exc)
            self._write_health("MARKET_STATUS_ERROR", error_type=type(exc).__name__)
            return {"valid": 0, "stale": 0, "invalid": 0, "api_error": 0, "persisted": 0}
        if not is_open:
            self._write_health("MARKET_CLOSED", market_status=market_status)
            return {"valid": 0, "stale": 0, "invalid": 0, "api_error": 0, "persisted": 0}
        today = observed_at.astimezone(MARKET_TZ).date()
        active = [item for item in self.options if self._active(item, today)]
        if not active:
            raise RuntimeError("No active CE/PE contracts remain after expiry filtering")
        counts = self._collect_quotes("OPTIONS", active, observed_at)
        self._cycle_counts.update({"options_" + key: value for key, value in counts.items()})
        if counts["valid"]:
            self._last_success = observed_at.isoformat()
        self._write_health("COLLECTING", counts=self._cycle_counts)
        return counts

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        now = self.clock()
        next_futures = next_options = now
        while not stop.is_set():
            current = self.clock()
            wall_time = datetime.now(timezone.utc)
            try:
                cycle = self.run_cycle(wall_time) if current >= next_futures else {"market_open": False}
                if current >= next_futures:
                    next_futures = current + self.config.live_futures_poll_seconds
                if cycle.get("market_open") and current >= next_options:
                    try:
                        self.collect_options_cycle(wall_time)
                    except Exception:
                        logger.exception("Options collection cycle failed")
                    next_options = current + self.config.live_options_poll_seconds
            except Exception:
                logger.exception("Futures collection cycle failed")
                next_futures = current + self.config.live_futures_poll_seconds
            stop.wait(min(1.0, self.config.live_futures_poll_seconds, self.config.live_options_poll_seconds))
        logger.info("Live market-data collector stopped")
        self._write_health("STOPPED")

    def shutdown(self) -> None:
        self._write_health("STOPPED")

    def _write_health(self, status: str, **details: Any) -> None:
        self._last_health = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_successful_collection": self._last_success,
            "trading_mode": "PAPER",
            "selected_future": (
                {
                    "instrument_key": self.future.get("instrument_key"),
                    "trading_symbol": self.future.get("trading_symbol"),
                    "expiry": self.future.get("expiry"),
                } if self.future else None
            ),
            "active_call_contracts": sum(
                item["instrument_type"] == "CE" for item in self.options
            ),
            "active_put_contracts": sum(
                item["instrument_type"] == "PE" for item in self.options
            ),
            **details,
        }
        # Health reporting must never terminate market-data collection. On Windows,
        # another process (for example an editor or antivirus scanner) can briefly
        # lock the destination while os.replace() is attempted. Use a unique temp
        # file in the same directory (required for an atomic same-volume replace),
        # retry transient PermissionError, and always clean up the temp file.
        temporary_path: Path | None = None
        try:
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._last_health, indent=2, ensure_ascii=False) + "\n"

            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.health_path.name}.",
                suffix=".tmp",
                dir=str(self.health_path.parent),
                text=True,
            )
            temporary_path = Path(temporary_name)

            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            retry_delays = (0.05, 0.10, 0.20, 0.40, 0.80)
            for attempt, delay in enumerate(retry_delays, start=1):
                try:
                    os.replace(temporary_path, self.health_path)
                    temporary_path = None  # The rename consumed the temp file.
                    return
                except PermissionError:
                    if attempt == len(retry_delays):
                        raise
                    logger.warning(
                        "Health report is temporarily locked; retrying replace "
                        "(%d/%d): %s",
                        attempt,
                        len(retry_delays),
                        self.health_path,
                    )
                    time.sleep(delay)
        except Exception:
            # A health-file failure is non-fatal: collection and database writes
            # must continue. Keep the exception visible for diagnosis.
            logger.exception(
                "Unable to write collector health report to %s; "
                "market-data collection will continue",
                self.health_path,
            )
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Could not remove temporary health report %s",
                        temporary_path,
                        exc_info=True,
                    )
