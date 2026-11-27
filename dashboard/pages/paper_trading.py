
import streamlit as st
from datetime import datetime, timezone

from dashboard.dashboard_data import (
    get_trade_rows,
    trades_frame,
    IST,
    fmt_ist,
)


def render() -> None:
    st.title("PAPER Trading Monitor")

    trades = get_trade_rows(2000)

    opened = [t for t in trades if t.status == "OPEN"]
    closed = [t for t in trades if t.status == "CLOSED"]

    today = datetime.now(IST).date()

    today_trades = [
        t
        for t in trades
        if t.opened_at
        and (
            (
                t.opened_at.replace(tzinfo=timezone.utc)
                if t.opened_at.tzinfo is None
                else t.opened_at
            )
            .astimezone(IST)
            .date()
            == today
        )
    ]

    realized = sum(
        float(t.net_pnl or 0)
        for t in today_trades
        if t.status == "CLOSED"
    )

    unrealized = sum(
        float(t.unrealized_pnl or 0)
        for t in opened
    )

    a, b, c, d = st.columns(4)

    a.metric("Open positions", len(opened))
    b.metric("Closed trades", len(closed))
    c.metric("Today's realized P&L", f"₹{realized:,.2f}")
    d.metric("Unrealized P&L", f"₹{unrealized:,.2f}")

    st.subheader("Open positions")

    if opened:
        st.dataframe(
            trades_frame(opened),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No open PAPER positions.")

    st.subheader("Trade history")

    if trades:
        st.dataframe(
            trades_frame(trades),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No PAPER trades recorded.")

    st.warning(
        "Monitoring only. This page cannot place, modify or cancel orders."
    )


# Prevent database and Streamlit execution during module imports.
if __name__ == "__main__":
    render()
