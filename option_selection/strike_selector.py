def select_nearest_strike(contracts: list[dict], underlying_price: float, option_type: str) -> dict | None:
    eligible = [c for c in contracts if c.get("option_type") == option_type and c.get("strike") is not None]
    return min(eligible, key=lambda c: abs(float(c["strike"])-underlying_price)) if eligible else None
