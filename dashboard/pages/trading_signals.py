
import streamlit as st
from dashboard.dashboard_data import fmt_ist, get_signal_snapshot


def render() -> None:
    st.title("Trading Signals")
    st.caption(
        "Read-only calculation using the project's existing "
        "five-minute futures strategy."
    )

    result = get_signal_snapshot()

    if not result["available"]:
        st.info(result["reason"])
        return

    signal = result["signal"]
    timing = result.get("timing")

    if signal.valid:
        st.success(f"{signal.direction} signal · {signal.option_type}")
    else:
        st.info(f"{signal.direction} · no entry")

    st.write("Signal candle:", fmt_ist(result["timestamp"]))
    st.write("Candle age:", f"{result['age_seconds']:.0f} seconds")
    st.write("Decision reasons:")

    for reason in signal.reasons:
        st.write(f"- {reason}")

    if timing is not None:
        st.subheader("Entry timing assessment")
        st.metric("Timing action", timing.action)
        st.write("Setup:", timing.setup_type)
        st.write("Timing score:", f"{timing.score}/100")
        if timing.extension_atr is not None:
            st.write("Extension from EMA20:", f"{timing.extension_atr:.2f} ATR")
        for reason in timing.reasons:
            st.write(f"- {reason}")
        st.caption("Rule-based timing filter; not a trained machine-learning model.")

    ind = result["indicators"]

    cols = st.columns(5)

    for col, label, key in [
        (cols[0], "Close", "close"),
        (cols[1], "EMA 9", "ema9"),
        (cols[2], "EMA 20", "ema20"),
        (cols[3], "EMA 50", "ema50"),
        (cols[4], "RSI 14", "rsi14"),
    ]:
        value = ind.get(key)
        col.metric(
            label,
            f"{float(value):.2f}" if value is not None else "—"
        )

    cols = st.columns(4)

    for col, label, key in [
        (cols[0], "VWAP", "vwap"),
        (cols[1], "ATR 14", "atr14"),
        (cols[2], "Momentum", "momentum"),
        (cols[3], "Volume", "volume"),
    ]:
        value = ind.get(key)
        col.metric(
            label,
            f"{float(value):.4f}" if value is not None else "—"
        )

    st.warning(
        "A displayed strategy signal is not an order. "
        "The page does not submit trades."
    )


if __name__ == "__main__":
    render()
