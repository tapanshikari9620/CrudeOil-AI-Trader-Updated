from __future__ import annotations

import json
from datetime import datetime, timezone
import sys
from pathlib import Path

import streamlit as st
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from dashboard.dashboard_data import (
    IST, age_seconds, fmt_ist, get_quality_summary, get_signal_snapshot,
    get_trade_rows, latest_futures_quote, trades_frame,
)
from database.db_connection import SessionLocal
from database.models import MarketQuoteSnapshot, PaperTrade, VerifiedInstrument
from paper_trading.paper_execution_engine import PaperExecutionEngine

st.set_page_config(page_title="Crude Oil AI Trader", page_icon="🛢️", layout="wide")
st.title("🛢️ Crude Oil AI Trader")
st.caption("MCX Crude Oil • PAPER mode • Read-only market data with local simulated exits")
st.markdown(
    """
    <style>
      .stApp { background: #0B1220; }
      [data-testid="stSidebar"] { background: #0E1728; border-right: 1px solid #263449; }
      [data-testid="stMetric"] { background: linear-gradient(145deg, #142238, #101A2B); border: 1px solid #263B55; padding: 14px 16px; border-radius: 12px; }
      [data-testid="stMetricLabel"] { color: #9FB0C5; }
      [data-testid="stMetricValue"] { color: #F3F6FA; }
      div[data-testid="stAlert"] { border-radius: 10px; }
      h1, h2, h3 { letter-spacing: -0.02em; }
      div[data-testid="stDataFrame"] { border: 1px solid #263449; border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.warning("PAPER MODE ONLY — manual exit creates a simulated local SELL fill. No Upstox order endpoint is called.")

refresh = st.sidebar.selectbox("Auto-refresh (seconds)", [3, 5, 10, 15, 30], index=1)
st.sidebar.caption("Market data is read from the database. Manual exit is local PAPER simulation only.")
if settings.trading_mode != "PAPER":
    st.error("Configuration is not PAPER. Manual exits are disabled; check TRADING_MODE in .env.")


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@st.fragment(run_every=refresh)
def live_dashboard():
    try:
        with SessionLocal() as session:
            future, futures_quote = latest_futures_quote(session)
            trades = get_trade_rows(1000)
            opened = [t for t in trades if t.status == "OPEN"]
            today = datetime.now(IST).date()
            todays = [t for t in trades if t.opened_at and _utc(t.opened_at).astimezone(IST).date() == today]
            realized = sum(float(t.net_pnl or 0) for t in todays if t.status == "CLOSED")
            unrealized = sum(float(t.unrealized_pnl or 0) for t in opened)
            quality = get_quality_summary()
            open_details = []
            for trade in opened:
                instrument = session.get(VerifiedInstrument, trade.instrument_key)
                quote = session.scalar(
                    select(MarketQuoteSnapshot)
                    .where(MarketQuoteSnapshot.instrument_key == trade.instrument_key)
                    .order_by(MarketQuoteSnapshot.timestamp.desc())
                    .limit(1)
                )
                open_details.append((trade, instrument, quote))
    except Exception as exc:
        st.error("Unable to read the database. Confirm the virtual environment and database path.")
        st.exception(exc)
        return

    cols = st.columns(4)
    if futures_quote:
        quote_age = age_seconds(futures_quote.last_trade_time)
        cols[0].metric("Crude futures LTP", f"₹{futures_quote.last_price:,.2f}")
        cols[1].metric("Last trade age", f"{quote_age:.0f}s" if quote_age is not None and quote_age >= 0 else "Unknown")
    else:
        cols[0].metric("Crude futures LTP", "Unavailable")
        cols[1].metric("Last trade age", "—")
    cols[2].metric("Open PAPER positions", len(opened))
    cols[3].metric("Today's realized P&L", f"₹{realized:,.2f}")

    cols = st.columns(3)
    cols[0].metric("Today's trades", len(todays))
    cols[1].metric("Open-position P&L", f"₹{unrealized:,.2f}")
    cols[2].metric("Stored quote snapshots", f"{quality['counts']['market_quotes']:,}")

    if future:
        st.caption(f"Selected futures: {future.trading_symbol} · {future.instrument_key} · expiry {future.expiry}")
    if futures_quote:
        st.caption(f"Last quote observed: {fmt_ist(futures_quote.timestamp)} · Exchange trade time: {fmt_ist(futures_quote.last_trade_time)} · quality {futures_quote.quality_status}")

    st.subheader("Current strategy snapshot")
    snapshot = get_signal_snapshot()
    if snapshot["available"]:
        signal = snapshot["signal"]
        if signal.valid:
            st.success(f"{signal.direction} — {signal.option_type}")
        else:
            st.info(f"{signal.direction} — no entry")
        st.caption(f"Based on completed 5-minute candle {fmt_ist(snapshot['timestamp'])}; candle age {snapshot['age_seconds']:.0f}s.")
        st.write("Reason:", ", ".join(signal.reasons))
    else:
        st.info(snapshot["reason"])

    st.subheader("Open PAPER positions")
    if not open_details:
        st.info("No open PAPER positions.")
    else:
        for trade, instrument, quote in open_details:
            with st.container(border=True):
                symbol = instrument.trading_symbol if instrument else trade.instrument_key
                strike = getattr(instrument, "strike_price", None) if instrument else None
                expiry = getattr(instrument, "expiry", None) if instrument else None
                option_type = (instrument.instrument_type if instrument else trade.option_type) or "—"
                st.markdown(f"**{symbol}** · {option_type} · Trade #{trade.id}")
                st.caption(f"Instrument: {trade.instrument_key} · Strike: {strike if strike is not None else '—'} · Expiry: {expiry or '—'}")
                current_price = quote.last_price if quote else None
                quote_time = _utc(quote.response_timestamp or quote.timestamp) if quote else None
                quote_age = (datetime.now(timezone.utc) - quote_time).total_seconds() if quote_time else None
                c = st.columns(4)
                c[0].metric("Entry price", f"₹{trade.entry_price:,.2f}")
                c[1].metric("Current option LTP", f"₹{current_price:,.2f}" if current_price else "Unavailable")
                c[2].metric("Quantity", str(trade.quantity))
                c[3].metric("Unrealized P&L", f"₹{float(trade.unrealized_pnl or 0):,.2f}")
                c = st.columns(3)
                c[0].metric("Stop loss", f"₹{trade.stop_loss:,.2f}")
                c[1].metric("Target", f"₹{trade.target:,.2f}")
                c[2].metric("Quote age", f"{quote_age:.1f}s" if quote_age is not None and quote_age >= 0 else "Unknown")
                st.caption(f"Status: {trade.status} · Last quote: {fmt_ist(quote_time)} · Quote quality: {quote.quality_status if quote else 'Unavailable'}")

                stored_tick = float(instrument.tick_size or 0) if instrument is not None else 0.0
                crude_tick_metadata_needs_refresh = bool(
                    instrument is not None
                    and instrument.instrument_type in {"CE", "PE"}
                    and str(instrument.underlying_symbol or "").upper() == "CRUDEOIL"
                    and int(instrument.lot_size or 0) == 100
                    and abs(stored_tick - 10.0) < 1e-9
                )
                safe_quote = bool(
                    settings.trading_mode == "PAPER"
                    and instrument is not None
                    and not crude_tick_metadata_needs_refresh
                    and quote is not None
                    and quote.quality_status == "VALID_FRESH"
                    and quote.bid_price is not None and quote.ask_price is not None
                    and quote.last_price is not None
                    and quote_time is not None and quote_age is not None
                    and 0 <= quote_age <= 30
                    and float(instrument.tick_size or 0) > 0
                    and float(quote.bid_price) > 0
                    and float(quote.ask_price) >= float(quote.bid_price)
                )
                confirm = st.checkbox("Confirm simulated full-position exit", key=f"confirm_exit_{trade.id}")
                if st.button(
                    f"Manual Exit — Trade #{trade.id}",
                    key=f"manual_exit_{trade.id}",
                    type="primary",
                    disabled=not (safe_quote and confirm),
                    use_container_width=True,
                ):
                    try:
                        exit_quote = {
                            "instrument_key": trade.instrument_key,
                            "option_type": option_type,
                            "timestamp": quote_time,
                            "bid_price": float(quote.bid_price),
                            "ask_price": float(quote.ask_price),
                            "ltp": float(quote.last_price),
                            "tick_size": float(instrument.tick_size),
                        }
                        pnl = PaperExecutionEngine().close_trade_from_quote(
                            trade.id,
                            exit_quote,
                            now=datetime.now(timezone.utc),
                            max_age_seconds=30.0,
                            charges=0.0,
                            exit_reason="MANUAL",
                        )
                        st.success(f"PAPER position #{trade.id} closed. Realized P&L before charges: ₹{pnl:,.2f}")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Manual PAPER exit was not completed: {exc}")
                if crude_tick_metadata_needs_refresh:
                    st.error("Manual exit disabled: this stored Crude Oil option still has the known unnormalized tick size (10.0). Refresh verified contract metadata before enabling simulated fills.")
                elif not safe_quote:
                    st.caption("Manual exit disabled: verified contract metadata or a fresh, valid option quote is unavailable.")

    st.subheader("Recent PAPER trades")
    if trades:
        st.dataframe(trades_frame(trades[:20]), use_container_width=True, hide_index=True)
    else:
        st.info("No PAPER trades recorded yet.")

    st.subheader("System and data status")
    a, b, c, d = st.columns(4)
    a.metric("Candles", f"{quality['counts']['candles']:,}")
    b.metric("Option snapshots", f"{quality['counts']['option_snapshots']:,}")
    c.metric("Verified instruments", f"{quality['counts']['verified_instruments']:,}")
    d.metric("Quality events (all time)", f"{quality['total_events']:,}")
    st.write("Latest quote timestamp:", fmt_ist(quality["last_quote"]))
    st.write("Latest candle timestamp:", fmt_ist(quality["last_candle"]))

    validation_path = settings.data_dir / "market_hours_validation_latest.json"
    if validation_path.exists():
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            with st.expander("Latest Upstox read-only diagnostic"):
                st.json(validation)
        except (OSError, json.JSONDecodeError):
            st.warning("The diagnostic JSON exists but could not be read.")


live_dashboard()
st.caption("PAPER dashboard · manual exits are simulated locally · no broker order endpoint is used.")
