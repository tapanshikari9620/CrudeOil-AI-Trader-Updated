"""Risk-multiple target calculation for adaptive paper trades."""
from __future__ import annotations

import math


def target_from_risk(entry: float, stop: float, reward_multiple: float = 2.0) -> float:
    """Set target from the actual initial premium risk, not a fixed rupee profit."""
    values = (entry, stop, reward_multiple)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("Target inputs must be finite numbers")
    if entry <= 0 or stop <= 0 or stop >= entry or reward_multiple <= 0:
        raise ValueError("Require entry > stop > 0 and positive reward_multiple")
    return round(entry + (entry - stop) * reward_multiple, 2)


def target_from_net_profit(
    entry: float, quantity: int, target_net: float, estimated_exit_charges: float = 0.0
) -> float:
    """Legacy fixed-net-target helper retained for historical compatibility."""
    if entry <= 0 or quantity <= 0 or target_net <= 0:
        raise ValueError("Inputs must be positive")
    return round(entry + (target_net + max(0, estimated_exit_charges)) / quantity, 2)
