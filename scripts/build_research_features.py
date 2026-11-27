
from __future__ import annotations

from pathlib import Path

import pandas as pd

from strategy.features import build_features


ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "data" / "research" / "regular_full_session_candles.csv"
OUTPUT_FILE = ROOT / "data" / "research" / "regular_session_features.csv"

FORWARD_HORIZONS = (1, 3, 6)  # 5, 15 and 30 minutes


def main():
    print("Loading regular-session research dataset...")

    df = pd.read_csv(INPUT_FILE)

    if df.empty:
        raise ValueError("Research dataset is empty.")

    required = {
        "instrument_key",
        "trading_date_ist",
        "timestamp_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    # Explicit UTC parsing and chronological ordering.
    df["timestamp"] = pd.to_datetime(
        df["timestamp_utc"], utc=True, errors="raise"
    )

    df["trading_date_ist"] = pd.to_datetime(
        df["trading_date_ist"], errors="raise"
    ).dt.date.astype(str)

    df = df.sort_values(
        ["instrument_key", "timestamp"]
    ).reset_index(drop=True)

    if df.duplicated(["instrument_key", "timestamp"]).any():
        raise ValueError("Duplicate instrument timestamps detected.")

    numeric_columns = ["open", "high", "low", "close", "volume"]

    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="raise")

    if not df[numeric_columns].notna().all().all():
        raise ValueError("Missing OHLCV values detected.")

    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Non-positive prices detected.")

    print("Calculating existing production indicators...")

    # build_features expects a column named 'timestamp'.
    # We supply it explicitly so it cannot accidentally sort by close.
    feature_groups = []

    for instrument_key, group in df.groupby(
        "instrument_key", sort=False
    ):
        group = group.sort_values("timestamp").reset_index(drop=True)

        featured = build_features(group)

        # Keep the existing live-strategy momentum calculation for comparison.
        featured = featured.rename(
            columns={"momentum": "momentum_3bar_live_parity"}
        )

        # Additional session-local momentum avoids crossing overnight boundaries.
        featured["momentum_3bar_session"] = (
            featured.groupby("trading_date_ist")["close"]
            .pct_change(3)
        )

        featured["close_vs_vwap_pct"] = (
            featured["close"] / featured["vwap"] - 1
        ) * 100

        featured["close_vs_ema20_pct"] = (
            featured["close"] / featured["ema20"] - 1
        ) * 100

        featured["ema9_vs_ema20_pct"] = (
            featured["ema9"] / featured["ema20"] - 1
        ) * 100

        featured["ema20_vs_ema50_pct"] = (
            featured["ema20"] / featured["ema50"] - 1
        ) * 100

        featured["atr_pct"] = (
            featured["atr14"] / featured["close"]
        ) * 100

        featured["volume_ratio"] = (
            featured["volume"] / featured["volume_sma20"]
        )

        # Future returns are research targets, never model input features.
        for horizon in FORWARD_HORIZONS:
            future_close = (
                featured.groupby("trading_date_ist")["close"]
                .shift(-horizon)
            )

            featured[f"forward_return_{horizon * 5}m_pct"] = (
                future_close / featured["close"] - 1
            ) * 100

        feature_groups.append(featured)

    result = pd.concat(feature_groups, ignore_index=True)

    # Remove the helper timestamp column only after all calculations.
    result = result.drop(columns=["timestamp"])

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_FILE, index=False)

    print("\n========== RESEARCH FEATURE BUILD ==========")
    print(f"Input rows: {len(df)}")
    print(f"Output rows: {len(result)}")
    print(f"Trading dates: {result['trading_date_ist'].nunique()}")
    print(f"Output file: {OUTPUT_FILE}")

    feature_columns = [
        "ema9",
        "ema20",
        "ema50",
        "rsi14",
        "vwap",
        "atr14",
        "volume_sma20",
        "momentum_3bar_live_parity",
        "momentum_3bar_session",
        "close_vs_vwap_pct",
        "close_vs_ema20_pct",
        "ema9_vs_ema20_pct",
        "ema20_vs_ema50_pct",
        "atr_pct",
        "volume_ratio",
    ]

    print("\nFeature missing-value counts:")
    print(result[feature_columns].isna().sum().to_string())

    print("\nForward-return target missing-value counts:")
    for horizon in FORWARD_HORIZONS:
        column = f"forward_return_{horizon * 5}m_pct"
        print(f"{column}: {result[column].isna().sum()}")

    print("\nResearch feature build completed.")
    print("Original database and production strategy were not modified.")


if __name__ == "__main__":
    main()
