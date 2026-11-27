import pandas as pd
def ema(series: pd.Series, period: int) -> pd.Series:
    if period <= 0: raise ValueError("period must be positive")
    return series.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()
