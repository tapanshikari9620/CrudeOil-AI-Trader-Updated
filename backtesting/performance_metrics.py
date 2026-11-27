import pandas as pd
def calculate_metrics(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"total_trades":0,"winning_trades":0,"losing_trades":0,"win_rate":0.0,"net_profit":0.0,"profit_factor":0.0,"max_drawdown":0.0}
    pnl = trades["net_pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    gross_loss = abs(losses.sum())
    return {"total_trades":len(pnl),"winning_trades":int((pnl>0).sum()),"losing_trades":int((pnl<0).sum()),
            "win_rate":float((pnl>0).mean()*100),"net_profit":float(pnl.sum()),
            "profit_factor":float(wins.sum()/gross_loss) if gross_loss else float("inf") if wins.sum()>0 else 0.0,
            "max_drawdown":float(drawdown.min())}
