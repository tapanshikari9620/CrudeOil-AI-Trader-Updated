"""Run a bounded, GET-only MCX crude market-data validation."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from data_collector.contract_selection import CrudeContractSelector
from data_collector.historical_collector import HistoricalCollector
from data_collector.instrument_collector import InstrumentCollector
from data_collector.upstox_read_only import UpstoxReadOnlyService
from database.db_connection import initialize_database
from database.repository import Repository
from execution.upstox_client import (
    UpstoxAPIError,
    UpstoxAuthenticationError,
    UpstoxClient,
    UpstoxPermissionError,
)

MARKET_TZ = ZoneInfo("Asia/Kolkata")
CLOSED_STATUSES = {"CLOSED", "NORMAL_CLOSE", "CLOSED_FOR_THE_DAY", "MARKET_CLOSED"}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _market_status_value(payload: Any) -> str | None:
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status.upper() not in {"SUCCESS", "ERROR"}:
            return status.upper()
        for value in payload.values():
            found = _market_status_value(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _market_status_value(value)
            if found:
                return found
    return None


def classify_quote(instrument: dict, quote_result: dict, market_status: str | None) -> str:
    if instrument.get("contract_status") == "EXPIRED":
        return "CONTRACT_INACTIVE"
    try:
        expiry = date.fromisoformat(str(instrument["expiry"]))
    except (KeyError, TypeError, ValueError):
        return "INVALID_QUOTE"
    if expiry < datetime.now(MARKET_TZ).date():
        return "CONTRACT_INACTIVE"

    accepted = quote_result.get("accepted", [])
    if any(item.get("instrument_key") == instrument.get("instrument_key") for item in accepted):
        return "VALID_MARKET_DATA"
    rejected = next(
        (item for item in quote_result.get("rejected", [])
         if item.get("instrument_key") == instrument.get("instrument_key")),
        None,
    )
    if rejected is None:
        return "INVALID_QUOTE"
    evidence = rejected.get("quote_evidence") or {}
    if market_status in CLOSED_STATUSES:
        return "MARKET_CLOSED"
    last_price = evidence.get("last_price")
    volume = evidence.get("volume")
    last_trade = evidence.get("last_trade_time")
    if last_price in (None, 0, "0") and volume in (None, 0, "0") and last_trade in (None, 0, "0", ""):
        return "NO_TRADE"
    return "INVALID_QUOTE"


def _contract_summary(instrument: Any) -> dict[str, Any]:
    fields = (
        "instrument_key", "trading_symbol", "exchange", "segment", "instrument_type",
        "underlying_symbol", "expiry", "strike_price", "lot_size", "tick_size", "contract_status",
    )
    if isinstance(instrument, dict):
        return {name: instrument.get(name) for name in fields}
    return {name: getattr(instrument, name, None) for name in fields}


def _discover(
    repository: Repository,
    service: UpstoxReadOnlyService,
    product: str,
) -> tuple[dict, str]:
    product = str(product).upper().strip()

    if product not in {"CRUDEOIL", "CRUDEOILM"}:
        raise ValueError(f"Unsupported MCX product: {product}")
    existing = repository.get_verified_instruments()
    try:
        selected = CrudeContractSelector.select_from(
            existing,
            product=product,
        )
        if selected["call"] is not None and selected["put"] is not None:
            return {
                label: _contract_summary(item) if item is not None else None
                for label, item in selected.items()
            }, "verified local instrument metadata"
    except ValueError:
        pass

    try:
        discovered = service.discover_crude_instruments()
        if any(discovered.values()):
            source = "Instrument Search API"
        else:
            raise ValueError("Instrument Search API returned no crude contracts")
    except (UpstoxAPIError, ValueError):
        collector = InstrumentCollector()
        rows = collector.crude_contracts(
            collector.fetch(),
            product=product,
        )
        discovered = {kind: [] for kind in ("FUT", "CE", "PE")}
        for row in rows:
            kind = row.get("instrument_type")
            if kind not in discovered:
                continue
            instrument = service._normalize_instrument(
                row, kind, metadata_source="official_instrument_master"
            )
            if instrument["contract_status"] != "EXPIRED":
                discovered[kind].append(instrument)
                repository.save_verified_instrument(instrument)
        source = "official MCX instrument master fallback"

    contracts = [item for group in discovered.values() for item in group]
    selection = CrudeContractSelector.select_from(
        contracts,
        product=product,
    )
    return {label: _contract_summary(item) if item is not None else None
            for label, item in selection.items()}, source


def _classify_api_error(error: UpstoxAPIError) -> str:
    if isinstance(error, UpstoxAuthenticationError):
        return "AUTHENTICATION_FAILED"
    if isinstance(error, UpstoxPermissionError):
        return "ACCESS_DENIED"
    return "API_UNAVAILABLE"


def run_validation(
    *,
    timeout: float = 15,
    retries: int = 2,
    retry_backoff_seconds: float = 1,
    historical_days: int = 30,
    repository: Repository | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run read-only checks and return a credential-free diagnostic report."""
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc),
        "trading_mode": settings.trading_mode,
        "token_loaded": bool(settings.upstox_access_token),
        "instrument_discovery": "NOT_TESTED",
        "market_status": "UNKNOWN",
        "market_quote_authentication": "NOT_TESTED",
        "quotes": [],
        "historical_data": "NOT_TESTED",
        "database_persistence": "NOT_TESTED",
        "errors": [],
    }
    if settings.trading_mode != "PAPER":
        report["errors"].append({"stage": "safety", "classification": "BLOCKED",
                                 "message": "TRADING_MODE must be PAPER"})
        return report
    if not settings.upstox_access_token and client is None:
        report["errors"].append({"stage": "authentication", "classification": "BLOCKED",
                                 "message": "UPSTOX_ACCESS_TOKEN is not configured"})
        return report
    if isinstance(historical_days, bool) or not 1 <= historical_days <= 3650:
        raise ValueError("historical_days must be between 1 and 3650")

    initialize_database()
    repo = repository or Repository()
    own_client = client is None
    api = client or UpstoxClient(
        settings.upstox_access_token or "",
        base_url=settings.upstox_api_base,
        timeout=timeout,
        retry_count=retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    try:
        service = UpstoxReadOnlyService(api, repo)
        try:
            selected, source = _discover(
                repo,
                service,
                settings.mcx_product,
            )
            report["instrument_discovery"] = "PASS"
            report["discovery_source"] = source
        except (UpstoxAPIError, ValueError, OSError, httpx.HTTPError) as exc:
            report["instrument_discovery"] = "FAIL"
            report["errors"].append({
                "stage": "instrument_discovery",
                "classification": _classify_api_error(exc) if isinstance(exc, UpstoxAPIError) else "API_UNAVAILABLE",
                "error_type": type(exc).__name__,
            })
            return report

        chosen = [selected["future"], selected["call"], selected["put"]]
        report["selected_contracts"] = {
            label: _contract_summary(instrument)
            for label, instrument in zip(("future", "call", "put"), chosen)
        }
        report["database_persistence"] = "PASS (verified metadata available)"

        market_status = None
        try:
            payload = api.market_status("MCX")
            market_status = _market_status_value(payload)
            report["market_status"] = market_status or "UNKNOWN"
        except UpstoxAPIError as exc:
            report["market_status"] = "UNAVAILABLE"
            report["errors"].append({
                "stage": "market_status",
                "classification": _classify_api_error(exc),
                "http_status": exc.status_code,
                "request_id": exc.request_id,
                "api_error_code": exc.api_error_code,
            })

        try:
            quote_result = service.fetch_and_persist_quotes(chosen)
            report["market_quote_authentication"] = "CONFIRMED (market-data GET returned success)"
            report["actual_quote_retrieval"] = "HTTP success"
            for instrument in chosen:
                classification = classify_quote(instrument, quote_result, market_status)
                accepted = next(
                    (item for item in quote_result["accepted"]
                     if item["instrument_key"] == instrument["instrument_key"]),
                    None,
                )
                rejected = next(
                    (item for item in quote_result["rejected"]
                     if item["instrument_key"] == instrument["instrument_key"]),
                    None,
                )
                report["quotes"].append({
                    "instrument": _contract_summary(instrument),
                    "classification": classification,
                    "quote": accepted,
                    "rejection_reason": rejected.get("reason") if rejected else None,
                    "quote_evidence": rejected.get("quote_evidence") if rejected else None,
                    "persisted": accepted["persisted"] if accepted else False,
                })
            report["database_persistence"] = (
                "PASS (new snapshot or duplicate safely detected)"
                if any(item["classification"] == "VALID_MARKET_DATA" for item in report["quotes"])
                else "NOT TESTED (no quote passed validation)"
            )
        except UpstoxAPIError as exc:
            classification = _classify_api_error(exc)
            report["market_quote_authentication"] = (
                "FAIL" if classification == "AUTHENTICATION_FAILED" else classification
            )
            report["actual_quote_retrieval"] = "FAIL"
            report["errors"].append({
                "stage": "market_quotes",
                "classification": classification,
                "http_status": exc.status_code,
                "request_id": exc.request_id,
                "api_error_code": exc.api_error_code,
            })

        market_today = datetime.now(MARKET_TZ).date()
        end_date = market_today - timedelta(days=1)
        start_date = end_date - timedelta(days=historical_days - 1)
        try:
            history = HistoricalCollector(
                api, repo, instrument_metadata=_contract_summary(selected["future"])
            ).collect(
                selected["future"]["instrument_key"],
                "days", 1, end_date.isoformat(), start_date.isoformat(),
            )
            report["historical_data"] = "PASS" if history["received"] > 0 else "NO_DATA"
            report["historical_result"] = history
        except UpstoxAPIError as exc:
            report["historical_data"] = _classify_api_error(exc)
            report["errors"].append({
                "stage": "historical_candles",
                "classification": _classify_api_error(exc),
                "http_status": exc.status_code,
                "request_id": exc.request_id,
                "api_error_code": exc.api_error_code,
            })
        except (TypeError, ValueError, OSError) as exc:
            report["historical_data"] = "FAIL"
            report["errors"].append({
                "stage": "historical_candles",
                "classification": "INVALID_OR_UNAVAILABLE_DATA",
                "error_type": type(exc).__name__,
            })
        return report
    finally:
        if own_client:
            api.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=15, help="HTTP timeout in seconds (default: 15)")
    parser.add_argument("--retries", type=int, choices=range(0, 4), default=2,
                        help="bounded retries for transient GET failures (default: 2)")
    parser.add_argument("--retry-backoff", type=float, default=1,
                        help="base exponential backoff in seconds (default: 1)")
    parser.add_argument("--historical-days", type=int, default=30,
                        help="daily futures history lookback, 1-3650 days (default: 30)")
    parser.add_argument("--report", type=Path,
                        default=settings.data_dir / "market_hours_validation_latest.json",
                        help="credential-free JSON report output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0 or args.retry_backoff < 0:
        raise SystemExit("--timeout must be positive and --retry-backoff non-negative")
    report = run_validation(
        timeout=args.timeout,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff,
        historical_days=args.historical_days,
    )
    report["finished_at"] = datetime.now(timezone.utc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, default=_json_default, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, default=_json_default, ensure_ascii=True))
    print(f"Diagnostic report: {args.report}")
    return 0 if (
        report["quotes"]
        and all(item["classification"] == "VALID_MARKET_DATA" for item in report["quotes"])
        and report.get("historical_data") == "PASS"
        and not report["errors"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
