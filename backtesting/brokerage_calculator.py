def estimate_round_trip_cost(quantity: int, entry: float, exit: float, brokerage: float = 0.0,
                             exchange_and_taxes: float = 0.0) -> float:
    if quantity <= 0 or entry < 0 or exit < 0: raise ValueError("Invalid trade values")
    return max(0.0, brokerage) + max(0.0, exchange_and_taxes)
