def daily_limit_reached(realized_net_pnl: float, max_daily_loss: float) -> bool:
    if max_daily_loss <= 0: raise ValueError("max_daily_loss must be positive")
    return realized_net_pnl <= -abs(max_daily_loss)
