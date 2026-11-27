
import pandas as pd


def vwap(frame: pd.DataFrame) -> pd.Series:
    required = {"high", "low", "close", "volume"}

    if not required.issubset(frame.columns):
        raise ValueError(f"Need columns {required}")

    if frame.empty:
        return pd.Series(index=frame.index, dtype=float)

    typical_price = (
        frame["high"].astype(float)
        + frame["low"].astype(float)
        + frame["close"].astype(float)
    ) / 3

    volume = frame["volume"].astype(float)
    price_volume = typical_price * volume

    if "timestamp" in frame.columns:
        timestamps = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )

        if timestamps.isna().any():
            raise ValueError("Invalid candle timestamps found")

        session = timestamps.dt.tz_convert("Asia/Kolkata").dt.date

        cumulative_pv = price_volume.groupby(session).cumsum()
        cumulative_volume = volume.groupby(session).cumsum()
    else:
        # Without timestamps, treat all rows as one session.
        cumulative_pv = price_volume.cumsum()
        cumulative_volume = volume.cumsum()

    return cumulative_pv / cumulative_volume.where(cumulative_volume > 0)
