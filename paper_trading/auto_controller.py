from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config.settings import Settings, settings
from data_collector.continuous_live_collector import ContinuousMarketDataCollector
from data_collector.historical_collector import HistoricalCollector
from database.repository import Repository
from execution.upstox_client import UpstoxClient
from paper_trading.paper_execution_engine import DuplicateOpenPosition, PaperExecutionEngine
from risk_management.position_sizing import calculate_position_size
from risk_management.risk_validator import validate_trade
from risk_management.stoploss import stop_from_premium
from risk_management.target_calculator import target_from_risk
from strategy.features import build_features
from strategy.signal_engine import generate_signal
from strategy.entry_timing_engine import evaluate_entry_timing

logger = logging.getLogger("crude_oil_trader.paper_controller")
MARKET_TZ = ZoneInfo("Asia/Kolkata")
CANDLE_INTERVAL_MINUTES = 5
CANDLE_REFRESH_SECONDS = 60
MAX_CANDLE_AGE_SECONDS = 600
MAX_EXECUTION_QUOTE_AGE_SECONDS = 30
MAX_OPEN_POSITIONS = 1
HISTORICAL_LOOKBACK_DAYS = 10
MAX_CANDLE_HISTORY = 500


def as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite may return naive values for DateTime(timezone=True); stored values are UTC.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def round_down_tick(price: float, tick: float) -> float:
    return round(math.floor((price + 1e-12) / tick) * tick, 8)


def round_up_tick(price: float, tick: float) -> float:
    return round(math.ceil((price - 1e-12) / tick) * tick, 8)


