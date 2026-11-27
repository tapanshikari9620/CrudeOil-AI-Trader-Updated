import pandas as pd

from strategy.entry_timing_engine import evaluate_entry_timing
from strategy.signal_engine import Signal


def _frame(*, close=103.5, open_=102.8, ema9=102.5, ema20=102.0, ema50=101.0,
           rsi=62.0, atr=2.0, momentum=0.003, volume=120.0, volume_sma=100.0,
           prior_high=103.0, prior_low=102.0, vwap=101.5):
    rows = []
    for i in range(5):
        rows.append({
            "open": 100.0 + i * 0.2,
            "high": prior_high - (4 - i) * 0.2,
            "low": prior_low + (4 - i) * 0.2,
            "close": 100.0 + i * 0.2,
            "ema9": 100.5 + i * 0.2,
            "ema20": 100.0 + i * 0.2,
            "ema50": 99.0 + i * 0.2,
            "rsi14": 58.0,
            "atr14": atr,
            "momentum": 0.001,
            "volume": 110.0,
            "volume_sma20": 100.0,
        })
    rows.append({
        "open": open_, "high": close + 0.2, "low": open_ - 0.2, "close": close,
        "ema9": ema9, "ema20": ema20, "ema50": ema50, "vwap": vwap, "rsi14": rsi,
        "atr14": atr, "momentum": momentum, "volume": volume,
        "volume_sma20": volume_sma,
    })
    return pd.DataFrame(rows)


def test_no_existing_signal_without_directional_structure_waits():
    result = evaluate_entry_timing(Signal("NO_TRADE", None, False, ("not_ready",)), _frame(vwap=104.0))
    assert result.action == "WAIT"
    assert result.setup_type == "NO_DIRECTIONAL_BIAS"


def test_early_momentum_setup_is_accepted_after_existing_signal():
    # Close is above EMA9 but remains below the prior five-candle high.
    frame = _frame(close=102.8, open_=102.4, ema9=102.5, ema20=102.0,
                   ema50=101.0, prior_high=103.5)
    result = evaluate_entry_timing(Signal("BULLISH", "CE", True, ()), frame)
    assert result.action == "ENTER"
    assert result.setup_type == "EARLY_MOMENTUM"
    assert result.score == 76


def test_breakout_setup_requires_trend_and_volume():
    frame = _frame(close=103.5, open_=102.8, prior_high=103.0)
    result = evaluate_entry_timing(Signal("BULLISH", "CE", True, ()), frame)
    assert result.action == "ENTER"
    assert result.setup_type == "RANGE_BREAKOUT"


def test_late_extended_entry_is_rejected():
    frame = _frame(close=106.0, open_=105.0, ema9=104.0, ema20=102.0,
                   ema50=100.0, atr=2.0, prior_high=105.0)
    result = evaluate_entry_timing(Signal("BULLISH", "CE", True, ()), frame)
    assert result.action == "REJECT"
    assert result.setup_type == "LATE_ENTRY"


def test_insufficient_feature_history_waits():
    result = evaluate_entry_timing(Signal("BULLISH", "CE", True, ()), _frame().head(4))
    assert result.action == "WAIT"
    assert result.setup_type == "INSUFFICIENT_HISTORY"


def test_early_trend_candidate_can_act_before_full_existing_signal():
    # Existing signal remains NO_TRADE because its complete RSI/volume rules are not met.
    frame = _frame(close=102.8, open_=102.4, ema9=102.5, ema20=102.0,
                   ema50=101.0, rsi=53.0, volume=85.0, volume_sma=100.0,
                   prior_high=103.5, prior_low=102.0)
    result = evaluate_entry_timing(Signal("NO_TRADE", None, False, ("volume_confirmation",)), frame)
    assert result.action == "ENTER"
    assert result.option_type == "CE"
    assert result.directional_source == "EARLY_TREND_CANDIDATE"
    assert result.setup_type == "EARLY_MOMENTUM"
    assert result.score == 70


def test_bearish_early_trend_candidate_can_select_pe():
    frame = _frame(
        close=97.0, open_=97.4, ema9=97.5, ema20=98.0, ema50=99.0,
        rsi=45.0, atr=2.0, momentum=-0.003, volume=85.0,
        prior_high=101.0, prior_low=95.0, vwap=98.0,
    )
    result = evaluate_entry_timing(Signal("NO_TRADE", None, False, ("not_ready",)), frame)
    assert result.action == "ENTER"
    assert result.option_type == "PE"
    assert result.directional_source == "EARLY_TREND_CANDIDATE"
    assert result.setup_type == "EARLY_MOMENTUM"


def test_pullback_reclaim_is_identified():
    rows = []
    for i in range(5):
        ema = 100.0 + i * 0.2
        rows.append({
            "open": ema + 0.1, "high": ema + 0.8, "low": ema,
            "close": ema + 0.2, "ema9": ema, "ema20": ema - 0.5,
            "ema50": ema - 1.5, "vwap": 99.0, "rsi14": 58.0,
            "atr14": 2.0, "momentum": 0.001, "volume": 110.0,
            "volume_sma20": 100.0,
        })
    rows.append({
        "open": 101.1, "high": 102.0, "low": 100.9, "close": 101.5,
        "ema9": 101.0, "ema20": 100.5, "ema50": 99.5, "vwap": 100.0,
        "rsi14": 60.0, "atr14": 2.0, "momentum": 0.002,
        "volume": 120.0, "volume_sma20": 100.0,
    })
    result = evaluate_entry_timing(
        Signal("BULLISH", "CE", True, ()), pd.DataFrame(rows)
    )
    assert result.action == "ENTER"
    assert result.setup_type == "PULLBACK_RECLAIM"
