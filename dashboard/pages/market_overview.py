
from __future__ import annotations

import streamlit as st

from dashboard.dashboard_data import (
    age_seconds,
    fmt_ist,
    latest_futures_quote,
    load_recent_candles,
)
from database.db_connection import SessionLocal


def render() -> None:
    """Render the Market Overview dashboard page."""

    st.title("Market Overview")

    # Read the latest futures contract and quote from the local database.
    with SessionLocal() as session:
        future, quote = latest_futures_quote(session)

    if future:
        st.caption(
            f"{future.trading_symbol} · "
            f"{future.instrument_key} · "
            f"expiry {future.expiry}"
        )

    if quote:
        age = age_seconds(quote.last_trade_time)

        a, b, c, d = st.columns(4)

        a.metric("LTP", f"₹{quote.last_price:,.2f}")

        b.metric(
            "Bid",
            f"₹{quote.bid_price:,.2f}"
            if quote.bid_price
            else "—",
        )

        c.metric(
            "Ask",
            f"₹{quote.ask_price:,.2f}"
            if quote.ask_price
            else "—",
        )

        d.metric(
            "Last trade age",
            f"{age:.0f}s"
            if age is not None and age >= 0
            else "Unknown",
        )

        st.caption(
            f"Observed {fmt_ist(quote.timestamp)} · "
            f"Exchange trade {fmt_ist(quote.last_trade_time)} · "
            f"{quote.quality_status}"
        )

    else:
        st.warning("No futures quote is stored yet.")

    # Display stored futures candles.
    if future:
        candles = load_recent_candles(
            future.instrument_key,
            "5_min",
            300,
        )

        if not candles.empty:
            chart = candles.set_index("timestamp")[["close"]]

            st.subheader("Stored 5-minute futures candles")

            st.line_chart(chart, y="close")

            st.dataframe(
                candles.tail(20).iloc[::-1],
                use_container_width=True,
                hide_index=True,
            )

        else:
            st.info(
                "No 5-minute candles are stored for "
                "the selected futures contract."
            )

    st.caption(
        "Values are read from the local database; "
        "no direct broker API request is made by this page."
    )


if __name__ == "__main__":
    render()
