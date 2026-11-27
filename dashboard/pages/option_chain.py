
import streamlit as st
import pandas as pd

from dashboard.dashboard_data import (
    fmt_ist,
    get_option_expiries,
    get_latest_option_snapshots,
    latest_active_future,
)
from database.db_connection import SessionLocal


def render() -> None:
    st.title("MCX Crude Oil Option Contracts")

    # Fetch the latest active futures contract only when the page renders.
    with SessionLocal() as session:
        future = latest_active_future(session)

    future_key = future.instrument_key if future else None

    if future:
        st.caption(
            f"Underlying futures: {future.trading_symbol} · "
            f"{future.instrument_key}"
        )
    else:
        st.warning("No ACTIVE verified crude futures contract found.")

    expiries = get_option_expiries(future_key)

    if not expiries:
        st.info(
            "No valid option snapshots for unexpired contracts "
            "are stored yet."
        )
        return

    expiry = st.selectbox("Expiry", expiries)

    rows = get_latest_option_snapshots(future_key, expiry, 1000)

    if not rows:
        st.info("No latest snapshots found for this expiry.")
        return

    frame = pd.DataFrame([
        {
            "Strike": q.strike_price,
            "Type": q.option_type,
            "Symbol": q.trading_symbol,
            "LTP": q.ltp,
            "Bid": q.bid_price,
            "Bid qty": q.bid_quantity,
            "Ask": q.ask_price,
            "Ask qty": q.ask_quantity,
            "Volume": q.volume,
            "Open interest": q.open_interest,
            "Last trade": fmt_ist(q.last_trade_time),
            "Observed": fmt_ist(q.timestamp),
            "Quality": q.quality_status,
        }
        for q in rows
    ])

    calls = frame[frame["Type"] == "CE"].sort_values("Strike")
    puts = frame[frame["Type"] == "PE"].sort_values("Strike")

    a, b = st.columns(2)
    a.metric("Call contracts", len(calls))
    b.metric("Put contracts", len(puts))

    st.subheader("Calls (CE)")
    st.dataframe(
        calls,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Puts (PE)")
    st.dataframe(
        puts,
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "This is a locally stored quote snapshot view, "
        "not an official live option-chain endpoint."
    )


# Prevent database and Streamlit execution during module imports.
if __name__ == "__main__":
    render()
