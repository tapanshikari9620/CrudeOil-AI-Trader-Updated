
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from database.db_connection import SessionLocal
from database.models import CandleTrainingExclusion, MarketCandle

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "research"

# Officially configured special MCX sessions.
# Five-minute timestamps include the first candle and the final
# timestamp listed below.
SPECIAL_SESSIONS = {
    "2026-11-08": ("14:20", "15:25", "DIWALI_MUHURAT"),
    "2026-11-10": ("17:00", "23:50", "BALIPRATIPADA"),
    "2026-11-24": ("17:00", "23:50", "GURU_NANAK_JAYANTI"),
}


def to_ist(timestamp: datetime) -> datetime:
    """Convert a database timestamp to IST."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(IST)


def to_utc_iso(timestamp: datetime) -> str:
    """Return a timezone-aware UTC timestamp."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat()


def expected_times(
    trading_date: date,
    start: str,
    end: str,
) -> list[datetime]:
    start_dt = datetime.combine(
        trading_date,
        datetime.strptime(start, "%H:%M").time(),
        IST,
    )
    end_dt = datetime.combine(
        trading_date,
        datetime.strptime(end, "%H:%M").time(),
        IST,
    )

    result = []
    current = start_dt

    while current <= end_dt:
        result.append(current)
        current += timedelta(minutes=5)

    return result


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="raise",
        )
        writer.writeheader()
        writer.writerows(rows)


def candle_to_row(
    candle: MarketCandle,
    local_time: datetime,
    classification: str,
) -> dict:
    return {
        "id": candle.id,
        "instrument_key": candle.instrument_key,
        "trading_date_ist": local_time.date().isoformat(),
        "timestamp_ist": local_time.isoformat(),
        "timestamp_utc": to_utc_iso(candle.timestamp),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "open_interest": candle.open_interest,
        "source": candle.source,
        "classification": classification,
    }


