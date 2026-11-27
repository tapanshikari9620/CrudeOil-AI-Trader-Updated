
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.db_connection import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key",
            "interval",
            "timestamp",
            name="uq_candle_instrument_interval_time",
        ),
        Index("ix_candle_instrument_timestamp", "instrument_key", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(150), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), default="MCX", nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(30), nullable=False)
    interval: Mapped[str] = mapped_column(String(10), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="upstox", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class CandleTrainingExclusion(Base):
    """
    Audit record for candles excluded from AI training and backtesting.

    Original market candle data is never modified or deleted.
    """

    __tablename__ = "candle_training_exclusions"

    candle_id: Mapped[int] = mapped_column(
        ForeignKey("market_candles.id"),
        primary_key=True,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class OptionSnapshot(Base):
    __tablename__ = "option_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key",
            "timestamp",
            name="uq_option_instrument_snapshot_time",
        ),
        Index("ix_option_timestamp", "timestamp"),
        Index("ix_option_instrument_timestamp", "instrument_key", "timestamp"),
        Index("ix_option_expiry_type", "expiry", "option_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(150), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    underlying_instrument_key: Mapped[str | None] = mapped_column(String(150))
    expiry: Mapped[str] = mapped_column(String(20), nullable=False)
    strike_price: Mapped[float] = mapped_column(Float, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trade_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_status: Mapped[str] = mapped_column(
        String(20), default="VALID_FRESH", nullable=False
    )
    ltp: Mapped[float | None] = mapped_column(Float)
    bid_price: Mapped[float | None] = mapped_column(Float)
    bid_quantity: Mapped[float | None] = mapped_column(Float)
    ask_price: Mapped[float | None] = mapped_column(Float)
    ask_quantity: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    implied_volatility: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), default="upstox", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class VerifiedInstrument(Base):
    __tablename__ = "verified_instruments"

    instrument_key: Mapped[str] = mapped_column(String(150), primary_key=True)
    trading_symbol: Mapped[str] = mapped_column(String(120), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    segment: Mapped[str] = mapped_column(String(30), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(10), nullable=False)
    expiry: Mapped[str] = mapped_column(String(20), nullable=False)
    strike_price: Mapped[float] = mapped_column(Float, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    underlying_key: Mapped[str | None] = mapped_column(String(150), nullable=True)
    underlying_symbol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    contract_status: Mapped[str] = mapped_column(
        String(20), default="UNKNOWN", nullable=False
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RawInstrumentMetadata(Base):
    __tablename__ = "raw_instrument_metadata"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key",
            "payload_hash",
            name="uq_raw_instrument_payload",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MarketQuoteSnapshot(Base):
    __tablename__ = "market_quote_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key",
            "timestamp",
            name="uq_market_quote_key_timestamp",
        ),
        Index("ix_market_quote_timestamp", "timestamp"),
        Index("ix_market_quote_instrument_timestamp", "instrument_key", "timestamp"),
        Index("ix_market_quote_expiry", "expiry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(150), nullable=False)
    trading_symbol: Mapped[str | None] = mapped_column(String(120))
    expiry: Mapped[str | None] = mapped_column(String(20))
    option_type: Mapped[str | None] = mapped_column(String(2))
    underlying_instrument_key: Mapped[str | None] = mapped_column(String(150))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_status: Mapped[str] = mapped_column(
        String(20), default="VALID_FRESH", nullable=False
    )
    last_price: Mapped[float] = mapped_column(Float, nullable=False)
    bid_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_trade_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(30), default="upstox", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CollectionQualityEvent(Base):
    __tablename__ = "collection_quality_events"
    __table_args__ = (
        Index("ix_collection_quality_instrument_time", "instrument_key", "observed_at"),
        Index("ix_collection_quality_time", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collector: Mapped[str] = mapped_column(String(20), nullable=False)
    instrument_key: Mapped[str | None] = mapped_column(String(150))
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    __table_args__ = (
        Index(
            "uq_open_paper_trade_instrument",
            "instrument_key",
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
        Index(
            "uq_single_open_paper_trade",
            text("1"),
            unique=True,
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(150), nullable=False)
    side: Mapped[str] = mapped_column(String(10), default="BUY", nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    charges: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    last_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
