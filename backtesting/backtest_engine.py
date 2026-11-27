import pandas as pd
from backtesting.trade_simulator import exit_reason
from backtesting.performance_metrics import calculate_metrics

def run_long_option_backtest(candles: pd.DataFrame, entry_signals: pd.Series, stop_fraction: float = .20,
                             target_fraction: float = .35, quantity: int = 1, cost_per_trade: float = 0.0) -> tuple[pd.DataFrame, dict]:
    if not {"open","high","low","close"}.issubset(candles.columns): raise ValueError("Option candles required")
    if len(entry_signals) != len(candles): raise ValueError("Signals length mismatch")
    records=[]; i=0
    while i < len(candles):
        if not bool(entry_signals.iloc[i]): i += 1; continue
        entry=float(candles.iloc[i]["close"])
        if entry <= 0: i += 1; continue
        stop=entry*(1-stop_fraction); target=entry*(1+target_fraction)
        result=None; reason=None; exit_i=None
        for j in range(i+1,len(candles)):
            result,reason=exit_reason(entry,stop,target,candles.iloc[j].to_dict())
            if result is not None: exit_i=j; break
        if result is None: result=float(candles.iloc[-1]["close"]); reason="END_OF_DATA"; exit_i=len(candles)-1
        pnl=(result-entry)*quantity-cost_per_trade
        records.append({"entry_index":i,"exit_index":exit_i,"entry_price":entry,"exit_price":result,
                        "quantity":quantity,"net_pnl":pnl,"exit_reason":reason})
        i=exit_i+1
    result=pd.DataFrame(records)
    return result, calculate_metrics(result)
