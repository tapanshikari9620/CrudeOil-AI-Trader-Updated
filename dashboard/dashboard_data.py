"""Read-only data access helpers for the Streamlit dashboard."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select

from database.db_connection import SessionLocal
from config.settings import settings
from database.models import (
    CollectionQualityEvent,
    MarketCandle,
    MarketQuoteSnapshot,
    OptionSnapshot,
    PaperTrade,
    SystemEvent,
    VerifiedInstrument,
)

IST = ZoneInfo("Asia/Kolkata")


def utc_now():
    return datetime.now(timezone.utc)


def aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fmt_ist(value):
    value = aware_utc(value)
    return value.astimezone(IST).strftime("%d-%b-%Y %H:%M:%S") if value else "—"


def age_seconds(value, now=None):
    value = aware_utc(value)
    if value is None:
        return None
    return ((now or utc_now()) - value).total_seconds()


def latest_active_future(session):
    today = datetime.now(IST).date().isoformat()
    product = settings.mcx_product.upper().strip()

    if product not in {"CRUDEOIL", "CRUDEOILM"}:
        raise ValueError(
            f"Unsupported MCX product configured: {product}"
        )

    return session.scalar(
        select(VerifiedInstrument)
        .where(
            VerifiedInstrument.instrument_type == "FUT",
            VerifiedInstrument.contract_status == "ACTIVE",
            VerifiedInstrument.expiry >= today,
            VerifiedInstrument.underlying_symbol == product,
        )
        .order_by(
            VerifiedInstrument.expiry,
            VerifiedInstrument.trading_symbol,
        )
        .limit(1)
    )


def latest_quote(session, instrument_key):
    if not instrument_key:
        return None
    return session.scalar(
        select(MarketQuoteSnapshot)
        .where(MarketQuoteSnapshot.instrument_key == instrument_key)
        .order_by(MarketQuoteSnapshot.timestamp.desc())
        .limit(1)
    )


def latest_futures_quote(session):
    future = latest_active_future(session)
    if future:
        quote = latest_quote(session, future.instrument_key)
        if quote:
            return future, quote
    quote = session.scalar(
        select(MarketQuoteSnapshot)
        .where(
            MarketQuoteSnapshot.option_type.is_(None),
            MarketQuoteSnapshot.last_price > 0,
        )
        .order_by(MarketQuoteSnapshot.timestamp.desc())
        .limit(1)
    )
    return future, quote


def load_recent_candles(instrument_key, interval="5_min", limit=300):
    if not instrument_key:
        return pd.DataFrame()
    with SessionLocal() as session:
        rows = list(session.scalars(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_key == instrument_key,
                MarketCandle.interval == interval,
            )
            .order_by(MarketCandle.timestamp.desc())
            .limit(limit)
        ))
    rows.reverse()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "timestamp": aware_utc(row.timestamp),
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
        "open_interest": row.open_interest,
    } for row in rows])


def get_trade_rows(limit=500):
    with SessionLocal() as session:
        rows = list(session.scalars(
            select(PaperTrade).order_by(PaperTrade.opened_at.desc()).limit(limit)
        ))
    return rows


def trades_frame(trades):
    return pd.DataFrame([{
        "Trade ID": trade.id,
        "Status": trade.status,
        "Instrument": trade.instrument_key,
        "Option": trade.option_type or "—",
        "Quantity": trade.quantity,
        "Entry": trade.entry_price,
        "Exit": trade.exit_price,
        "Stop loss": trade.stop_loss,
        "Target": trade.target,
        "Gross P&L": trade.gross_pnl,
        "Charges": trade.charges,
        "Net P&L": trade.net_pnl,
        "Unrealized P&L": trade.unrealized_pnl,
        "Exit reason": trade.exit_reason or "—",
        "Opened (IST)": fmt_ist(trade.opened_at),
        "Closed (IST)": fmt_ist(trade.closed_at),
        "Last quote (IST)": fmt_ist(trade.last_quote_at),
    } for trade in trades])


def dashboard_counts(session):
    count = lambda model: session.scalar(select(func.count()).select_from(model)) or 0
    return {
        "candles": count(MarketCandle),
        "market_quotes": count(MarketQuoteSnapshot),
        "option_snapshots": count(OptionSnapshot),
        "verified_instruments": count(VerifiedInstrument),
        "paper_trades": count(PaperTrade),
    }


def get_quality_summary(hours=24):
    cutoff = utc_now() - timedelta(hours=hours)
    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(CollectionQualityEvent)) or 0
        recent = list(session.execute(
            select(CollectionQualityEvent.classification, func.count())
            .where(CollectionQualityEvent.observed_at >= cutoff)
            .group_by(CollectionQualityEvent.classification)
        ))
        latest_events = list(session.scalars(
            select(CollectionQualityEvent)
            .order_by(CollectionQualityEvent.observed_at.desc())
            .limit(100)
        ))
        by_collector = list(session.execute(
            select(CollectionQualityEvent.collector, CollectionQualityEvent.classification, func.count())
            .where(CollectionQualityEvent.observed_at >= cutoff)
            .group_by(CollectionQualityEvent.collector, CollectionQualityEvent.classification)
        ))
        counts = dashboard_counts(session)
        last_quote = session.scalar(select(func.max(MarketQuoteSnapshot.timestamp)))
        last_candle = session.scalar(select(func.max(MarketCandle.timestamp)))
        instrument_status = list(session.execute(
            select(VerifiedInstrument.instrument_type, VerifiedInstrument.contract_status, func.count())
            .group_by(VerifiedInstrument.instrument_type, VerifiedInstrument.contract_status)
        ))
    return {
        "total_events": total,
        "recent": recent,
        "events": latest_events,
        "by_collector": by_collector,
        "counts": counts,
        "last_quote": last_quote,
        "last_candle": last_candle,
        "instrument_status": instrument_status,
    }


def get_latest_system_events(limit=50):
    with SessionLocal() as session:
        return list(session.scalars(
            select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)
        ))


def get_latest_option_snapshots(future_key=None, expiry=None, limit=300):
    today = datetime.now(IST).date().isoformat()
    with SessionLocal() as session:
        stmt = select(OptionSnapshot).where(
            OptionSnapshot.quality_status == "VALID_FRESH",
            OptionSnapshot.expiry >= today,
        )
        if future_key:
            stmt = stmt.where(OptionSnapshot.underlying_instrument_key == future_key)
        if expiry:
            stmt = stmt.where(OptionSnapshot.expiry == expiry)
        # Latest timestamp per instrument; keeps the displayed chain from mixing old rows.
        latest = (
            select(
                OptionSnapshot.instrument_key.label("key"),
                func.max(OptionSnapshot.timestamp).label("latest_ts"),
            )
            .where(
                OptionSnapshot.quality_status == "VALID_FRESH",
                OptionSnapshot.expiry >= today,
            )
            .group_by(OptionSnapshot.instrument_key)
            .subquery()
        )
        stmt = select(OptionSnapshot).join(
            latest,
            (OptionSnapshot.instrument_key == latest.c.key)
            & (OptionSnapshot.timestamp == latest.c.latest_ts),
        ).where(OptionSnapshot.quality_status == "VALID_FRESH", OptionSnapshot.expiry >= today)
        if future_key:
            stmt = stmt.where(OptionSnapshot.underlying_instrument_key == future_key)
        if expiry:
            stmt = stmt.where(OptionSnapshot.expiry == expiry)
        stmt = stmt.order_by(OptionSnapshot.expiry, OptionSnapshot.strike_price, OptionSnapshot.option_type).limit(limit)
        return list(session.scalars(stmt))


def get_option_expiries(future_key=None):
    today = datetime.now(IST).date().isoformat()
    with SessionLocal() as session:
        stmt = select(OptionSnapshot.expiry).where(
            OptionSnapshot.quality_status == "VALID_FRESH",
            OptionSnapshot.expiry >= today,
        ).distinct().order_by(OptionSnapshot.expiry)
        if future_key:
            stmt = stmt.where(OptionSnapshot.underlying_instrument_key == future_key)
        return list(session.scalars(stmt))


def get_signal_snapshot():
    """Calculate a read-only snapshot using the project's existing strategy code."""
    with SessionLocal() as session:
        future = latest_active_future(session)
    if not future:
        return {"available": False, "reason": "No ACTIVE verified MCX crude futures contract is stored."}
    candles = load_recent_candles(future.instrument_key, "5_min", 300)
    if len(candles) < 60:
        return {"available": False, "reason": f"Only {len(candles)} candles available; at least 60 are required."}
    now = utc_now()
    latest_time = aware_utc(candles.iloc[-1]["timestamp"])
    candle_age = (now - (latest_time + timedelta(minutes=5))).total_seconds()
    if candle_age < 0:
        return {"available": False, "reason": "Latest five-minute candle may still be forming."}
    if candle_age > 600:
        return {"available": False, "reason": f"Latest completed candle is stale ({candle_age:.0f} seconds old)."}
    try:
        from strategy.features import build_features
        from strategy.signal_engine import generate_signal
        from strategy.entry_timing_engine import evaluate_entry_timing
        featured = build_features(candles)
        latest = featured.iloc[-1]
        row = latest.to_dict()
        signal = generate_signal(row)
        timing = evaluate_entry_timing(signal, featured)
        return {
            "available": True,
            "future": future,
            "timestamp": latest_time,
            "age_seconds": candle_age,
            "signal": signal,
            "timing": timing,
            "indicators": row,
        }
    except Exception as exc:
        return {"available": False, "reason": f"Strategy calculation unavailable: {type(exc).__name__}: {exc}"}


def get_performance():
    trades = [trade for trade in get_trade_rows(5000) if trade.status == "CLOSED"]
    pnl_values = [float(t.net_pnl or 0.0) for t in sorted(trades, key=lambda x: aware_utc(x.closed_at or x.opened_at))]
    winners = [v for v in pnl_values if v > 0]
    losers = [v for v in pnl_values if v < 0]
    equity = []
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnl_values:
        running += value
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)
        equity.append(running)
    gross_loss = abs(sum(losers))
    return {
        "trades": trades,
        "total": len(pnl_values),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": (len(winners) / len(pnl_values) * 100) if pnl_values else 0.0,
        "net": sum(pnl_values),
        "gross_profit": sum(winners),
        "gross_loss": gross_loss,
        "profit_factor": (sum(winners) / gross_loss) if gross_loss else (float("inf") if winners else 0.0),
        "max_drawdown": max_drawdown,
        "equity": equity,
        "closed": sorted(trades, key=lambda x: aware_utc(x.closed_at or x.opened_at)),
    }
