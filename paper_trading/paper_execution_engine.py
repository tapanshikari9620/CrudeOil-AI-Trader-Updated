from __future__ import annotations

import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from database.db_connection import SessionLocal
from database.models import PaperTrade, VerifiedInstrument
from paper_trading.paper_broker import PaperBroker, PaperFill
from risk_management.stoploss import trailing_stop_from_peak


MARKET_TZ = ZoneInfo("Asia/Kolkata")


class DuplicateOpenPosition(ValueError):
    """Raised when a paper position already exists for an option instrument."""


class PositionAlreadyClosed(ValueError):
    """Raised when another exit path has already claimed the open position."""


class PaperExecutionEngine:
    """Manage simulated long-option orders and quote-driven position exits."""

    def __init__(self, broker: PaperBroker | None = None, session_factory=None):
        self.broker = broker or PaperBroker()
        self.session_factory = session_factory or SessionLocal

    @staticmethod
    def _validate_verified_option(
        session,
        instrument_key: str,
        quantity: int,
        option_type: str,
    ) -> VerifiedInstrument:
        """Fail closed unless the exact contract is verified, active and one lot."""
        instrument = session.get(VerifiedInstrument, instrument_key)

        if instrument is None:
            raise ValueError("Unverified option contract")

        if instrument.exchange != "MCX":
            raise ValueError("Option contract must belong to MCX")

        if instrument.segment != "MCX_FO":
            raise ValueError("Option contract must belong to MCX_FO")

        if instrument.contract_status != "ACTIVE":
            raise ValueError("Option contract is not ACTIVE")

        if instrument.instrument_type != option_type:
            raise ValueError("Verified contract option type mismatch")

        try:
            expiry = date.fromisoformat(str(instrument.expiry)[:10])
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid verified contract expiry") from exc

        # Use the actual MCX-local date, not the caller-supplied trade timestamp.
        # A stale/historical timestamp must never make an expired contract tradable.
        market_date = datetime.now(MARKET_TZ).date()
        if expiry < market_date:
            raise ValueError("Option contract has expired")

        lot_size = instrument.lot_size
        if (
            isinstance(lot_size, bool)
            or not isinstance(lot_size, int)
            or lot_size <= 0
        ):
            raise ValueError("Invalid verified contract lot size")

        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity != lot_size
        ):
            raise ValueError("Paper quantity must equal exactly one verified lot")

        return instrument

    def open_long_option(
        self,
        instrument_key: str,
        quantity: int,
        bid: float,
        ask: float,
        tick_size: float,
        stop_loss: float,
        target: float,
        timestamp: datetime | None = None,
        *,
        option_type: str | None = None,
    ) -> int:
        """Create a paper-only long option position; never calls a broker API."""
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            raise ValueError("instrument_key is required")

        if option_type not in {"CE", "PE"}:
            raise ValueError(
                "Verified option_type CE or PE is required for a paper option position"
            )

        opened_at = timestamp or datetime.now(timezone.utc)
        self._require_aware(opened_at, "trade timestamp")

        if (
            isinstance(stop_loss, bool)
            or isinstance(target, bool)
            or not isinstance(stop_loss, (int, float))
            or not isinstance(target, (int, float))
            or not math.isfinite(stop_loss)
            or not math.isfinite(target)
            or stop_loss <= 0
            or target <= stop_loss
        ):
            raise ValueError("Require finite positive stop_loss below target")

        try:
            with self.session_factory.begin() as session:
                # Metadata and quantity checks happen before any simulated fill.
                self._validate_verified_option(
                    session=session,
                    instrument_key=instrument_key,
                    quantity=quantity,
                    option_type=option_type,
                )

                # Global limit: at most one OPEN paper position across all contracts.
                existing = session.scalar(
                    select(PaperTrade.id).where(PaperTrade.status == "OPEN").limit(1)
                )
                if existing is not None:
                    raise DuplicateOpenPosition(
                        "A global open paper position already exists"
                    )

                # This is a local PAPER fill only; no broker order API is called.
                fill = self.broker.market_fill(
                    instrument_key,
                    "BUY",
                    quantity,
                    bid,
                    ask,
                    tick_size,
                    opened_at,
                )

                if not stop_loss < fill.price < target:
                    raise ValueError(
                        "Paper fill must be above stop_loss and below target"
                    )

                trade = PaperTrade(
                    instrument_key=instrument_key,
                    option_type=option_type,
                    side="BUY",
                    quantity=quantity,
                    entry_price=fill.price,
                    stop_loss=stop_loss,
                    initial_stop_loss=stop_loss,
                    peak_mark_price=fill.price,
                    target=target,
                    gross_pnl=0.0,
                    charges=0.0,
                    net_pnl=0.0,
                    status="OPEN",
                    notes="PAPER_ONLY",
                    opened_at=opened_at,
                )
                session.add(trade)
                session.flush()
                trade_id = trade.id

        except IntegrityError as exc:
            # The database's global partial unique index is the concurrency guard.
            with self.session_factory() as session:
                existing = session.scalar(
                    select(PaperTrade.id).where(PaperTrade.status == "OPEN").limit(1)
                )
            if existing is not None:
                raise DuplicateOpenPosition(
                    "A global open paper position already exists"
                ) from exc
            raise

        return trade_id

    def close_trade(
            self,
            trade_id: int,
            bid: float,
            ask: float,
            tick_size: float,
            charges: float = 0.0,
            timestamp: datetime | None = None,
            *,
            exit_reason: str = "MANUAL",
    ) -> float:
        """Close one open position with a simulated sell fill."""
        closed_at = timestamp or datetime.now(timezone.utc)
        self._require_aware(closed_at, "close timestamp")
        self._validate_charges(charges)
        with self.session_factory.begin() as session:
            trade = session.get(PaperTrade, trade_id)
            if trade is None or trade.status != "OPEN":
                raise ValueError("Open paper trade not found")
            fill = self.broker.market_fill(
                trade.instrument_key, "SELL", trade.quantity, bid, ask, tick_size, closed_at
            )
            return self._close_with_fill(session, trade, fill, charges, exit_reason)

    def close_trade_from_quote(
            self,
            trade_id: int,
            quote: dict,
            *,
            now: datetime | None = None,
            max_age_seconds: float = 5.0,
            charges: float = 0.0,
            exit_reason: str = "MANUAL",
    ) -> float:
        """Manually close from a fresh quote for the exact open option contract."""
        normalized = self._validate_quote(quote, now, max_age_seconds)
        self._validate_charges(charges)
        with self.session_factory() as session:
            trade = session.get(PaperTrade, trade_id)
            if trade is None or trade.status != "OPEN":
                raise ValueError("Open paper trade not found")
            if trade.instrument_key != normalized["instrument_key"]:
                raise ValueError("Manual-exit quote does not match the open trade instrument")
            if trade.option_type and trade.option_type != normalized["option_type"]:
                raise ValueError("Manual-exit quote option type does not match the open trade")
        return self.close_trade(
            trade_id,
            normalized["bid_price"],
            normalized["ask_price"],
            normalized["tick_size"],
            charges=charges,
            timestamp=normalized["timestamp"],
            exit_reason=exit_reason,
        )

    def process_quote(
            self,
            quote: dict,
            *,
            now: datetime | None = None,
            max_age_seconds: float = 5.0,
            charges: float = 0.0,
    ) -> list[dict]:
        """Mark matching option positions and simulate exits from one fresh quote.

        The quote must identify a CE/PE instrument and contain its actual option
        bid, ask, last price, tick size, and aware timestamp. Barriers use the
        executable option bid, not the last price or underlying future. Stop-loss wins if
        both barriers could be inferred from ambiguous historical candle data;
        this quote handler itself receives a single sequenced market snapshot.
        """
        normalized = self._validate_quote(quote, now, max_age_seconds)
        self._validate_charges(charges)
        outcomes = []
        with self.session_factory.begin() as session:
            positions = session.execute(
                select(PaperTrade).where(
                    PaperTrade.instrument_key == normalized["instrument_key"],
                    PaperTrade.status == "OPEN",
                )
            ).scalars().all()
            for trade in positions:
                if trade.option_type and trade.option_type != normalized["option_type"]:
                    raise ValueError("Quote option type does not match the open paper position")
                if trade.last_quote_at is not None:
                    previous = self._as_utc(trade.last_quote_at)
                    if normalized["timestamp"] <= previous:
                        raise ValueError("Out-of-order quote timestamp for open paper position")

                fill = self.broker.market_fill(
                    trade.instrument_key,
                    "SELL",
                    trade.quantity,
                    normalized["bid_price"],
                    normalized["ask_price"],
                    normalized["tick_size"],
                    normalized["timestamp"],
                )
                # Track the best executable bid and ratchet the stop only upward.
                prior_peak = trade.peak_mark_price or trade.entry_price
                previous_stop = float(trade.stop_loss)
                trade.peak_mark_price = max(float(prior_peak), normalized["bid_price"])
                initial_stop = trade.initial_stop_loss or trade.stop_loss
                trade.stop_loss = trailing_stop_from_peak(
                    entry=float(trade.entry_price),
                    initial_stop=float(initial_stop),
                    current_stop=float(trade.stop_loss),
                    peak_bid=float(trade.peak_mark_price),
                    tick_size=float(normalized["tick_size"]),
                )
                trade.last_mark_price = fill.price
                trade.unrealized_pnl = (fill.price - trade.entry_price) * trade.quantity
                trade.last_quote_at = normalized["timestamp"]

                reason = None
                if normalized["bid_price"] <= trade.stop_loss:
                    reason = "TRAILING_STOP" if trade.stop_loss > float(initial_stop) else "STOP_LOSS"
                elif normalized["bid_price"] >= trade.target:
                    reason = "TARGET"

                if reason is not None:
                    try:
                        net_pnl = self._close_with_fill(session, trade, fill, charges, reason)
                    except PositionAlreadyClosed:
                        # Another controller or a manual exit won the atomic close claim.
                        # Discard quote-mark mutations staged on this ORM object so a
                        # losing exit cannot overwrite fields on the already-closed row.
                        session.expire(trade)
                        continue
                    outcomes.append({
                        "trade_id": trade.id,
                        "status": "CLOSED",
                        "exit_reason": reason,
                        "exit_price": fill.price,
                        "gross_pnl": trade.gross_pnl,
                        "charges": trade.charges,
                        "net_pnl": net_pnl,
                    })
                else:
                    outcomes.append({
                        "trade_id": trade.id,
                        "status": "OPEN",
                        "unrealized_pnl": trade.unrealized_pnl,
                        "mark_price": trade.last_mark_price,
                        "stop_loss": trade.stop_loss,
                        "target": trade.target,
                        "peak_mark_price": trade.peak_mark_price,
                        "previous_stop_loss": previous_stop,
                        "stop_updated": trade.stop_loss > previous_stop,
                    })
        return outcomes

    @staticmethod
    def _close_with_fill(session, trade: PaperTrade, fill: PaperFill, charges: float, reason: str) -> float:
        # Conditional UPDATE is the single-winner guard shared by manual exits,
        # automatic exits, and multiple controller processes.
        claimed = session.execute(
            update(PaperTrade)
            .where(PaperTrade.id == trade.id, PaperTrade.status == "OPEN")
            .values(status="CLOSING")
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise PositionAlreadyClosed("Paper position was already closed by another exit request")

        gross_pnl = (fill.price - trade.entry_price) * trade.quantity
        net_pnl = gross_pnl - charges
        trade.exit_price = fill.price
        trade.gross_pnl = gross_pnl
        trade.charges = charges
        trade.net_pnl = net_pnl
        trade.unrealized_pnl = 0.0
        trade.last_mark_price = fill.price
        trade.last_quote_at = fill.timestamp
        trade.status = "CLOSED"
        trade.closed_at = fill.timestamp
        trade.exit_reason = reason
        session.flush()
        return net_pnl

    @classmethod
    def _validate_quote(cls, quote: dict, now: datetime | None, max_age_seconds: float) -> dict:
        if not isinstance(quote, dict):
            raise ValueError("Option quote must be a mapping")
        required = ("instrument_key", "timestamp", "bid_price", "ask_price", "ltp", "tick_size")
        missing = [key for key in required if quote.get(key) is None]
        if missing:
            raise ValueError(f"Option quote missing required fields: {missing}")
        option_type = quote.get("option_type", quote.get("instrument_type"))
        if option_type not in {"CE", "PE"}:
            raise ValueError("Quote must identify an option instrument_type of CE or PE")
        timestamp = quote["timestamp"]
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        cls._require_aware(timestamp, "quote timestamp")
        current = now or datetime.now(timezone.utc)
        cls._require_aware(current, "current timestamp")
        timestamp = cls._as_utc(timestamp)
        current = cls._as_utc(current)
        if max_age_seconds < 0 or not math.isfinite(max_age_seconds):
            raise ValueError("max_age_seconds must be finite and non-negative")
        age_seconds = (current - timestamp).total_seconds()
        if age_seconds < 0:
            raise ValueError("Quote timestamp is in the future")
        if age_seconds > max_age_seconds:
            raise ValueError(f"Stale option quote ({age_seconds:.3f}s old; maximum {max_age_seconds:.3f}s)")

        numeric = {}
        for field in ("bid_price", "ask_price", "ltp", "tick_size"):
            value = quote[field]
            if isinstance(value, bool):
                raise ValueError(f"Quote {field} must be a finite positive number")
            try:
                numeric[field] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Quote {field} must be a finite positive number") from exc
            if not math.isfinite(numeric[field]) or numeric[field] <= 0:
                raise ValueError(f"Quote {field} must be a finite positive number")
        if numeric["ask_price"] < numeric["bid_price"]:
            raise ValueError("Invalid option quote: ask_price is below bid_price")
        instrument_key = quote["instrument_key"]
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            raise ValueError("Quote instrument_key must not be empty")
        return {**numeric, "instrument_key": instrument_key, "option_type": option_type, "timestamp": timestamp}

    @staticmethod
    def _validate_charges(charges: float) -> None:
        if isinstance(charges, bool) or not isinstance(charges, (int, float)):
            raise ValueError("charges must be a finite non-negative number")
        if not math.isfinite(charges) or charges < 0:
            raise ValueError("charges must be a finite non-negative number")

    @staticmethod
    def _require_aware(value: datetime, label: str) -> None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class PaperPositionMonitor:
    """Explicit event handler suitable for a separately managed quote stream."""

    def __init__(self, execution_engine: PaperExecutionEngine, max_age_seconds: float = 5.0):
        self.execution_engine = execution_engine
        self.max_age_seconds = max_age_seconds

    def process_quote(self, quote: dict, *, now: datetime | None = None, charges: float = 0.0) -> list[dict]:
        return self.execution_engine.process_quote(
            quote,
            now=now,
            max_age_seconds=self.max_age_seconds,
            charges=charges,
        )
