
from __future__ import annotations

from datetime import datetime, timezone
import math
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db_connection import SessionLocal
from database.models import (
    CandleTrainingExclusion,
    MarketCandle,
    MarketQuoteSnapshot,
    OptionSnapshot,
    PaperTrade,
    RawInstrumentMetadata,
    SystemEvent,
    VerifiedInstrument,
)


class RepositoryIntegrityError(RuntimeError):
    """A database integrity failure that is not a known duplicate record."""


class Repository:
    """Simple persistence wrapper for candle, quote, and paper-trade records."""

    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def add_candle(self, candle: dict) -> bool:
        self._validate_candle(candle)
        try:
            with self.session_factory.begin() as session:
                session.add(MarketCandle(**candle))
                session.flush()
            return True
        except IntegrityError as exc:
            with self.session_factory() as session:
                duplicate = session.scalar(
                    select(MarketCandle.id).where(
                        MarketCandle.instrument_key == candle["instrument_key"],
                        MarketCandle.interval == candle["interval"],
                        MarketCandle.timestamp == candle["timestamp"],
                    )
                )
            if duplicate is not None:
                return False
            raise RepositoryIntegrityError("Database rejected a market candle; transaction rolled back") from exc

    def get_market_candles(
        self,
        instrument_key: str,
        interval: str = "5_min",
        limit: int = 500,
    ) -> list[MarketCandle]:
        """Return the newest bounded candle history in chronological order."""
        if not instrument_key:
            raise ValueError("instrument_key is required")
        if not interval:
            raise ValueError("interval is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self.session_factory() as session:
            statement = (
                select(MarketCandle)
                .where(
                    MarketCandle.instrument_key == instrument_key,
                    MarketCandle.interval == interval,
                    MarketCandle.instrument_type == "FUT",
                    MarketCandle.exchange == "MCX",
                )
                .order_by(MarketCandle.timestamp.desc())
                .limit(limit)
            )
            rows = list(session.scalars(statement).all())
            rows.reverse()
            return rows

    def exclude_candle_from_training(
        self,
        candle_id: int,
        reason: str,
    ) -> bool:
        """
        Exclude one existing candle from AI training and backtesting.

        Returns True when a new exclusion is created, or False when the
        candle was already excluded. The original candle is never changed.
        """
        if (
            isinstance(candle_id, bool)
            or not isinstance(candle_id, int)
            or candle_id < 1
        ):
            raise ValueError("candle_id must be a positive integer")

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An exclusion reason is required")

        with self.session_factory.begin() as session:
            candle = session.get(MarketCandle, candle_id)
            if candle is None:
                raise ValueError(f"Market candle {candle_id} does not exist")

            existing = session.get(CandleTrainingExclusion, candle_id)
            if existing is not None:
                return False

            session.add(
                CandleTrainingExclusion(
                    candle_id=candle_id,
                    reason=reason.strip(),
                )
            )
            session.flush()

        return True

    def get_training_candles(
        self,
        instrument_key: str,
        interval: str = "5_min",
        limit: int = 500,
    ) -> list[MarketCandle]:
        """
        Return non-quarantined MCX futures candles for training/backtesting.

        The newest bounded set is selected first and returned chronologically.
        Existing get_market_candles() behaviour is intentionally unchanged.
        """
        if not instrument_key:
            raise ValueError("instrument_key is required")
        if not interval:
            raise ValueError("interval is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")

        with self.session_factory() as session:
            statement = (
                select(MarketCandle)
                .outerjoin(
                    CandleTrainingExclusion,
                    CandleTrainingExclusion.candle_id == MarketCandle.id,
                )
                .where(
                    MarketCandle.instrument_key == instrument_key,
                    MarketCandle.interval == interval,
                    MarketCandle.instrument_type == "FUT",
                    MarketCandle.exchange == "MCX",
                    CandleTrainingExclusion.candle_id.is_(None),
                )
                .order_by(MarketCandle.timestamp.desc())
                .limit(limit)
            )
            rows = list(session.scalars(statement).all())
            rows.reverse()
            return rows

    def get_candle_training_exclusions(self) -> list[CandleTrainingExclusion]:
        """Return all training exclusions, newest first, for audit purposes."""
        with self.session_factory() as session:
            statement = select(CandleTrainingExclusion).order_by(
                CandleTrainingExclusion.created_at.desc(),
                CandleTrainingExclusion.candle_id.desc(),
            )
            return list(session.scalars(statement).all())

    def add_quote(self, quote: dict) -> bool:
        snapshot = self._normalize_quote(quote)
        if snapshot["option_type"] not in {"CE", "PE"}:
            raise ValueError("Option snapshot option_type must be CE or PE")
        if not snapshot["instrument_key"] or not snapshot["trading_symbol"] or not snapshot["expiry"]:
            raise ValueError("Option snapshot requires instrument key, symbol, and expiry")
        if snapshot["strike_price"] <= 0:
            raise ValueError("Option snapshot strike_price must be positive")
        if snapshot["timestamp"].tzinfo is None or snapshot["timestamp"].utcoffset() is None:
            raise ValueError("Option snapshot timestamp must be timezone-aware")
        if snapshot["ltp"] is None or not math.isfinite(snapshot["ltp"]) or snapshot["ltp"] <= 0:
            raise ValueError("Option snapshot ltp must be finite and positive")
        try:
            with self.session_factory.begin() as session:
                session.add(OptionSnapshot(**snapshot))
                session.flush()
            return True
        except IntegrityError as exc:
            with self.session_factory() as session:
                duplicate = session.scalar(
                    select(OptionSnapshot.id).where(
                        OptionSnapshot.instrument_key == snapshot["instrument_key"],
                        OptionSnapshot.timestamp == snapshot["timestamp"],
                    )
                )
            if duplicate is not None:
                return False
            raise RepositoryIntegrityError("Database rejected an option snapshot; transaction rolled back") from exc

    def add_system_event(self, event_type: str, message: str, severity: str = "INFO") -> None:
        with self.session_factory.begin() as session:
            session.add(SystemEvent(event_type=event_type, message=message, severity=severity))

    def save_verified_instrument(self, instrument: dict) -> None:
        required = (
            "instrument_key", "trading_symbol", "exchange", "segment", "instrument_type",
            "expiry", "strike_price", "lot_size", "tick_size", "underlying_symbol",
            "contract_status",
        )
        missing = [field for field in required if instrument.get(field) is None]
        if missing:
            raise ValueError(f"Verified instrument missing required fields: {missing}")
        if instrument["exchange"] != "MCX" or instrument["segment"] != "MCX_FO":
            raise ValueError("Only verified MCX_FO instruments can be persisted")
        if instrument["instrument_type"] not in {"FUT", "CE", "PE"}:
            raise ValueError(f"Unsupported instrument type: {instrument['instrument_type']!r}")
        if (
            not isinstance(instrument["lot_size"], int)
            or isinstance(instrument["lot_size"], bool)
            or not isinstance(instrument["tick_size"], (int, float))
            or isinstance(instrument["tick_size"], bool)
            or not math.isfinite(instrument["tick_size"])
            or instrument["lot_size"] <= 0
            or instrument["tick_size"] <= 0
        ):
            raise ValueError("Instrument lot_size and tick_size must be positive")
        with self.session_factory.begin() as session:
            row = session.get(VerifiedInstrument, instrument["instrument_key"])
            values = {field: instrument[field] for field in required}
            values["underlying_key"] = instrument.get("underlying_key")
            if row is None:
                session.add(VerifiedInstrument(**values, verified_at=datetime.now(timezone.utc)))
            else:
                changes = []
                for field, value in values.items():
                    current = getattr(row, field)
                    if field == "contract_status" or current in (None, "", "UNKNOWN"):
                        setattr(row, field, value)
                    elif field == "expiry" and str(current).isdigit():
                        try:
                            normalized_old_expiry = datetime.fromtimestamp(
                                int(current) / 1000, tz=timezone.utc
                            ).date().isoformat()
                        except (OverflowError, OSError, ValueError):
                            normalized_old_expiry = str(current)
                        if normalized_old_expiry == value:
                            row.expiry = value
                        else:
                            changes.append(field)
                    elif field == "tick_size" and current != value:
                        raw = instrument.get("raw_metadata") or {}
                        raw_tick = raw.get("tick_size")

                        instrument_type = str(instrument.get("instrument_type", "")).upper()
                        underlying_symbol = str(
                            instrument.get("underlying_symbol", "")
                        ).upper()
                        normalized_symbol = str(
                            instrument.get("trading_symbol", "")
                        ).upper().replace(" ", "")
                        lot_size = instrument.get("lot_size")

                        valid_raw_tick = (
                            isinstance(raw_tick, (int, float))
                            and not isinstance(raw_tick, bool)
                        )

                        is_standard_crude_option_tick_correction = (
                            instrument_type in {"CE", "PE"}
                            and underlying_symbol == "CRUDEOIL"
                            and normalized_symbol.startswith("CRUDEOIL")
                            and lot_size == 100
                            and valid_raw_tick
                            and math.isclose(
                                float(raw_tick), 10.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(current), 10.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(value), 0.10, rel_tol=0.0, abs_tol=1e-9
                            )
                        )

                        is_mini_crude_option_tick_correction = (
                            instrument_type in {"CE", "PE"}
                            and underlying_symbol == "CRUDEOILM"
                            and normalized_symbol.startswith("CRUDEOILM")
                            and lot_size == 10
                            and valid_raw_tick
                            and math.isclose(
                                float(raw_tick), 5.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(current), 5.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(value), 0.05, rel_tol=0.0, abs_tol=1e-9
                            )
                        )

                        is_standard_crude_future_tick_correction = (
                            instrument_type == "FUT"
                            and underlying_symbol == "CRUDEOIL"
                            and normalized_symbol.startswith("CRUDEOILFUT")
                            and lot_size == 100
                            and valid_raw_tick
                            and math.isclose(
                                float(raw_tick), 100.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(current), 100.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(value), 1.0, rel_tol=0.0, abs_tol=1e-9
                            )
                        )

                        is_mini_crude_future_tick_correction = (
                            instrument_type == "FUT"
                            and underlying_symbol == "CRUDEOILM"
                            and normalized_symbol.startswith("CRUDEOILMFUT")
                            and lot_size == 10
                            and valid_raw_tick
                            and math.isclose(
                                float(raw_tick), 100.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(current), 100.0, rel_tol=0.0, abs_tol=1e-9
                            )
                            and math.isclose(
                                float(value), 1.0, rel_tol=0.0, abs_tol=1e-9
                            )
                        )

                        is_verified_crude_tick_correction = (
                            is_standard_crude_option_tick_correction
                            or is_mini_crude_option_tick_correction
                            or is_standard_crude_future_tick_correction
                            or is_mini_crude_future_tick_correction
                        )

                        if is_verified_crude_tick_correction:
                            row.tick_size = value
                        else:
                            changes.append(field)
                    elif current != value:
                        changes.append(field)
                if changes:
                    raise ValueError(
                        "Previously verified instrument metadata changed for "
                        f"{instrument['instrument_key']!r}: {changes}"
                    )
                row.verified_at = datetime.now(timezone.utc)
            raw = instrument.get("raw_metadata")
            if raw is not None:
                raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                payload_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
                raw_exists = session.scalar(
                    select(RawInstrumentMetadata.id).where(
                        RawInstrumentMetadata.instrument_key == instrument["instrument_key"],
                        RawInstrumentMetadata.payload_hash == payload_hash,
                    )
                )
                if raw_exists is None:
                    session.add(RawInstrumentMetadata(
                        instrument_key=instrument["instrument_key"],
                        payload_hash=payload_hash,
                        raw_json=raw_json,
                        source=instrument.get("metadata_source", "upstox"),
                        captured_at=datetime.now(timezone.utc),
                    ))

    def add_market_quote(self, quote: dict, instrument: dict | None = None) -> bool:
        required = ("instrument_key", "timestamp", "last_price")
        missing = [field for field in required if quote.get(field) is None]
        if missing:
            raise ValueError(f"Market quote missing required fields: {missing}")
        if (
            not isinstance(quote["timestamp"], datetime)
            or quote["timestamp"].tzinfo is None
            or quote["timestamp"].utcoffset() is None
        ):
            raise ValueError("Market quote timestamp must be timezone-aware")
        numeric_fields = ("last_price", "bid_price", "ask_price", "volume", "open_interest")
        for field in numeric_fields:
            value = quote.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"Market quote {field} must be a finite number when supplied")
        if quote.get("last_trade_time") is not None and (
            not isinstance(quote["last_trade_time"], datetime)
            or quote["last_trade_time"].tzinfo is None
            or quote["last_trade_time"].utcoffset() is None
        ):
            raise ValueError("Market quote last_trade_time must be timezone-aware when supplied")
        if quote.get("response_timestamp") is not None and (
            not isinstance(quote["response_timestamp"], datetime)
            or quote["response_timestamp"].tzinfo is None
            or quote["response_timestamp"].utcoffset() is None
        ):
            raise ValueError("Market quote response_timestamp must be timezone-aware when supplied")
        if quote.get("quality_status", "VALID_FRESH") != "VALID_FRESH":
            raise ValueError("Only VALID_FRESH market quotes can be persisted as price snapshots")
        if quote.get("quality_status") is not None and (
            quote.get("response_timestamp") is None or quote.get("last_trade_time") is None
        ):
            raise ValueError("Live market quote requires response and last-trade timestamps")
        for field in ("open", "high", "low", "close"):
            value = quote.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Market quote {field} must be finite and positive when supplied")
        ohlc = [quote.get(field) for field in ("open", "high", "low", "close")]
        open_price, high_price, low_price, _previous_close = ohlc
        if all(value is not None for value in (open_price, high_price, low_price)):
            if high_price < max(open_price, low_price) or low_price > min(open_price, high_price):
                raise ValueError("Market quote OHLC values are inconsistent")
        if quote["last_price"] <= 0:
            raise ValueError("Market quote last_price must be positive")
        bid, ask = quote.get("bid_price"), quote.get("ask_price")
        if bid is not None and bid <= 0:
            raise ValueError("Market quote bid_price must be positive when supplied")
        if ask is not None and ask <= 0:
            raise ValueError("Market quote ask_price must be positive when supplied")
        if bid is not None and ask is not None and ask < bid:
            raise ValueError("Market quote ask_price must not be below bid_price")
        if quote.get("volume") is not None and quote["volume"] < 0:
            raise ValueError("Market quote volume must be non-negative")
        if quote.get("open_interest") is not None and quote["open_interest"] < 0:
            raise ValueError("Market quote open_interest must be non-negative")
        for field in ("bid_quantity", "ask_quantity"):
            value = quote.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Market quote {field} must be finite and non-negative")

        try:
            with self.session_factory.begin() as session:
                snapshot_fields = {
                    key: value for key, value in quote.items()
                    if key in {
                        "instrument_key", "timestamp", "last_price", "bid_price", "ask_price",
                        "volume", "open_interest", "open", "high", "low", "close", "previous_close",
                        "last_trade_time", "source", "trading_symbol", "expiry", "option_type",
                        "underlying_instrument_key", "response_timestamp", "quality_status",
                        "bid_quantity", "ask_quantity",
                    }
                }
                session.add(MarketQuoteSnapshot(**snapshot_fields))
                if instrument and instrument.get("instrument_type") in {"CE", "PE"}:
                    session.add(OptionSnapshot(
                        instrument_key=instrument["instrument_key"],
                        trading_symbol=instrument["trading_symbol"],
                        underlying_instrument_key=instrument.get("underlying_key"),
                        expiry=instrument["expiry"],
                        strike_price=instrument["strike_price"],
                        option_type=instrument["instrument_type"],
                        timestamp=quote["timestamp"],
                        response_timestamp=quote.get("response_timestamp"),
                        last_trade_time=quote.get("last_trade_time"),
                        quality_status=quote.get("quality_status", "VALID_FRESH"),
                        ltp=quote["last_price"],
                        bid_price=quote.get("bid_price"),
                        bid_quantity=quote.get("bid_quantity"),
                        ask_price=quote.get("ask_price"),
                        ask_quantity=quote.get("ask_quantity"),
                        volume=quote.get("volume"),
                        open_interest=quote.get("open_interest"),
                        implied_volatility=quote.get("implied_volatility"),
                        source=quote.get("source", "upstox"),
                    ))
                session.flush()
            return True
        except IntegrityError as exc:
            with self.session_factory() as session:
                duplicate = session.scalar(
                    select(MarketQuoteSnapshot.id).where(
                        MarketQuoteSnapshot.instrument_key == quote["instrument_key"],
                        MarketQuoteSnapshot.timestamp == quote["timestamp"],
                    )
                )
            if duplicate is not None:
                return False
            raise RepositoryIntegrityError(
                "Database rejected a market quote; transaction rolled back"
            ) from exc

    def add_collection_quality_event(
        self,
        *,
        collector: str,
        instrument_key: str | None,
        classification: str,
        reason: str,
        observed_at: datetime,
        response_timestamp: datetime | None = None,
    ) -> None:
        from database.models import CollectionQualityEvent

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Quality-event observed_at must be timezone-aware")
        if response_timestamp is not None and (
            response_timestamp.tzinfo is None or response_timestamp.utcoffset() is None
        ):
            raise ValueError("Quality-event response_timestamp must be timezone-aware")
        with self.session_factory.begin() as session:
            session.add(CollectionQualityEvent(
                collector=collector,
                instrument_key=instrument_key,
                classification=classification,
                reason=reason,
                observed_at=observed_at,
                response_timestamp=response_timestamp,
            ))

    def get_paper_trades(self, limit: int | None = None):
        with self.session_factory() as session:
            stmt = select(PaperTrade).order_by(PaperTrade.opened_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            return session.execute(stmt).scalars().all()

    def get_verified_instruments(self, *, instrument_type: str | None = None,
                                 contract_status: str | None = None) -> list[VerifiedInstrument]:
        with self.session_factory() as session:
            statement = select(VerifiedInstrument).order_by(
                VerifiedInstrument.expiry, VerifiedInstrument.trading_symbol
            )
            if instrument_type is not None:
                statement = statement.where(VerifiedInstrument.instrument_type == instrument_type)
            if contract_status is not None:
                statement = statement.where(VerifiedInstrument.contract_status == contract_status)
            return list(session.scalars(statement))

    def get_latest_market_quote(self, instrument_key: str) -> MarketQuoteSnapshot | None:
        with self.session_factory() as session:
            statement = (
                select(MarketQuoteSnapshot)
                .where(MarketQuoteSnapshot.instrument_key == instrument_key)
                .order_by(MarketQuoteSnapshot.timestamp.desc())
                .limit(1)
            )
            return session.scalar(statement)

    def data_quality_counts(self) -> dict:
        from sqlalchemy import func

        with self.session_factory() as session:
            count = lambda model: session.scalar(select(func.count()).select_from(model)) or 0
            instruments = count(VerifiedInstrument)
            futures = session.scalar(select(func.count()).select_from(VerifiedInstrument).where(
                VerifiedInstrument.instrument_type == "FUT"
            )) or 0
            calls = session.scalar(select(func.count()).select_from(VerifiedInstrument).where(
                VerifiedInstrument.instrument_type == "CE"
            )) or 0
            puts = session.scalar(select(func.count()).select_from(VerifiedInstrument).where(
                VerifiedInstrument.instrument_type == "PE"
            )) or 0
            active = session.scalar(select(func.count()).select_from(VerifiedInstrument).where(
                VerifiedInstrument.contract_status == "ACTIVE"
            )) or 0
            expired = session.scalar(select(func.count()).select_from(VerifiedInstrument).where(
                VerifiedInstrument.contract_status == "EXPIRED"
            )) or 0
            quote_duplicates = session.scalar(
                select(func.count()).select_from(
                    select(MarketQuoteSnapshot.instrument_key, MarketQuoteSnapshot.timestamp)
                    .group_by(MarketQuoteSnapshot.instrument_key, MarketQuoteSnapshot.timestamp)
                    .having(func.count() > 1).subquery()
                )
            ) or 0
            candle_duplicates = session.scalar(
                select(func.count()).select_from(
                    select(MarketCandle.instrument_key, MarketCandle.interval, MarketCandle.timestamp)
                    .group_by(MarketCandle.instrument_key, MarketCandle.interval, MarketCandle.timestamp)
                    .having(func.count() > 1).subquery()
                )
            ) or 0
            last_collection = max(
                (value for value in (
                    session.scalar(select(func.max(MarketCandle.created_at))),
                    session.scalar(select(func.max(MarketQuoteSnapshot.created_at))),
                    session.scalar(select(func.max(OptionSnapshot.created_at))),
                    session.scalar(select(func.max(VerifiedInstrument.verified_at))),
                ) if value is not None),
                default=None,
            )
            invalid_quotes = session.scalar(select(func.count()).select_from(MarketQuoteSnapshot).where(
                MarketQuoteSnapshot.last_price <= 0
            )) or 0
            invalid_candles = session.scalar(select(func.count()).select_from(MarketCandle).where(
                (MarketCandle.open <= 0) | (MarketCandle.high <= 0) |
                (MarketCandle.low <= 0) | (MarketCandle.close <= 0) |
                (MarketCandle.volume < 0)
            )) or 0
            return {
                "instruments": instruments,
                "active_instruments": active,
                "expired_instruments": expired,
                "futures": futures,
                "calls": calls,
                "puts": puts,
                "candles": count(MarketCandle),
                "option_snapshots": count(OptionSnapshot),
                "quote_snapshots": count(MarketQuoteSnapshot),
                "duplicate_records": candle_duplicates + quote_duplicates,
                "invalid_records": invalid_quotes + invalid_candles,
                "last_successful_collection": last_collection,
            }

    @staticmethod
    def _validate_candle(candle: dict) -> None:
        required = {
            "instrument_key", "exchange", "instrument_type", "interval", "timestamp",
            "open", "high", "low", "close", "volume", "source",
        }
        missing = sorted(required - candle.keys())
        if missing:
            raise ValueError(f"Market candle missing required fields: {missing}")
        if not all(candle[field] for field in ("instrument_key", "exchange", "interval", "source")):
            raise ValueError("Market candle instrument_key, exchange, interval, and source must not be empty")
        if candle["instrument_type"] not in {"FUT", "CE", "PE"}:
            raise ValueError(f"Unsupported market candle instrument_type: {candle['instrument_type']!r}")
        if not isinstance(candle["timestamp"], datetime) or candle["timestamp"].tzinfo is None:
            raise ValueError("Market candle timestamp must be timezone-aware")
        for field in ("open", "high", "low", "close", "volume"):
            value = candle[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Market candle {field} must be a finite number")
        if candle["volume"] < 0:
            raise ValueError("Market candle volume must be non-negative")
        if candle["low"] < 0 or candle["high"] < max(candle["open"], candle["close"], candle["low"]):
            raise ValueError("Market candle OHLC values are inconsistent")
        if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
            raise ValueError("Market candle OHLC values are inconsistent")

    @staticmethod
    def _normalize_quote(raw: dict) -> dict:
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, datetime):
            raise ValueError("Option snapshot timestamp must be a datetime")
        required = ("instrument_key", "trading_symbol", "expiry", "strike_price", "option_type", "ltp")
        missing = [field for field in required if raw.get(field) is None]
        if missing:
            raise ValueError(f"Option snapshot missing required fields: {missing}")
        try:
            option_type = str(raw["option_type"]).upper()
            strike_price = float(raw["strike_price"])
            ltp = float(raw["ltp"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Option snapshot strike_price and ltp must be numeric") from exc
        if not math.isfinite(strike_price) or not math.isfinite(ltp):
            raise ValueError("Option snapshot strike_price and ltp must be finite")
        expiry = str(raw["expiry"])

        return {
            "instrument_key": str(raw["instrument_key"]),
            "trading_symbol": str(raw["trading_symbol"]),
            "expiry": expiry,
            "strike_price": strike_price,
            "option_type": option_type,
            "timestamp": timestamp,
            "ltp": ltp,
            "bid_price": raw.get("bid_price"),
            "ask_price": raw.get("ask_price"),
            "volume": raw.get("volume"),
            "open_interest": raw.get("open_interest"),
            "implied_volatility": raw.get("implied_volatility"),
            "source": raw.get("source", "upstox"),
        }


def insert_market_candle(session: Session, candle: dict) -> bool:
    Repository._validate_candle(candle)
    session.add(MarketCandle(**candle))
    try:
        session.flush()
        session.commit()
        return True
    except IntegrityError as exc:
        session.rollback()
        duplicate = session.scalar(
            select(MarketCandle.id).where(
                MarketCandle.instrument_key == candle["instrument_key"],
                MarketCandle.interval == candle["interval"],
                MarketCandle.timestamp == candle["timestamp"],
            )
        )
        if duplicate is not None:
            return False
        raise RepositoryIntegrityError("Database rejected a market candle; transaction rolled back") from exc


def insert_option_snapshot(session: Session, snapshot: dict) -> bool:
    session.add(OptionSnapshot(**snapshot))
    try:
        session.flush()
        session.commit()
        return True
    except IntegrityError as exc:
        session.rollback()
        duplicate = session.scalar(
            select(OptionSnapshot.id).where(
                OptionSnapshot.instrument_key == snapshot["instrument_key"],
                OptionSnapshot.timestamp == snapshot["timestamp"],
            )
        )
        if duplicate is not None:
            return False
        raise RepositoryIntegrityError("Database rejected an option snapshot; transaction rolled back") from exc


def record_system_event(session: Session, event_type: str, message: str, severity: str = "INFO") -> None:
    session.add(SystemEvent(event_type=event_type, message=message, severity=severity))
    session.commit()
