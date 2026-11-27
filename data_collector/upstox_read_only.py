from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from data_collector.market_data_normalizer import SUPPORTED_INSTRUMENT_TYPES
from data_collector.tick_size_normalizer import normalize_upstox_tick_size
from database.repository import Repository
from execution.upstox_client import UpstoxClient


class QuoteValidationError(ValueError):
    """An Upstox market quote is incomplete, invalid, or stale."""


class UpstoxReadOnlyService:
    """Read-only Upstox authentication, MCX crude discovery, and quote storage."""

    CONTRACT_TYPES = ("FUT", "CE", "PE")
    MAX_SEARCH_PAGES = 100

    def __init__(
        self,
        client: UpstoxClient,
        repository: Repository | None = None,
        *,
        max_quote_age_seconds: float = 300.0,
    ):
        if max_quote_age_seconds < 0 or not math.isfinite(max_quote_age_seconds):
            raise ValueError("max_quote_age_seconds must be finite and non-negative")
        self.client = client
        self.repository = repository or Repository()
        self.max_quote_age_seconds = max_quote_age_seconds

    def check_profile_api(self) -> dict[str, str]:
        """Check profile access separately; Upstox requires a registered static IP."""
        profile = self.client.user_profile()
        return {
            "status": "accessible",
            "broker": str(profile.get("broker", "unknown")),
        }

    def discover_crude_instruments(self) -> dict[str, list[dict[str, Any]]]:
        """Search MCX FO, retain only verified crude FUT/CE/PE instrument rows."""
        discovered: dict[str, list[dict[str, Any]]] = {kind: [] for kind in self.CONTRACT_TYPES}
        seen: set[str] = set()
        for instrument_type in self.CONTRACT_TYPES:
            page_number = 1
            while page_number <= self.MAX_SEARCH_PAGES:
                payload = self.client.search_instruments(
                    "CRUDE",
                    exchanges="MCX",
                    segments="FO",
                    instrument_types=instrument_type,
                    page_number=page_number,
                    records=30,
                )
                rows = payload.get("data")
                if payload.get("status") != "success" or not isinstance(rows, list):
                    raise ValueError("Unexpected Upstox instrument-search response schema")
                for row in rows:
                    if not self._is_crude_row(row):
                        continue
                    metadata = self._normalize_instrument(
                        row, instrument_type, metadata_source="instrument_search"
                    )
                    if metadata["instrument_key"] not in seen:
                        discovered[instrument_type].append(metadata)
                        seen.add(metadata["instrument_key"])
                metadata = payload.get("meta_data")
                page = metadata.get("page") if isinstance(metadata, dict) else None
                if not isinstance(page, dict):
                    raise ValueError("Upstox instrument-search response is missing pagination metadata")
                total_pages = page.get("total_pages")
                returned_page = page.get("page_number")
                if (
                    not isinstance(total_pages, int)
                    or isinstance(total_pages, bool)
                    or total_pages < 1
                    or not isinstance(returned_page, int)
                    or returned_page != page_number
                    or total_pages < page_number
                ):
                    raise ValueError("Invalid pagination metadata from Upstox instrument search")
                if page_number >= total_pages:
                    break
                page_number += 1
            else:
                raise ValueError("Instrument search exceeded safe pagination limit")

        for instruments in discovered.values():
            for instrument in instruments:
                self.repository.save_verified_instrument(instrument)
        return discovered

    def fetch_and_persist_quotes(
        self,
        instruments: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch quotes in documented batches, persisting only complete fresh snapshots."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = current_time.astimezone(timezone.utc)

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for start in range(0, len(instruments), 500):
            batch = instruments[start : start + 500]
            keys = [item["instrument_key"] for item in batch]
            payload = self.client.market_quotes(keys)
            if payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
                raise ValueError("Unexpected Upstox market-quote response schema")
            quotes = payload["data"]
            batch_time = current_time if now is not None else datetime.now(timezone.utc)
            for instrument in batch:
                try:
                    raw_quote = self._find_quote(quotes, instrument["instrument_key"])
                    normalized = self.normalize_quote(
                        instrument,
                        raw_quote,
                        now=batch_time,
                        max_age_seconds=self.max_quote_age_seconds,
                    )
                    saved = self.repository.add_market_quote(normalized, instrument=instrument)
                    accepted.append({**normalized, "persisted": saved})
                except (KeyError, TypeError, ValueError) as exc:
                    detail = self._quote_evidence(quotes, instrument["instrument_key"])
                    rejected.append({
                        "instrument_key": instrument["instrument_key"],
                        "reason": str(exc),
                        "instrument": {
                            key: instrument.get(key)
                            for key in ("trading_symbol", "instrument_type", "expiry", "contract_status")
                        },
                        "quote_evidence": detail,
                    })
        return {"accepted": accepted, "rejected": rejected}

    @staticmethod
    def _is_crude_row(row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        names = " ".join(
            str(row.get(field, ""))
            for field in ("name", "trading_symbol", "underlying_symbol")
        ).upper().replace(" ", "")
        return "CRUDEOIL" in names or "CRUDE" in names

    @classmethod
    def _normalize_instrument(
        cls, row: dict, requested_type: str, *, metadata_source: str = "instrument_search"
    ) -> dict[str, Any]:
        required = (
            "instrument_key", "trading_symbol", "exchange", "segment", "instrument_type",
            "expiry", "strike_price", "lot_size", "tick_size", "underlying_symbol",
        )
        missing = [field for field in required if row.get(field) is None or row.get(field) == ""]
        if missing:
            raise ValueError(f"Upstox crude contract missing required verified fields: {missing}")
        if row["exchange"] != "MCX" or row["segment"] != "MCX_FO":
            raise ValueError("Upstox crude contract is not MCX_FO")
        if row["instrument_type"] != requested_type or requested_type not in SUPPORTED_INSTRUMENT_TYPES:
            raise ValueError("Upstox crude contract instrument_type does not match the requested type")
        try:
            strike = float(row["strike_price"])
            lot_value = float(row["lot_size"])
            raw_tick_size = float(row["tick_size"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Upstox crude contract has invalid strike, lot size, or tick size") from exc
        if (
            not math.isfinite(strike)
            or not math.isfinite(lot_value)
            or not math.isfinite(raw_tick_size)
            or lot_value <= 0
            or not lot_value.is_integer()
            or raw_tick_size <= 0
        ):
            raise ValueError("Upstox crude contract has invalid strike, lot size, or tick size")
        tick_size = normalize_upstox_tick_size(
            raw_tick_size,
            instrument_type=requested_type,
            trading_symbol=str(row["trading_symbol"]),
            underlying_symbol=str(row["underlying_symbol"]),
            lot_size=int(lot_value),
        )
        try:
            expiry = cls._normalize_expiry(row["expiry"])
        except (OverflowError, OSError, TypeError, ValueError) as exc:
            raise ValueError("Upstox crude contract expiry must be a valid ISO date or epoch milliseconds") from exc
        if not str(row["instrument_key"]).strip() or not str(row["trading_symbol"]).strip():
            raise ValueError("Upstox crude contract key and trading symbol must not be empty")
        if requested_type in {"CE", "PE"} and strike <= 0:
            raise ValueError("Upstox option contract strike_price must be positive")
        if requested_type == "FUT" and strike != 0:
            raise ValueError("Upstox futures contract strike_price must match its reported value (expected 0)")
        contract_status = (
            "ACTIVE"
            if expiry >= datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
            else "EXPIRED"
        )
        return {
            "instrument_key": str(row["instrument_key"]),
            "trading_symbol": str(row["trading_symbol"]),
            "exchange": row["exchange"],
            "segment": row["segment"],
            "instrument_type": row["instrument_type"],
            "expiry": expiry,
            "strike_price": strike,
            "lot_size": int(lot_value),
            "tick_size": tick_size,
            "underlying_key": row.get("underlying_key"),
            "underlying_symbol": str(row["underlying_symbol"]),
            "contract_status": contract_status,
            "raw_metadata": dict(row),
            "metadata_source": metadata_source,
        }

    @staticmethod
    def _normalize_expiry(value: Any) -> str:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if not math.isfinite(numeric) or numeric <= 0:
                raise ValueError("Expiry epoch must be positive and finite")
            return datetime.fromtimestamp(numeric / 1000, tz=timezone.utc).date().isoformat()
        expiry_text = str(value)
        if expiry_text.isdigit():
            return UpstoxReadOnlyService._normalize_expiry(int(expiry_text))
        return date.fromisoformat(expiry_text).isoformat()

    @staticmethod
    def _find_quote(quotes: dict, instrument_key: str) -> dict:
        for quote in quotes.values():
            if isinstance(quote, dict) and quote.get("instrument_token") == instrument_key:
                return quote
        direct = quotes.get(instrument_key)
        if isinstance(direct, dict):
            return direct
        raise QuoteValidationError("Upstox response did not contain a quote matching the instrument key")

    @classmethod
    def normalize_quote(
        cls,
        instrument: dict,
        quote: dict,
        *,
        now: datetime,
        max_age_seconds: float,
    ) -> dict[str, Any]:
        if not isinstance(quote, dict):
            raise QuoteValidationError("Quote payload must be an object")
        if quote.get("instrument_token") != instrument["instrument_key"]:
            raise QuoteValidationError("Quote instrument token does not match discovered instrument")
        ltp = quote.get("last_price")
        if ltp is None:
            raise QuoteValidationError("Quote is incomplete: last_price is missing")
        if isinstance(ltp, bool):
            raise QuoteValidationError("Quote last_price is invalid")
        try:
            ltp = float(ltp)
        except (TypeError, ValueError) as exc:
            raise QuoteValidationError("Quote last_price is invalid") from exc
        if not math.isfinite(ltp) or ltp <= 0:
            raise QuoteValidationError("Quote last_price must be finite and positive")

        timestamp_value = quote.get("timestamp")
        if not timestamp_value:
            raise QuoteValidationError("Quote is incomplete: response timestamp is missing")
        try:
            response_timestamp = datetime.fromisoformat(
                str(timestamp_value).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise QuoteValidationError("Quote response timestamp is invalid") from exc
        if response_timestamp.tzinfo is None or response_timestamp.utcoffset() is None:
            raise QuoteValidationError("Quote response timestamp must include a timezone")
        response_timestamp = response_timestamp.astimezone(timezone.utc)
        current_time = now.astimezone(timezone.utc)
        response_age_seconds = (current_time - response_timestamp).total_seconds()
        if response_age_seconds < 0:
            raise QuoteValidationError("Quote response timestamp is in the future")
        if response_age_seconds > max_age_seconds:
            raise QuoteValidationError(
                "Quote response is stale "
                f"({response_age_seconds:.1f}s old; maximum {max_age_seconds:.1f}s)"
            )

        last_trade_time = cls._normalize_epoch_ms(quote.get("last_trade_time"))
        if last_trade_time is None:
            raise QuoteValidationError("Quote is incomplete: last_trade_time is missing")
        age_seconds = (current_time - last_trade_time).total_seconds()
        if age_seconds < 0:
            raise QuoteValidationError("Quote last-trade timestamp is in the future")
        if age_seconds > max_age_seconds:
            raise QuoteValidationError(
                "Quote last trade is stale "
                f"({age_seconds:.1f}s old; maximum {max_age_seconds:.1f}s)"
            )

        depth = quote.get("depth") or {}
        if not isinstance(depth, dict):
            raise QuoteValidationError("Quote depth must be an object")
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        bid = cls._top_price(bids)
        ask = cls._top_price(asks)
        if bid is not None and ask is not None and ask < bid:
            raise QuoteValidationError("Quote has a crossed bid/ask market")
        volume = cls._optional_nonnegative(quote.get("volume"), "volume")
        open_interest = cls._optional_nonnegative(quote.get("oi"), "open_interest")
        return {
            "instrument_key": instrument["instrument_key"],
            "timestamp": current_time,
            "response_timestamp": response_timestamp,
            "last_trade_time": last_trade_time,
            "quality_status": "VALID_FRESH",
            "last_price": ltp,
            "bid_price": bid,
            "ask_price": ask,
            "volume": volume,
            "open_interest": open_interest,
            "open": cls._optional_positive(quote.get("ohlc", {}).get("open"), "ohlc.open"),
            "high": cls._optional_positive(quote.get("ohlc", {}).get("high"), "ohlc.high"),
            "low": cls._optional_positive(quote.get("ohlc", {}).get("low"), "ohlc.low"),
            "close": cls._optional_positive(quote.get("ohlc", {}).get("close"), "ohlc.close"),
            "source": "upstox",
        }

    @classmethod
    def _quote_evidence(cls, quotes: dict, instrument_key: str) -> dict[str, Any] | None:
        try:
            quote = cls._find_quote(quotes, instrument_key)
        except QuoteValidationError:
            return None
        ohlc = quote.get("ohlc") if isinstance(quote.get("ohlc"), dict) else {}
        depth = quote.get("depth") if isinstance(quote.get("depth"), dict) else {}
        return {
            "instrument_token": quote.get("instrument_token"),
            "last_price": quote.get("last_price"),
            "timestamp": quote.get("timestamp"),
            "last_trade_time": quote.get("last_trade_time"),
            "ohlc": {key: ohlc.get(key) for key in ("open", "high", "low", "close")},
            "volume": quote.get("volume"),
            "oi": quote.get("oi"),
            "depth_buy": cls._first_depth(depth.get("buy")),
            "depth_sell": cls._first_depth(depth.get("sell")),
        }

    @staticmethod
    def _first_depth(levels: Any) -> dict[str, Any] | None:
        if not isinstance(levels, list):
            return None
        for level in levels:
            if isinstance(level, dict):
                return {key: level.get(key) for key in ("price", "quantity", "orders")}
        return None

    @staticmethod
    def _top_price(levels: list) -> float | None:
        if not isinstance(levels, list):
            raise QuoteValidationError("Quote depth must be a list")
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = level.get("price")
            quantity = level.get("quantity", 0)
            if price is None or quantity is None:
                continue
            if isinstance(price, bool) or isinstance(quantity, bool):
                raise QuoteValidationError("Quote depth contains invalid price or quantity")
            try:
                price_value = float(price)
                quantity_value = float(quantity)
            except (TypeError, ValueError) as exc:
                raise QuoteValidationError("Quote depth contains invalid price or quantity") from exc
            if not math.isfinite(price_value) or not math.isfinite(quantity_value):
                raise QuoteValidationError("Quote depth contains non-finite values")
            if price_value > 0 and quantity_value > 0:
                return price_value
        return None

    @staticmethod
    def _optional_nonnegative(value: Any, label: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise QuoteValidationError(f"Quote {label} is invalid")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise QuoteValidationError(f"Quote {label} is invalid") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise QuoteValidationError(f"Quote {label} must be finite and non-negative")
        return numeric

    @staticmethod
    def _optional_positive(value: Any, label: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise QuoteValidationError(f"Quote {label} is invalid")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise QuoteValidationError(f"Quote {label} is invalid") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise QuoteValidationError(f"Quote {label} must be finite and positive when supplied")
        return numeric

    @staticmethod
    def _normalize_epoch_ms(value: Any) -> datetime | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError
            return datetime.fromtimestamp(number / 1000, tz=timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError) as exc:
            raise QuoteValidationError("Quote last_trade_time is invalid") from exc
