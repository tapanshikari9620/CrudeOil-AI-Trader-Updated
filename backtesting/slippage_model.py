def apply_slippage(price: float, side: str, ticks: int, tick_size: float) -> float:
    if price <= 0 or ticks < 0 or tick_size <= 0: raise ValueError("Invalid slippage inputs")
    if side == "BUY": return price + ticks*tick_size
    if side == "SELL": return max(tick_size, price - ticks*tick_size)
    raise ValueError("side must be BUY or SELL")