class PaperTradingController:
    """Read-only Upstox data + local PAPER fills. No broker order API is used."""

    def __init__(
        self,
        client: UpstoxClient,
        repository: Repository,
        execution: PaperExecutionEngine,
        config: Settings = settings,
        *,
        sleep=time.sleep,
    ):
        self.client = client
        self.repository = repository
        self.execution = execution
        self.config = config
        self.sleep = sleep
        self.collector = ContinuousMarketDataCollector(client, repository, config=config)
        self._last_options_poll = 0.0
        self._last_candle_fetch = 0.0
        self._last_signal_candle: datetime | None = None
        self._last_candle_frame: pd.DataFrame | None = None
        self._historical_warmup_done = False

    def prepare(self) -> None:
        self.config.validate()
        if self.config.trading_mode != "PAPER":
            raise RuntimeError("Safety stop: only TRADING_MODE=PAPER is supported")
        if not self.config.upstox_access_token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing; its value will not be displayed")
        self.collector.prepare()
        if not self.collector.future or not self.collector.options:
            raise RuntimeError("Verified crude futures and linked option contracts are required")
        self._warm_up_historical_candles()
        logger.info(
            "PAPER controller ready: future=%s; linked option contracts=%d",
            self.collector.future.get("trading_symbol"), len(self.collector.options),
        )

    def run_forever(self) -> None:
        logger.info("PAPER trading loop started. Live order execution is disabled.")
        while True:
            now = datetime.now(timezone.utc)
            try:
                cycle = self.collector.run_cycle(now)
                if not cycle.get("market_open"):
                    self.sleep(2)
                    continue

                monotonic_now = time.monotonic()
                if monotonic_now - self._last_options_poll >= self.config.live_options_poll_seconds:
                    self._poll_options_and_monitor(now, monotonic_now)

                if monotonic_now - self._last_candle_fetch >= CANDLE_REFRESH_SECONDS:
                    self._last_candle_fetch = monotonic_now
                    self._evaluate_new_signal(now)

            except KeyboardInterrupt:
                logger.info("Ctrl+C received; stopping PAPER controller")
                break
            except Exception:
                logger.exception("PAPER controller cycle failed; will retry next cycle")
            self.sleep(min(1.0, self.config.live_futures_poll_seconds))

        self.collector.shutdown()
        logger.info("PAPER controller stopped")

    def _poll_options_and_monitor(
            self,
            now: datetime,
            monotonic_now: float,
    ) -> None:
        """
        Collect option quotes and monitor open paper positions independently.

        A failed options collection must never prevent an attempt to monitor
        existing positions. The execution engine still enforces quote freshness.
        """
        self._last_options_poll = monotonic_now

        try:
            stats = self.collector.collect_options_cycle(now)
            logger.info("Option quote cycle: %s", stats)
        except Exception:
            logger.exception(
                "PAPER option quote collection failed; "
                "continuing open-position monitoring"
            )

        try:
            self._monitor_open_positions(now)
        except Exception:
            logger.exception(
                "PAPER open-position monitoring cycle failed; "
                "will retry next cycle"
            )

    def _warm_up_historical_candles(self) -> None:
        """Idempotently seed recent completed 5-minute candles for the active future."""
        if self._historical_warmup_done:
            return

        future = self.collector.future
        future_key = future["instrument_key"]
        today = datetime.now(MARKET_TZ).date()
        from_date = (today - timedelta(days=HISTORICAL_LOOKBACK_DAYS)).isoformat()
        to_date = today.isoformat()

        try:
            result = HistoricalCollector(
                self.client,
                instrument_metadata=future,
            ).collect(
                future_key,
                "minutes",
                CANDLE_INTERVAL_MINUTES,
                to_date,
                from_date,
            )
            logger.info(
                "Historical candle warm-up completed for %s: %s",
                future.get("trading_symbol", future_key),
                result,
            )
        except Exception:
            logger.exception(
                "Historical candle warm-up failed for %s; using stored/intraday history",
                future_key,
            )
        finally:
            self._historical_warmup_done = True

    def _fetch_closed_futures_candles(self, now: datetime) -> pd.DataFrame:
        future = self.collector.future
        future_key = future["instrument_key"]

        payload = self.client.intraday_candles(
            future_key, "minutes", CANDLE_INTERVAL_MINUTES
        )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Unexpected Upstox intraday-candle response")
        data = payload.get("data")
        candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(candles, list):
            raise ValueError("Upstox returned an invalid intraday-candle list")

        intraday_rows = []
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            timestamp = as_utc(candle[0])
            if timestamp is None:
                continue
            if timestamp + timedelta(minutes=CANDLE_INTERVAL_MINUTES) > now:
                continue
            try:
                row = {
                    "timestamp": timestamp,
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
                if len(candle) > 6 and candle[6] is not None:
                    row["open_interest"] = float(candle[6])
                values = [row[k] for k in ("open", "high", "low", "close", "volume")]
                if not all(math.isfinite(value) for value in values):
                    continue
                if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
                    continue
                if row["volume"] < 0:
                    continue
                if row["high"] < max(row["open"], row["close"], row["low"]):
                    continue
                if row["low"] > min(row["open"], row["close"], row["high"]):
                    continue
                intraday_rows.append(row)
            except (TypeError, ValueError, OverflowError):
                continue

        for row in intraday_rows:
            self.repository.add_candle({
                "instrument_key": future_key,
                "exchange": "MCX",
                "instrument_type": "FUT",
                "interval": "5_min",
                "timestamp": row["timestamp"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "open_interest": row.get("open_interest"),
                "source": "upstox_intraday_v3",
            })

        stored = self.repository.get_market_candles(
            future_key,
            interval="5_min",
            limit=MAX_CANDLE_HISTORY,
        )
        rows_by_timestamp = {}
        for candle in stored:
            timestamp = as_utc(candle.timestamp)
            if timestamp is None:
                continue
            if timestamp + timedelta(minutes=CANDLE_INTERVAL_MINUTES) > now:
                continue
            rows_by_timestamp[timestamp] = {
                "timestamp": timestamp,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
            }

        for row in intraday_rows:
            rows_by_timestamp[row["timestamp"]] = row

        if not rows_by_timestamp:
            raise ValueError("No valid completed futures candles available")

        frame = (
            pd.DataFrame(rows_by_timestamp.values())
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .tail(MAX_CANDLE_HISTORY)
            .reset_index(drop=True)
        )
        latest_time = as_utc(frame.iloc[-1]["timestamp"])
        if latest_time is None:
            raise ValueError("Latest completed futures candle has an invalid timestamp")
        age = (now - latest_time).total_seconds()
        if age < 0 or age > MAX_CANDLE_AGE_SECONDS:
            raise ValueError(f"Latest completed futures candle is stale ({age:.0f}s)")

        logger.debug(
            "Candle history ready for %s: stored=%d intraday=%d combined=%d",
            future.get("trading_symbol", future_key),
            len(stored),
            len(intraday_rows),
            len(frame),
        )
        return frame

    def _evaluate_new_signal(self, now: datetime) -> None:
        frame = self._fetch_closed_futures_candles(now)
        if len(frame) < 60:
            logger.info("No entry: need at least 60 completed five-minute candles; have %d", len(frame))
            return
        featured = build_features(frame)
        latest = featured.iloc[-1]
        candle_time = as_utc(latest["timestamp"])
        if candle_time is None:
            logger.warning("No entry: latest candle timestamp is invalid")
            return
        if self._last_signal_candle is not None and candle_time <= self._last_signal_candle:
            return
        self._last_signal_candle = candle_time

        row = latest.to_dict()
        signal = generate_signal(row)
        timing = evaluate_entry_timing(signal, featured)
        logger.info(
            "ENTRY TIMING %s: action=%s setup=%s score=%d extension_atr=%s reasons=%s",
            candle_time.isoformat(), timing.action, timing.setup_type, timing.score,
            timing.extension_atr, timing.reasons,
        )
        if timing.action != "ENTER" or timing.option_type not in {"CE", "PE"}:
            logger.info(
                "No entry: existing_signal=%s/%s timing=%s (%s)",
                signal.direction, signal.valid, timing.action, timing.reasons,
            )
            return
        trade_option_type = timing.option_type

        trades = self.repository.get_paper_trades()
        today = now.astimezone(MARKET_TZ).date()
        todays = [t for t in trades if self._trade_day(t) == today]
        open_trades = [t for t in trades if t.status == "OPEN"]
        if open_trades:
            logger.info("No entry: an open PAPER position already exists")
            return
        option, quote = self._select_option(trade_option_type, float(latest["close"]), now)
        if option is None or quote is None:
            logger.info("No entry: no fresh, liquid, executable %s quote near ATM", trade_option_type)
            return

        bid = finite_positive(quote.bid_price)
        ask = finite_positive(quote.ask_price)
        tick = finite_positive(option.get("tick_size"))
        lot_size = option.get("lot_size")
        if bid is None or ask is None or tick is None or not isinstance(lot_size, int) or lot_size <= 0:
            logger.info("No entry: option quote or contract lot/tick metadata is incomplete")
            return
        if ask < bid or quote.bid_quantity is None or quote.ask_quantity is None:
            logger.info("No entry: invalid or incomplete option market depth")
            return
        if float(quote.bid_quantity or 0) <= 0 or float(quote.ask_quantity or 0) <= 0:
            logger.info("No entry: no executable quantity at best bid/ask")
            return

        estimated_entry = ask + tick  # conservative one-tick simulated entry slippage
        underlying_price = finite_positive(latest.get("close"))
        underlying_atr = finite_positive(latest.get("atr14"))
        if underlying_price is None or underlying_atr is None:
            logger.info("No entry: underlying ATR/price is unavailable for adaptive risk calculation")
            return
        underlying_atr_pct = underlying_atr / underlying_price
        stop = round_down_tick(
            stop_from_premium(
                estimated_entry,
                underlying_atr_pct=underlying_atr_pct,
                atr_multiplier=self.config.atr_stop_multiplier,
                min_risk_fraction=self.config.min_stop_risk_fraction,
                max_risk_fraction=self.config.max_stop_risk_fraction,
            ),
            tick,
        )
        sizing = calculate_position_size(
            estimated_entry,
            stop,
            lot_size,
            slippage_per_unit=tick,
            max_lots=self.config.max_paper_lots_per_trade,
        )
        if not sizing.allowed:
            logger.info("No entry: configured paper exposure ceiling produced no valid lot size")
            return
        # Stronger aligned momentum permits a larger reward multiple; the target
        # remains derived from this trade's actual premium risk, never fixed INR.
        momentum = float(latest.get("momentum") or 0.0)
        aligned_momentum = momentum if trade_option_type == "CE" else -momentum
        reward_multiple = self.config.base_reward_multiple * (
            1.0 + min(max(aligned_momentum, 0.0) * 20.0, 0.5)
        )
        target = round_up_tick(target_from_risk(estimated_entry, stop, reward_multiple), tick)
        if not stop < estimated_entry < target:
            logger.info("No entry: calculated stop/target does not bracket estimated entry")
            return

        quote_time = as_utc(quote.response_timestamp)
        if quote_time is None:
            logger.info("No entry: option quote-response timestamp is missing")
            return
        decision = validate_trade(
            quantity=sizing.quantity,
            entry=estimated_entry,
            stop=stop,
            lot_size=lot_size,
            trades_today=len(todays),
            open_positions=len(open_trades),
            data_timestamp=quote_time,
            max_data_age_seconds=MAX_EXECUTION_QUOTE_AGE_SECONDS,
            max_trades=self.config.max_trades_per_day,
            max_positions=MAX_OPEN_POSITIONS,
            now=now,
        )
        if not decision.allowed:
            logger.info("No entry: risk validation rejected trade: %s", decision.reasons)
            return

        try:
            trade_id = self.execution.open_long_option(
                instrument_key=option["instrument_key"],
                quantity=sizing.quantity,
                bid=bid,
                ask=ask,
                tick_size=tick,
                stop_loss=stop,
                target=target,
                timestamp=now,
                option_type=trade_option_type,
            )
        except DuplicateOpenPosition:
            logger.warning("Duplicate open position prevented for %s", option["instrument_key"])
            return
        logger.warning(
            "PAPER ENTRY trade_id=%s type=%s symbol=%s qty=%d timing=%s score=%d source=%s estimated_entry=%.2f initial_stop=%.2f target=%.2f estimated_stop_risk=₹%.2f reward_multiple=%.2f; charges excluded",
            trade_id, trade_option_type, option.get("trading_symbol"), sizing.quantity,
            timing.setup_type, timing.score, timing.directional_source,
            estimated_entry, stop, target, sizing.estimated_loss, reward_multiple,
        )

    def _select_option(self, option_type: str, underlying_price: float, now: datetime):
        today = now.astimezone(MARKET_TZ).date()
        future_key = self.collector.future["instrument_key"]
        candidates = [
            item for item in self.collector.options
            if item.get("instrument_type") == option_type
            and item.get("underlying_key") == future_key
            and item.get("contract_status") == "ACTIVE"
            and date.fromisoformat(str(item.get("expiry"))) >= today
        ]
        if not candidates:
            return None, None
        nearest_expiry = min(str(item["expiry"]) for item in candidates)
        candidates = [item for item in candidates if str(item["expiry"]) == nearest_expiry]
        candidates.sort(key=lambda item: abs(float(item.get("strike_price", 0)) - underlying_price))

        for option in candidates[:20]:
            snapshot = self.repository.get_latest_market_quote(option["instrument_key"])
            if snapshot is None or snapshot.quality_status != "VALID_FRESH":
                continue
            quote_time = as_utc(snapshot.response_timestamp)
            if quote_time is None:
                continue
            age = (now - quote_time).total_seconds()
            if age < 0 or age > MAX_EXECUTION_QUOTE_AGE_SECONDS:
                continue
            bid = finite_positive(snapshot.bid_price)
            ask = finite_positive(snapshot.ask_price)
            ltp = finite_positive(snapshot.last_price)
            if bid is None or ask is None or ltp is None or ask < bid:
                continue
            if snapshot.bid_quantity is None or snapshot.ask_quantity is None:
                continue
            if float(snapshot.bid_quantity or 0) <= 0 or float(snapshot.ask_quantity or 0) <= 0:
                continue
            return option, snapshot
        return None, None

    def _monitor_open_positions(self, now: datetime) -> None:
        open_trades = [t for t in self.repository.get_paper_trades() if t.status == "OPEN"]
        if not open_trades:
            return

        # Entry selection may use a capped option universe. An already-open
        # contract must still be quoted and monitored even when it falls outside
        # that entry universe.
        options_by_key = {item["instrument_key"]: item for item in self.collector.options}
        verified_by_key = {
            item.instrument_key: item
            for item in self.repository.get_verified_instruments()
        }
        missing_contracts = []
        for trade in open_trades:
            if trade.instrument_key in options_by_key:
                continue
            verified = verified_by_key.get(trade.instrument_key)
            if verified is None or verified.instrument_type not in {"CE", "PE"}:
                logger.error("Open PAPER trade %s has no verified option metadata", trade.id)
                continue
            expiry = str(verified.expiry)
            if verified.contract_status != "ACTIVE" or date.fromisoformat(expiry) < now.astimezone(MARKET_TZ).date():
                logger.error("Open PAPER trade %s contract is not active; monitoring quote request skipped", trade.id)
                continue
            option = {
                "instrument_key": verified.instrument_key,
                "trading_symbol": verified.trading_symbol,
                "exchange": verified.exchange,
                "segment": verified.segment,
                "instrument_type": verified.instrument_type,
                "expiry": expiry,
                "strike_price": verified.strike_price,
                "lot_size": verified.lot_size,
                "tick_size": verified.tick_size,
                "underlying_key": verified.underlying_key,
                "underlying_symbol": verified.underlying_symbol,
                "contract_status": verified.contract_status,
            }
            options_by_key[trade.instrument_key] = option
            missing_contracts.append(option)

        if missing_contracts:
            try:
                self.collector._collect_quotes(
                    "OPEN_POSITIONS",
                    missing_contracts,
                    now,
                )
            except Exception:
                logger.exception(
                    "PAPER MONITOR: additional quote collection failed; "
                    "checking existing snapshots with normal freshness validation"
                )

        for trade in open_trades:
            option = options_by_key.get(trade.instrument_key)
            if option is None:
                continue
            snapshot = self.repository.get_latest_market_quote(trade.instrument_key)
            if snapshot is None:
                logger.warning(
                    "PAPER MONITOR: no market quote available for open trade_id=%s instrument=%s",
                    trade.id,
                    trade.instrument_key,
                )
                continue
            quality_status = getattr(snapshot, "quality_status", None)

            if quality_status != "VALID_FRESH":
                logger.warning(
                    "PAPER MONITOR: quote quality rejected for trade_id=%s "
                    "instrument=%s quality_status=%s",
                    trade.id,
                    trade.instrument_key,
                    quality_status,
                )
                continue

            quote_time = as_utc(snapshot.response_timestamp)

            if quote_time is None:
                logger.warning(
                    "PAPER MONITOR: invalid response timestamp for trade_id=%s "
                    "instrument=%s",
                    trade.id,
                    trade.instrument_key,
                )
                continue
            quote = {
                "instrument_key": trade.instrument_key,
                "option_type": option.get("instrument_type"),
                "timestamp": quote_time,  # freshness uses Upstox quote-response time
                "bid_price": snapshot.bid_price,
                "ask_price": snapshot.ask_price,
                "ltp": snapshot.last_price,
                "tick_size": option.get("tick_size"),
            }
            try:
                outcomes = self.execution.process_quote(
                    quote, now=now, max_age_seconds=MAX_EXECUTION_QUOTE_AGE_SECONDS, charges=0.0
                )
                for outcome in outcomes:
                    if outcome.get("status") == "OPEN" and outcome.get("stop_updated"):
                        logger.info(
                            "PAPER TRAIL trade_id=%s previous_stop=%.2f new_stop=%.2f peak_bid=%.2f",
                            trade.id, outcome["previous_stop_loss"], outcome["stop_loss"],
                            outcome["peak_mark_price"],
                        )
                    if outcome.get("status") == "CLOSED":
                        logger.warning(
                            "PAPER EXIT trade_id=%s reason=%s exit=%.2f net_pnl=%.2f (charges excluded)",
                            outcome["trade_id"], outcome["exit_reason"], outcome["exit_price"], outcome["net_pnl"],
                        )
            except ValueError as exc:
                logger.warning(
                    "PAPER MONITOR: quote rejected for trade_id=%s instrument=%s reason=%s",
                    trade.id,
                    trade.instrument_key,
                    exc,
                )

    @staticmethod
    def _trade_day(trade) -> date | None:
        opened = as_utc(getattr(trade, "opened_at", None))
        return opened.astimezone(MARKET_TZ).date() if opened else None
