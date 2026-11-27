def spread_percent(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask < bid: raise ValueError("Invalid bid/ask")
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100
def is_liquid(bid: float, ask: float, min_volume: float, volume: float, max_spread_percent: float) -> bool:
    return bid > 0 and ask >= bid and volume >= min_volume and spread_percent(bid, ask) <= max_spread_percent
