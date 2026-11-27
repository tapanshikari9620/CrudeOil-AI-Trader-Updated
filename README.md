# CrudeOil-AI-Trader — Paper Trading and Read-Only Market Data

The application retains the Phase 1 foundation and paper-trading workflow. Phase 2 adds
read-only Upstox market-data integration for verified MCX crude contracts. Market-hours
validation uses GET requests only and does not place, modify, or cancel broker orders.

## Requirements
- Python 3.12+ (Python 3.13 is supported by the foundation dependencies)
- Windows 10/11 or another Python-compatible OS

## Windows PowerShell setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

If `py -3.13` is unavailable, use `python -m venv .venv` with your installed Python.

## Initialize database

```powershell
python -m scripts.initialize_database
```

## Run foundation application

```powershell
python main.py
```

## Configure read-only Upstox market data

Set `UPSTOX_ACCESS_TOKEN` in the local `.env` file. Never commit or print the token.
Keep `TRADING_MODE=PAPER`. The dashboard reads local status and saved data only; it
does not make broker API calls.

During an MCX session, run the bounded diagnostic:

```powershell
python -m scripts.run_market_hours_validation
```

The command selects verified active futures and a same-expiry CE/PE pair, checks MCX
market status, requests full quotes, validates and persists only valid quotes, and
retrieves daily futures candles for the preceding 30 calendar days. It writes the
credential-free report to `data/market_hours_validation_latest.json`. A zero LTP does
not become a valid quote or get replaced by OHLC close. The default HTTP timeout is
15 seconds with at most two retries; options can be adjusted with `--timeout`,
`--retries`, `--retry-backoff`, and `--historical-days`.

To run it manually with Windows Task Scheduler, create a task for the next MCX session
that starts in the project directory and invokes
`python -m scripts.run_market_hours_validation`. Do not configure an unattended repeat
loop. Multiple runs are safe: quote and candle keys prevent duplicate database rows.

The separate `python -m scripts.test_upstox_connectivity` command is retained for
explicit API discovery diagnostics. A Profile API failure is not an Analytics Token
market-data test; that API has a registered-static-IP requirement.

## Continuous MCX futures and options collection

The continuous collector uses read-only Upstox REST GET requests. It discovers an
active crude futures contract from verified MCX metadata, discovers related CE/PE
contracts from the option-contract API (using the instrument master as a fallback),
and polls Full Market Quotes. It does not depend on the unfinished WebSocket decoder
and never calls an order endpoint.

Set `TRADING_MODE=PAPER` and add `UPSTOX_ACCESS_TOKEN` to the local `.env` file.
Configure `LIVE_FUTURES_POLL_SECONDS`, `LIVE_OPTIONS_POLL_SECONDS`,
`LIVE_MAX_CONTRACTS_PER_CYCLE` (up to 500), `LIVE_API_TIMEOUT_SECONDS`,
`LIVE_RETRY_COUNT` (0-3), `LIVE_RETRY_BACKOFF_SECONDS`,
`LIVE_QUOTE_FRESHNESS_SECONDS`, `MCX_MARKET_OPEN_TIME`, `MCX_MARKET_CLOSE_TIME`,
and `LIVE_CONTRACT_REFRESH_SECONDS` as needed. Hours are interpreted in
`Asia/Kolkata`; the local hours and Upstox MCX market status must both indicate an
open market before quotes are persisted. Exchange holidays therefore do not create
snapshots.

Start and stop the collector from PowerShell:

```powershell
python -m scripts.start_live_collector
# Press Ctrl+C to stop cleanly.
```

Futures snapshots are in `market_quote_snapshots`; CE/PE snapshots are in
`option_snapshots`. Both tables use an observation timestamp and retain the API
response timestamp and actual last-trade timestamp separately. Only positive-LTP,
metadata-verified, non-expired quotes with a recent last trade are persisted as
`VALID_FRESH`. Stale quotes, malformed/missing quotes and API failures are written
to `collection_quality_events`, never as valid prices. LTP is never inferred from
OHLC close. The credential-free status report is
`data/live_collection_health.json`.

Check the newest persisted records with:

```powershell
@'
from sqlalchemy import text
from database.db_connection import engine

with engine.connect() as connection:
    print("futures:", connection.execute(text(
        "SELECT instrument_key, trading_symbol, timestamp, last_price, last_trade_time "
        "FROM market_quote_snapshots WHERE option_type IS NULL "
        "AND response_timestamp IS NOT NULL AND quality_status = 'VALID_FRESH' "
        "ORDER BY timestamp DESC LIMIT 5"
    )).all())
    print("options:", connection.execute(text(
        "SELECT instrument_key, trading_symbol, option_type, expiry, timestamp, ltp, last_trade_time "
        "FROM option_snapshots WHERE response_timestamp IS NOT NULL "
        "AND quality_status = 'VALID_FRESH' ORDER BY timestamp DESC LIMIT 10"
    )).all())
'@ | python
```

Requests are sequential and use the Full Market Quotes maximum of 500 keys per
request, with bounded retries for timeouts, network errors, server errors, and
rate-limit responses. The configured polling periods should be kept within your
Upstox subscription's API limits. A successful HTTP response alone is not treated as
a fresh trade.

## Entry-timing layer (PAPER only)

The existing `strategy.signal_engine.generate_signal()` remains unchanged. The new
`strategy.entry_timing_engine.evaluate_entry_timing()` adds a rule-based timing layer
for early trend candidates, EMA pullback/reclaim, range breakout, and late-entry rejection.
When the full existing signal is not yet valid, an early candidate requires aligned
VWAP, EMA9/EMA20/EMA50 structure, and directional momentum before the timing patterns
can return `ENTER`. This is deterministic rule logic, not a trained ML model. It must be
validated with out-of-sample futures and option-premium data before performance claims.

`MAX_TRADES_PER_DAY` defaults to 5 and is capped at 5. The controller still permits
only one open paper position at a time. Fixed rupee loss/target gates are not used;
ATR-derived initial stops, risk-multiple targets, and one-way trailing stops remain.
`MAX_PAPER_LOTS_PER_TRADE` remains a separate exposure ceiling (default 1 lot), not a
rupee-loss limit.

## Run tests

```powershell
pytest -q
```

## Database tables
- `market_candles`: normalized underlying/market OHLCV candles
- `market_quote_snapshots`: validated full market quote snapshots
- `option_snapshots`: validated CE/PE option quote snapshots
- `verified_instruments` and `raw_instrument_metadata`: normalized contracts and
  separately versioned official source metadata
- `system_events`: application events

All timestamps supplied to persistence should be timezone-aware. SQLite may return stored datetimes without timezone metadata; normalize them at application boundaries.

## Safety
- `TRADING_MODE` defaults to `PAPER`.
- LIVE trading remains disabled.
- Upstox Phase 2 requests are read-only GETs; no broker order endpoint is called.
- Never commit `.env`, access tokens, database files or secrets.

## Known limitations
- Live market quotes are only confirmed when the current validation returns fresh,
  positive-LTP data; closed-market zero-LTP responses are rejected.
- Streaming remains unavailable until the official Upstox V3 Protobuf definitions and
  generated decoder are available; REST collection remains independent.
- Historical option-premium candles and option implied volatility are not inferred
  from futures candles or full-quote responses.
- Additive schema changes preserve existing SQLite rows.
