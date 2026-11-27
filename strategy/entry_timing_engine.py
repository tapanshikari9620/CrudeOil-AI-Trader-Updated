"""Deterministic entry-timing engine layered beside the existing signal engine.

This is a rule-based timing engine, not a trained ML model. It preserves the
existing signal output and can form a separate, stricter early trend candidate
from completed candles when the full existing confirmation is not yet present.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class EntryTimingDecision:
    action: str  # ENTER, WAIT, REJECT
    setup_type: str
    score: int
    extension_atr: float | None
    reasons: tuple[str, ...]
    option_type: str | None = None
    directional_source: str = "EXISTING_SIGNAL"


def _number(row: Any, key: str) -> float | None:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def evaluate_entry_timing(
    signal: Any,
    candles_with_features: pd.DataFrame,
    *,
    max_extension_atr: float = 1.5,
    min_volume_ratio: float = 1.0,
) -> EntryTimingDecision:
    """Score an existing signal or a stricter early directional candidate.

    The original signal function and its output are never modified. Early
    candidates require VWAP, EMA9/20/50 structure and aligned momentum; the
    timing patterns then apply additional confirmation before returning ENTER.
    """
    existing_valid = bool(getattr(signal, "valid", False))
    existing_option_type = getattr(signal, "option_type", None)
    if not isinstance(candles_with_features, pd.DataFrame) or len(candles_with_features) < 6:
        return EntryTimingDecision("WAIT", "INSUFFICIENT_HISTORY", 0, None, ("need_six_completed_featured_candles",))

    row = candles_with_features.iloc[-1]
    close = _number(row, "close")
    open_ = _number(row, "open")
    high = _number(row, "high")
    low = _number(row, "low")
    ema9 = _number(row, "ema9")
    ema20 = _number(row, "ema20")
    ema50 = _number(row, "ema50")
    rsi = _number(row, "rsi14")
    atr = _number(row, "atr14")
    momentum = _number(row, "momentum")
    volume = _number(row, "volume")
    volume_sma = _number(row, "volume_sma20")

    required = (close, open_, high, low, ema9, ema20, ema50, rsi, atr, momentum, volume, volume_sma)
    if any(value is None for value in required) or close <= 0 or atr <= 0 or volume_sma <= 0:
        return EntryTimingDecision("WAIT", "INCOMPLETE_FEATURES", 0, None, ("required_timing_features_missing",))

    # Keep the original signal untouched. If it is not yet confirmed, allow a
    # separate, stricter directional candidate from the trend structure so the
    # timing layer can assess a genuinely earlier setup.
    directional_source = "EXISTING_SIGNAL"
    if existing_valid and existing_option_type in {"CE", "PE"}:
        direction = existing_option_type
    else:
        vwap = _number(row, "vwap")
        if vwap is None:
            return EntryTimingDecision("WAIT", "NO_DIRECTIONAL_BIAS", 0, None, ("vwap_unavailable_for_early_candidate",))
        if close > vwap and close > ema20 and ema9 > ema20 > ema50 and momentum > 0:
            direction = "CE"
        elif close < vwap and close < ema20 and ema9 < ema20 < ema50 and momentum < 0:
            direction = "PE"
        else:
            return EntryTimingDecision("WAIT", "NO_DIRECTIONAL_BIAS", 0, None, ("early_directional_structure_not_confirmed",))
        directional_source = "EARLY_TREND_CANDIDATE"

    bullish = direction == "CE"
    extension = (close - ema20) / atr if bullish else (ema20 - close) / atr
    aligned_momentum = momentum if bullish else -momentum
    volume_ratio = volume / volume_sma

    # Reject stretched moves before considering attractive-looking setups.
    overbought_oversold = (rsi >= 78) if bullish else (rsi <= 22)
    if extension > max_extension_atr or overbought_oversold:
        return EntryTimingDecision(
            "REJECT", "LATE_ENTRY", 0, round(extension, 3),
            ("price_extended_from_ema20",) if extension > max_extension_atr else ("rsi_extreme_after_move",),
            option_type=direction, directional_source=directional_source,
        )

    recent = candles_with_features.iloc[-6:-1]
    prev_high = float(recent["high"].max())
    prev_low = float(recent["low"].min())
    recent_ema9 = pd.to_numeric(recent["ema9"], errors="coerce")
    recent_atr = pd.to_numeric(recent["atr14"], errors="coerce")
    valid_pullback_rows = recent_ema9.notna() & recent_atr.notna() & (recent_atr > 0)

    if bullish:
        touched_ema = bool(((recent.loc[valid_pullback_rows, "low"].astype(float) - recent_ema9[valid_pullback_rows]).abs() <= 0.20 * recent_atr[valid_pullback_rows]).any()) if valid_pullback_rows.any() else False
        reclaimed = close > ema9 and close > open_
        breakout = close > prev_high and volume_ratio >= min_volume_ratio
        trend_aligned = ema9 > ema20 > ema50
        momentum_ok = aligned_momentum > 0 and 52 <= rsi <= 74
    else:
        touched_ema = bool(((recent_ema9[valid_pullback_rows] - recent.loc[valid_pullback_rows, "high"].astype(float)).abs() <= 0.20 * recent_atr[valid_pullback_rows]).any()) if valid_pullback_rows.any() else False
        reclaimed = close < ema9 and close < open_
        breakout = close < prev_low and volume_ratio >= min_volume_ratio
        trend_aligned = ema9 < ema20 < ema50
        momentum_ok = aligned_momentum > 0 and 26 <= rsi <= 48

    if touched_ema and reclaimed and trend_aligned and volume_ratio >= min_volume_ratio:
        return EntryTimingDecision(
            "ENTER", "PULLBACK_RECLAIM", 88, round(extension, 3),
            ("recent_ema9_pullback", "directional_reclaim", "trend_stack_aligned", "volume_confirmed"),
            option_type=direction, directional_source=directional_source,
        )
    if breakout and trend_aligned:
        return EntryTimingDecision(
            "ENTER", "RANGE_BREAKOUT", 84, round(extension, 3),
            ("close_broke_previous_five_candle_range", "trend_stack_aligned", "volume_confirmed"),
            option_type=direction, directional_source=directional_source,
        )
    early_volume_ok = volume_ratio >= (0.80 if directional_source == "EARLY_TREND_CANDIDATE" else min_volume_ratio)
    if trend_aligned and momentum_ok and reclaimed and extension <= 1.0 and early_volume_ok:
        return EntryTimingDecision(
            "ENTER", "EARLY_MOMENTUM", 70 if directional_source == "EARLY_TREND_CANDIDATE" else 76, round(extension, 3),
            ("trend_stack_aligned", "directional_momentum", "non_extended_entry"),
            option_type=direction, directional_source=directional_source,
        )

    reasons = []
    if not trend_aligned:
        reasons.append("ema_trend_stack_not_aligned")
    if not momentum_ok:
        reasons.append("momentum_or_rsi_not_supportive")
    if volume_ratio < min_volume_ratio:
        reasons.append("volume_below_confirmation_threshold")
    if not (touched_ema and reclaimed or breakout or reclaimed):
        reasons.append("no_valid_entry_pattern")
    return EntryTimingDecision("WAIT", "NO_QUALIFIED_SETUP", 0, round(extension, 3), tuple(reasons or ["timing_conditions_not_met"]), option_type=direction, directional_source=directional_source)
