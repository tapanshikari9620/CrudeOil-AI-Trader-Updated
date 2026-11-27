
import streamlit as st
import pandas as pd
from dashboard.dashboard_data import get_performance, trades_frame


def render() -> None:
    st.title("PAPER Trading Performance")

    data = get_performance()

    if not data["total"]:
        st.info("No completed PAPER trades yet.")
        return

    a, b, c, d, e = st.columns(5)

    a.metric("Closed trades", data["total"])
    b.metric("Win rate", f"{data['win_rate']:.1f}%")
    c.metric("Net P&L", f"₹{data['net']:,.2f}")
    d.metric(
        "Profit factor",
        f"{data['profit_factor']:.2f}"
        if data["profit_factor"] != float("inf")
        else "∞"
    )
    e.metric("Max drawdown", f"₹{data['max_drawdown']:,.2f}")

    a, b, c = st.columns(3)

    a.metric("Winners", data["wins"])
    b.metric("Losers", data["losses"])
    c.metric("Gross profit", f"₹{data['gross_profit']:,.2f}")

    if data["equity"]:
        equity = pd.DataFrame({
            "Cumulative net P&L (₹)": data["equity"]
        })
        st.subheader("Closed-trade equity curve")
        st.line_chart(equity)

    st.subheader("Completed trades")
    st.dataframe(
        trades_frame(data["trades"]),
        use_container_width=True,
        hide_index=True
    )

    st.caption(
        "Metrics use stored net_pnl for CLOSED PAPER trades. "
        "Results depend on how charges are recorded."
    )


if __name__ == "__main__":
    render()
