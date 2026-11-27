import pandas as pd
def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    if period <= 0: raise ValueError("period must be positive")
    return volume.astype(float).rolling(period, min_periods=period).mean()
