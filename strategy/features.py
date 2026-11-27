import pandas as pd
from indicators.ema import ema
from indicators.rsi import rsi
from indicators.vwap import vwap
from indicators.atr import atr
from indicators.volume import volume_sma

def build_features(candles: pd.DataFrame) -> pd.DataFrame:
    required = {"open","high","low","close","volume"}
    if not required.issubset(candles.columns): raise ValueError(f"Missing {required-set(candles.columns)}")
    f = candles.copy().sort_values("timestamp" if "timestamp" in candles else candles.index.name or "close")
    f["ema9"] = ema(f.close, 9); f["ema20"] = ema(f.close, 20); f["ema50"] = ema(f.close, 50)
    f["rsi14"] = rsi(f.close, 14); f["vwap"] = vwap(f); f["atr14"] = atr(f, 14)
    f["volume_sma20"] = volume_sma(f.volume, 20)
    f["momentum"] = f.close.pct_change(3)
    return f
