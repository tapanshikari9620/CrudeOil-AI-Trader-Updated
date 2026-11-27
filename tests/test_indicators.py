import pandas as pd
from indicators.ema import ema
from indicators.rsi import rsi
from indicators.vwap import vwap
from indicators.atr import atr

def test_ema_has_expected_initial_nans():
    out=ema(pd.Series([1,2,3,4,5],dtype=float),3)
    assert out.iloc[:2].isna().all() and out.iloc[2] > 0

def test_rsi_bounds():
    out=rsi(pd.Series(range(1,40),dtype=float),14).dropna()
    assert (out.between(0,100)).all()

def test_vwap_constant_price():
    df=pd.DataFrame({"high":[10,10],"low":[10,10],"close":[10,10],"volume":[2,3]})
    assert (vwap(df).dropna()==10).all()

def test_atr_positive():
    df=pd.DataFrame({"high":[11,12,13],"low":[9,10,11],"close":[10,11,12]})
    assert atr(df,2).dropna().gt(0).all()
