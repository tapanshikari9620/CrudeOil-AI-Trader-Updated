import pandas as pd
import streamlit as st

from dashboard.dashboard_data import (
    fmt_ist,
    get_latest_system_events,
    get_quality_summary,
)


def render():
    st.title("Data Quality & Collector Health")

    hours = st.selectbox("Event window", [1, 6, 12, 24, 48, 72], index=3)
    data = get_quality_summary(hours)

    a, b, c, d = st.columns(4)
    a.metric("Quality events (all time)", data["total_events"])
    b.metric("Candles", data["counts"]["candles"])
    c.metric("Market quotes", data["counts"]["market_quotes"])
    d.metric("Option snapshots", data["counts"]["option_snapshots"])

    st.write("Latest quote:", fmt_ist(data["last_quote"]))
    st.write("Latest candle:", fmt_ist(data["last_candle"]))

    st.subheader(f"Quality classifications — last {hours} hours")
    if data["recent"]:
        st.dataframe(
            pd.DataFrame([
                {"Classification": r[0], "Count": r[1]}
                for r in data["recent"]
            ]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No quality events in the selected window.")

    st.subheader("Collector breakdown")
    if data["by_collector"]:
        st.dataframe(
            pd.DataFrame([
                {
                    "Collector": r[0],
                    "Classification": r[1],
                    "Count": r[2],
                }
                for r in data["by_collector"]
            ]),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Verified contract inventory")
    if data["instrument_status"]:
        st.dataframe(
            pd.DataFrame([
                {"Type": r[0], "Status": r[1], "Count": r[2]}
                for r in data["instrument_status"]
            ]),
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Latest quality events"):
        if data["events"]:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Observed (IST)": fmt_ist(e.observed_at),
                        "Collector": e.collector,
                        "Class": e.classification,
                        "Instrument": e.instrument_key,
                        "Reason": e.reason,
                    }
                    for e in data["events"]
                ]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("No quality events recorded.")

    with st.expander("Latest system events"):
        events = get_latest_system_events(100)
        if events:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Time (IST)": fmt_ist(e.created_at),
                        "Severity": e.severity,
                        "Event": e.event_type,
                        "Message": e.message,
                    }
                    for e in events
                ]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.write("No system events recorded.")

    st.caption(
        "This page only reads database records; it does not repair, "
        "delete or reclassify data."
    )


if __name__ == "__main__":
    render()