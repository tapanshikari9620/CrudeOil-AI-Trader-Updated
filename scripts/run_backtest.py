import argparse, pandas as pd, json
from backtesting.backtest_engine import run_long_option_backtest
def main():
    p=argparse.ArgumentParser(description="Backtest actual historical option candles from CSV")
    p.add_argument("--csv",required=True,help="CSV with timestamp, open, high, low, close")
    p.add_argument("--signals",required=True,help="CSV column name containing boolean entry signals")
    p.add_argument("--quantity",type=int,default=1); a=p.parse_args()
    df=pd.read_csv(a.csv); signals=df[a.signals].astype(bool)
    trades,metrics=run_long_option_backtest(df,signals,quantity=a.quantity)
    print(json.dumps(metrics,indent=2)); trades.to_csv("backtest_trades.csv",index=False)
if __name__=="__main__": main()
