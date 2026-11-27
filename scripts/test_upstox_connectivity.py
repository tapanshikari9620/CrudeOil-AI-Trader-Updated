"""Run an explicit, GET-only Upstox market-data connectivity check."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
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


def _report_api_error(label: str, error: UpstoxAPIError) -> None:
    if isinstance(error, UpstoxAuthenticationError):
        print(f"{label}: FAIL (HTTP 401; cause not determined)")
    elif isinstance(error, UpstoxPermissionError):
        diagnostics = [f"HTTP {error.status_code or 403}"]
        if error.api_error_code:
            diagnostics.append(f"Upstox error code {error.api_error_code}")
        if error.request_id:
            diagnostics.append(f"request ID {error.request_id}")
        print(f"{label}: ACCESS DENIED ({'; '.join(diagnostics)})")
    else:
        print(f"{label}: FAIL ({type(error).__name__}; HTTP {error.status_code or 'unavailable'})")


def _discover_from_official_master(repository: Repository) -> dict[str, list[dict]]:
    """Use Upstox's official MCX instrument master as a discovery fallback."""
    collector = InstrumentCollector()
    results: dict[str, list[dict]] = {kind: [] for kind in ("FUT", "CE", "PE")}
    seen: set[str] = set()
    market_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    for row in collector.crude_contracts(collector.fetch()):
        kind = row.get("instrument_type")
        if kind not in results:
            continue
        normalized = UpstoxReadOnlyService._normalize_instrument(
            row, kind, metadata_source="official_instrument_master"
        )
        if normalized["expiry"] < market_today.isoformat() or normalized["instrument_key"] in seen:
            continue
        results[kind].append(normalized)
        seen.add(normalized["instrument_key"])
    if not seen:
        raise ValueError("Official MCX instrument master contained no current crude contracts")
    for rows in results.values():
        for item in rows:
            repository.save_verified_instrument(item)
    return results


def _not_tested_quote(reason: str) -> None:
    print(f"Market quote authentication: NOT TESTED ({reason})")
    print("Actual market quote retrieval: NOT TESTED")
    print("Quote validation: NOT TESTED")
    print("Market quote database persistence: NOT TESTED")


def main() -> int:
    print(f"Token loaded: {'YES' if settings.upstox_access_token else 'NO'}")
    if not settings.upstox_access_token:
        print("Instrument discovery: BLOCKED (UPSTOX_ACCESS_TOKEN is not configured)")
        _not_tested_quote("no configured token")
        print("Database persistence: NOT TESTED")
        print("Profile API: NOT TESTED (registered static IP required)")
        return 2

    client = None
    try:
        settings.validate()
        initialize_database()
        repository = Repository()
        client = UpstoxClient(settings.upstox_access_token, base_url=settings.upstox_api_base)
        service = UpstoxReadOnlyService(client, repository)

        try:
            discovered = service.discover_crude_instruments()
            discovery_source = "Instrument Search API"
        except UpstoxAPIError as exc:
            _report_api_error("Instrument Search API", exc)
            try:
                discovered = _discover_from_official_master(repository)
                discovery_source = "official MCX instrument master fallback"
            except Exception as master_error:
                print(f"Instrument discovery: FAIL ({type(master_error).__name__})")
                _not_tested_quote("no verified instrument key")
                print("Database persistence: NOT TESTED (no verified instrument metadata)")
                _check_profile_api(service)
                return 1

        if not any(discovered.values()):
            try:
                discovered = _discover_from_official_master(repository)
                discovery_source = "official MCX instrument master (Search API returned no matches)"
            except Exception as master_error:
                print(
                    "Instrument discovery: FAIL "
                    f"(Search API returned no matches; official master fallback: {type(master_error).__name__})"
                )
                _not_tested_quote("no verified instrument key")
                print("Database persistence: NOT TESTED (no verified instrument metadata)")
                _check_profile_api(service)
                return 1

        counts = {kind: len(rows) for kind, rows in discovered.items()}
        print(
            f"Instrument discovery: PASS via {discovery_source} "
            f"(FUT={counts['FUT']}, CE={counts['CE']}, PE={counts['PE']})"
        )
        print("Database persistence: PASS (verified instrument metadata stored)")
        contracts = [item for rows in discovered.values() for item in rows]
        if not contracts:
            _not_tested_quote("no verified crude contract")
            print("Market quote database persistence: NOT TESTED")
            _check_profile_api(service)
            return 1

        chosen = next((item for item in discovered["FUT"]), contracts[0])
        try:
            result = service.fetch_and_persist_quotes([chosen])
        except UpstoxAuthenticationError:
            print("Market quote authentication: FAIL (quote endpoint returned HTTP 401; cause not determined)")
            print("Actual market quote retrieval: FAIL (HTTP 401)")
            print("Quote validation: NOT TESTED")
            print("Market quote database persistence: NOT TESTED")
            _check_profile_api(service)
            return 1
        except UpstoxPermissionError as exc:
            _report_api_error("Market quote authentication", exc)
            print("Actual market quote retrieval: FAIL (HTTP 403)")
            print("Quote validation: NOT TESTED")
            print("Market quote database persistence: NOT TESTED")
            _check_profile_api(service)
            return 1
        except UpstoxAPIError as exc:
            _report_api_error("Market quote retrieval", exc)
            print("Market quote authentication: NOT CONFIRMED")
            print("Actual market quote retrieval: FAIL")
            print("Quote validation: NOT TESTED")
            print("Market quote database persistence: NOT TESTED")
            _check_profile_api(service)
            return 1

        if result["accepted"]:
            quote = result["accepted"][0]
            print("Market quote authentication: PASS (market-data endpoint returned HTTP 200)")
            print("Actual market quote retrieval: PASS (Full Market Quotes returned a matching quote)")
            print("Quote validation: PASS (price, timestamp, freshness, and available depth/fields)")
            print(f"Market quote database persistence: PASS (new snapshot={quote['persisted']})")
            exit_code = 0
        else:
            reason = result["rejected"][0]["reason"] if result["rejected"] else "no matching quote"
            print("Market quote authentication: PASS (market-data endpoint returned HTTP 200)")
            print("Actual market quote retrieval: FAIL (no usable matching quote)")
            print(f"Quote validation: FAIL ({reason})")
            print("Market quote database persistence: NOT TESTED (no quote passed validation)")
            exit_code = 1

        _check_profile_api(service)
        return exit_code
    except Exception as exc:
        if isinstance(exc, UpstoxAPIError):
            _report_api_error("Connectivity", exc)
        else:
            print(f"Connectivity: FAIL ({type(exc).__name__})")
        return 1
    finally:
        if client is not None:
            client.close()


def _check_profile_api(service: UpstoxReadOnlyService) -> None:
    try:
        service.check_profile_api()
    except UpstoxAPIError as exc:
        if exc.status_code == 401:
            print("Profile API: HTTP 401 (registered static IP required; not used to judge Analytics Token validity)")
        elif isinstance(exc, UpstoxPermissionError):
            _report_api_error("Profile API (registered static IP required)", exc)
        else:
            print(f"Profile API: NOT CONFIRMED ({type(exc).__name__})")
    else:
        print("Profile API: PASS (registered static IP access available)")


if __name__ == "__main__":
    raise SystemExit(main())
