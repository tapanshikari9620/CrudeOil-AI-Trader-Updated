import pandas as pd
from backtesting.backtest_engine import run_long_option_backtest
def test_backtest_stops_on_stop_loss():
    df=pd.DataFrame({"open":[100,90],"high":[100,95],"low":[100,80],"close":[100,85]})
    trades,metrics=run_long_option_backtest(df,pd.Series([True,False]),quantity=1)
    assert len(trades)==1 and trades.iloc[0].exit_reason=="STOP_LOSS"