def main() -> None:
    session = SessionLocal()

    try:
        candles = (
            session.query(MarketCandle)
            .filter(
                MarketCandle.interval == "5_min",
                MarketCandle.instrument_type == "FUT",
            )
            .order_by(
                MarketCandle.instrument_key,
                MarketCandle.timestamp,
            )
            .all()
        )

        if not candles:
            print("No 5-minute futures candles found.")
            return

        # Read the quarantine register. Do not modify it.
        exclusions = (
            session.query(CandleTrainingExclusion)
            .order_by(CandleTrainingExclusion.candle_id)
            .all()
        )

        exclusion_by_id = {
            int(item.candle_id): item.reason
            for item in exclusions
        }

        grouped = defaultdict(list)

        for candle in candles:
            local_time = to_ist(candle.timestamp)
            grouped[
                (candle.instrument_key, local_time.date())
            ].append((candle, local_time))

        regular_rows = []
        special_rows = []
        quarantine_rows = []
        classifications = []

        exported_regular_ids = set()
        exported_special_ids = set()
        exported_quarantine_ids = set()

        for (instrument_key, trading_date), entries in sorted(grouped.items()):
            entries.sort(key=lambda item: item[1])

            actual_times = [local_time for _, local_time in entries]
            actual_set = set(actual_times)
            date_text = trading_date.isoformat()

            eligible_entries = []
            excluded_entries = []

            for candle, local_time in entries:
                candle_id = int(candle.id)

                if candle_id in exclusion_by_id:
                    excluded_entries.append((candle, local_time))
                else:
                    eligible_entries.append((candle, local_time))

            # Preserve all quarantined source records in a separate report.
            for candle, local_time in excluded_entries:
                row = candle_to_row(
                    candle,
                    local_time,
                    "QUARANTINED_FROM_TRAINING",
                )
                row["exclusion_reason"] = exclusion_by_id[int(candle.id)]

                quarantine_rows.append(row)
                exported_quarantine_ids.add(int(candle.id))

            # ---------------------------------------------------------
            # Special exchange sessions, including Diwali Muhurat.
            # ---------------------------------------------------------
            if date_text in SPECIAL_SESSIONS:
                start, end, special_name = SPECIAL_SESSIONS[date_text]

                expected = expected_times(
                    trading_date,
                    start,
                    end,
                )

                schedule_matches = (
                    actual_set == set(expected)
                    and len(actual_times) == len(expected)
                )

                if schedule_matches:
                    status = (
                        "SPECIAL_SESSION_SCHEDULE_MATCH_"
                        "PROVENANCE_UNVERIFIED"
                    )
                else:
                    status = (
                        "SPECIAL_SESSION_REVIEW_REQUIRED_"
                        "PROVENANCE_UNVERIFIED"
                    )

                # Export only non-quarantined records.
                # This file is for special-session review, NOT automatic
                # model training.
                for candle, local_time in eligible_entries:
                    row = candle_to_row(
                        candle,
                        local_time,
                        status,
                    )
                    row["special_session_name"] = special_name

                    special_rows.append(row)
                    exported_special_ids.add(int(candle.id))

                expected_count = len(expected)

                eligible_by_session_completeness = "NO"

            # ---------------------------------------------------------
            # Ordinary configured weekday sessions.
            # ---------------------------------------------------------
            else:
                # Existing project session boundary rule.
                if trading_date < date(2026, 11, 2):
                    start, end = "09:00", "23:25"
                else:
                    start, end = "09:00", "23:50"

                expected = expected_times(
                    trading_date,
                    start,
                    end,
                )

                schedule_matches = (
                    actual_set == set(expected)
                    and len(actual_times) == len(expected)
                )

                expected_count = len(expected)

                if not schedule_matches:
                    status = "PARTIAL_SESSION"
                    eligible_by_session_completeness = "NO"

                elif excluded_entries:
                    status = "FULL_SESSION_WITH_QUARANTINED_CANDLES"
                    eligible_by_session_completeness = "NO"

                else:
                    status = "FULL_SESSION"
                    eligible_by_session_completeness = "YES"

                    for candle, local_time in eligible_entries:
                        row = candle_to_row(
                            candle,
                            local_time,
                            status,
                        )
                        regular_rows.append(row)
                        exported_regular_ids.add(int(candle.id))

            classifications.append({
                "instrument_key": instrument_key,
                "trading_date_ist": date_text,
                "classification": status,
                "actual_candles": len(entries),
                "expected_candles": expected_count,
                "eligible_candles": len(eligible_entries),
                "quarantined_candles": len(excluded_entries),
                "first_candle_ist": actual_times[0].isoformat(),
                "last_candle_ist": actual_times[-1].isoformat(),
                "eligible_by_session_completeness":
                    eligible_by_session_completeness,
                "source_provenance": "NOT_INDEPENDENTLY_VERIFIED",
            })

        # -------------------------------------------------------------
        # Fail-closed export validation.
        # -------------------------------------------------------------
        selected_candle_ids = {
            int(candle.id)
            for candle in candles
        }

        expected_quarantine_ids = (
            set(exclusion_by_id.keys()) & selected_candle_ids
        )

        if exported_quarantine_ids != expected_quarantine_ids:
            missing = expected_quarantine_ids - exported_quarantine_ids
            raise RuntimeError(
                "Quarantine export mismatch. "
                f"Missing candle IDs: {sorted(missing)}"
            )

        overlap_regular = (
            exported_regular_ids & expected_quarantine_ids
        )
        overlap_special = (
            exported_special_ids & expected_quarantine_ids
        )

        if overlap_regular or overlap_special:
            raise RuntimeError(
                "QUARANTINE SAFETY FAILURE: excluded candles found "
                "in research exports. "
                f"Regular overlap={sorted(overlap_regular)}, "
                f"special overlap={sorted(overlap_special)}"
            )

        if exported_regular_ids & exported_special_ids:
            raise RuntimeError(
                "A candle appears in both regular and special exports."
            )

        # -------------------------------------------------------------
        # Output files.
        # -------------------------------------------------------------
        output_files = {
            "regular": OUTPUT_DIR / "regular_full_session_candles.csv",
            "special": OUTPUT_DIR / "special_session_candles.csv",
            "classification": OUTPUT_DIR / "session_classification.csv",
            "quarantine": OUTPUT_DIR / "quarantined_candles.csv",
        }

        candle_fields = [
            "id",
            "instrument_key",
            "trading_date_ist",
            "timestamp_ist",
            "timestamp_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            "source",
            "classification",
        ]

        special_fields = candle_fields + ["special_session_name"]
        quarantine_fields = candle_fields + ["exclusion_reason"]

        classification_fields = [
            "instrument_key",
            "trading_date_ist",
            "classification",
            "actual_candles",
            "expected_candles",
            "eligible_candles",
            "quarantined_candles",
            "first_candle_ist",
            "last_candle_ist",
            "eligible_by_session_completeness",
            "source_provenance",
        ]

        write_csv(
            output_files["regular"],
            regular_rows,
            candle_fields,
        )
        write_csv(
            output_files["special"],
            special_rows,
            special_fields,
        )
        write_csv(
            output_files["classification"],
            classifications,
            classification_fields,
        )
        write_csv(
            output_files["quarantine"],
            quarantine_rows,
            quarantine_fields,
        )

        print("\n========== CLEAN DATASET EXPORT ==========")
        print(f"Original database candles inspected: {len(candles)}")
        print(f"Regular full-session candles exported: {len(regular_rows)}")
        print(f"Special-session review candles exported: {len(special_rows)}")
        print(f"Quarantined candles exported: {len(quarantine_rows)}")

        print("\nQuarantine validation:")
        print(f"Expected quarantined IDs: {len(expected_quarantine_ids)}")
        print(f"Exported quarantined IDs: {len(exported_quarantine_ids)}")
        print("Quarantine/research overlap: 0")
        print("Quarantine validation: PASS")

        print("\nSession classifications:")
        for item in classifications:
            print(
                f"{item['trading_date_ist']} | "
                f"{item['classification']} | "
                f"{item['actual_candles']}/"
                f"{item['expected_candles']} candles | "
                f"quarantined={item['quarantined_candles']}"
            )

        print("\nOutput files:")
        for path in output_files.values():
            print(path)

        print("\nOriginal database was NOT modified.")
        print("No ML training was performed.")

    finally:
        session.close()


if __name__ == "__main__":
    main()
