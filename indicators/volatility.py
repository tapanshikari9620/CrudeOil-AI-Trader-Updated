import numpy as np
import pandas as pd
def historical_volatility(close: pd.Series, period: int = 20, annualization: float = 252.0) -> pd.Series:
    if period <= 1: raise ValueError("period must be > 1")
    returns = np.log(close.astype(float)).diff()
    return returns.rolling(period, min_periods=period).std() * np.sqrt(annualization)
