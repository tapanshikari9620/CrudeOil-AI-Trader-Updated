
from datetime import date

from data_collector.market_data_normalizer import (
    normalize_candle,
    validate_instrument_metadata,
)
from database.repository import Repository
from utils.date_utils import to_ist


class HistoricalCollector:
    """Collect history only when verified contract metadata accompanies the key."""

    def __init__(
        self,
        client,
        repository=None,
        instrument_metadata: dict | None = None,
    ):
        self.client = client
        self.repo = repository or Repository()
        self.instrument_metadata = instrument_metadata

    def collect(
        self,
        instrument_key: str,
        unit: str,
        interval: int,
        to_date: str,
        from_date: str | None = None,
        *,
        instrument_metadata: dict | None = None,
        exceptional_weekend_dates: set[date] | None = None,
    ) -> dict:
        unit = unit.lower()

        if unit not in {"minutes", "hours", "days", "weeks", "months"}:
            raise ValueError(
                f"Unsupported Upstox historical unit: {unit!r}"
            )

        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ValueError("Historical interval must be an integer")

        limits = {
            "minutes": (1, 300),
            "hours": (1, 5),
            "days": (1, 1),
            "weeks": (1, 1),
            "months": (1, 1),
        }

        if not limits[unit][0] <= interval <= limits[unit][1]:
            raise ValueError(
                f"Unsupported Upstox interval {interval} {unit}"
            )

        try:
            end_date = date.fromisoformat(to_date)
            start_date = (
                date.fromisoformat(from_date)
                if from_date
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Historical dates must use YYYY-MM-DD format"
            ) from exc

        if start_date and start_date > end_date:
            raise ValueError(
                "Historical from_date must not be after to_date"
            )

        if (
            unit == "minutes"
            and interval <= 15
            and start_date
            and (end_date - start_date).days > 31
        ):
            raise ValueError(
                "Upstox V3 permits at most one month for minute intervals up to 15"
            )

        if (
            unit == "minutes"
            and interval > 15
            and start_date
            and (end_date - start_date).days > 92
        ):
            raise ValueError(
                "Upstox V3 permits at most one quarter for minute intervals above 15"
            )

        if (
            unit == "hours"
            and start_date
            and (end_date - start_date).days > 92
        ):
            raise ValueError(
                "Upstox V3 permits at most one quarter for hourly intervals"
            )

        if (
            unit == "days"
            and start_date
            and (end_date - start_date).days > 3653
        ):
            raise ValueError(
                "Upstox V3 permits at most one decade for daily intervals"
            )

        metadata = (
            instrument_metadata
            if instrument_metadata is not None
            else self.instrument_metadata
        )

        if metadata is None:
            raise ValueError(
                "Verified instrument metadata is required; "
                "resolve instrument_key from the Upstox instrument master"
            )

        instrument_type = validate_instrument_metadata(
            instrument_key,
            metadata,
        )

        if instrument_type != "FUT":
            raise ValueError(
                "Historical futures collector only accepts verified FUT instruments"
            )

        # Only explicitly authorised weekend dates can bypass
        # the default weekend restriction.
        authorised_weekend_dates = exceptional_weekend_dates or set()

        payload = self.client.historical_candles(
            instrument_key,
            unit,
            interval,
            to_date,
            from_date,
        )

        try:
            candles = payload["data"]["candles"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "Unexpected Upstox historical-candle response schema"
            ) from exc

        if not isinstance(candles, list):
            raise ValueError(
                "Unexpected Upstox historical-candle response: "
                "candles must be a list"
            )

        inserted = 0
        duplicates = 0
        invalid = 0
        duplicate_response_rows = 0

        unit_labels = {
            "minutes": "min",
            "hours": "hour",
            "days": "day",
            "weeks": "week",
            "months": "month",
        }

        label = f"{interval}_{unit_labels[unit]}"

        seen_timestamps = set()
        first_timestamp = None
        last_timestamp = None

        for row in candles:
            try:
                item = normalize_candle(
                    instrument_key,
                    label,
                    row,
                    metadata,
                )

                # Normalized timestamps are stored in UTC.
                # Convert to IST only when checking market dates.
                market_date = to_ist(item["timestamp"]).date()

                # Reject weekend intraday candles by default.
                # Daily, weekly and monthly candles are unaffected.
                if (
                    unit in {"minutes", "hours"}
                    and market_date.weekday() >= 5
                    and market_date not in authorised_weekend_dates
                ):
                    raise ValueError(
                        "Weekend intraday candle is not an authorised trading session"
                    )

                if start_date and market_date < start_date:
                    raise ValueError(
                        "Candle timestamp falls before requested from_date"
                    )

                if market_date > end_date:
                    raise ValueError(
                        "Candle timestamp falls after requested to_date"
                    )

            except (TypeError, ValueError, OverflowError):
                invalid += 1
                continue

            timestamp = item["timestamp"]

            if timestamp in seen_timestamps:
                duplicate_response_rows += 1
                continue

            seen_timestamps.add(timestamp)

            first_timestamp = (
                min(first_timestamp, timestamp)
                if first_timestamp
                else timestamp
            )

            last_timestamp = (
                max(last_timestamp, timestamp)
                if last_timestamp
                else timestamp
            )

            if self.repo.add_candle(item):
                inserted += 1
            else:
                duplicates += 1

        return {
            "received": len(candles),
            "inserted": inserted,
            "duplicates": duplicates,
            "duplicate_response_rows": duplicate_response_rows,
            "invalid": invalid,
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "requested_from": start_date,
            "requested_to": end_date,
            "interval": label,
        }
