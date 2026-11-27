from dataclasses import dataclass
from strategy.bullish_strategy import bullish_conditions
from strategy.bearish_strategy import bearish_conditions
from strategy.market_regime import classify_regime

@dataclass(frozen=True)
class Signal:
    direction: str
    option_type: str | None
    valid: bool
    reasons: tuple[str, ...]

def generate_signal(row: dict) -> Signal:
    needed = ("close","ema9","ema20","ema50","rsi14","vwap","atr14","momentum","volume","volume_sma20")
    if any(row.get(k) is None for k in needed):
        return Signal("NO_TRADE", None, False, ("incomplete_indicators",))
    regime = classify_regime(row)
    if regime != "TRENDING":
        return Signal("NO_TRADE", None, False, (f"market_regime_{regime.lower()}",))
    bull, bull_fail = bullish_conditions(row)
    bear, bear_fail = bearish_conditions(row)
    if bull: return Signal("BULLISH", "CE", True, ("all_bullish_conditions_met",))
    if bear: return Signal("BEARISH", "PE", True, ("all_bearish_conditions_met",))
    return Signal("NO_TRADE", None, False, tuple(["conditions_not_met"] + (bull_fail if len(bull_fail) <= len(bear_fail) else bear_fail)))
