import pandas as pd
def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    if period <= 0: raise ValueError("period must be positive")
    prev = frame["close"].shift(1)
    tr = pd.concat([(frame["high"]-frame["low"]).abs(),
                    (frame["high"]-prev).abs(), (frame["low"]-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
