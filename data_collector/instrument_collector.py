
import httpx
import json
import math
import gzip

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from data_collector.tick_size_normalizer import normalize_upstox_tick_size


class InstrumentCollector:
    """Downloads Upstox's official MCX BOD instrument master in JSON format."""

    MCX_URL = (
        "https://assets.upstox.com/market-quote/"
        "instruments/exchange/MCX.json.gz"
    )

    def fetch(self):
        response = httpx.get(self.MCX_URL, timeout=30)
        response.raise_for_status()

        raw = gzip.decompress(response.content).decode("utf-8")
        data = json.loads(raw)

        if not isinstance(data, list):
            raise ValueError(
                "Unexpected MCX instrument master: expected a JSON array"
            )

        return data

    def find_contract(self, instrument_key: str) -> dict:
        """Return the unique official master row for an exact instrument key."""

        matches = [
            item
            for item in self.fetch()
            if item.get("instrument_key") == instrument_key
            and (item.get("segment") or item.get("exchange")) == "MCX_FO"
        ]

        if not matches:
            raise ValueError(
                f"Instrument key {instrument_key!r} "
                "was not found in the MCX instrument master"
            )

        if len(matches) != 1:
            raise ValueError(
                f"Instrument key {instrument_key!r} "
                "is not unique in the MCX instrument master"
            )

        return self.normalize_contract(matches[0])

    @staticmethod
    def crude_contracts(items, product: str | None = None):
        """
        Return MCX crude contracts.

        If product is supplied, return only that exact underlying.
        If product is None, preserve the legacy behavior and return
        both Regular and Mini crude contracts.
        """
        if product is not None:
            product = str(product).upper().strip()

            if product not in {"CRUDEOIL", "CRUDEOILM"}:
                raise ValueError(
                    f"Unsupported MCX crude product: {product}"
                )

        results = []

        for item in items:
            segment = item.get("segment") or item.get("exchange")

            if segment != "MCX_FO":
                continue

            underlying = str(
                item.get("underlying_symbol") or ""
            ).upper().strip()

            if product is not None:
                if underlying != product:
                    continue
            else:
                symbol = " ".join(
                    str(item.get(key, ""))
                    for key in (
                        "name",
                        "trading_symbol",
                        "underlying_symbol",
                    )
                ).upper()

                if "CRUDEOIL" not in symbol and "CRUDE" not in symbol:
                    continue

            results.append(item)

        return results

    @staticmethod
    def normalize_contract(item):
        required = (
            "instrument_key",
            "trading_symbol",
            "instrument_type",
            "lot_size",
            "tick_size",
            "underlying_symbol",
            "expiry",
        )

        missing = [
            key
            for key in required
            if item.get(key) is None or item.get(key) == ""
        ]

        if missing:
            raise ValueError(
                f"Instrument metadata missing fields: {missing}"
            )

        # Validate numeric metadata without silently truncating values.
        try:
            lot_value = float(item["lot_size"])
            raw_tick_size = float(item["tick_size"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Instrument lot_size or tick_size is invalid"
            ) from exc

        if (
            not math.isfinite(lot_value)
            or not lot_value.is_integer()
            or lot_value <= 0
        ):
            raise ValueError(
                "Instrument lot_size must be a positive integer"
            )

        if (
            not math.isfinite(raw_tick_size)
            or raw_tick_size <= 0
        ):
            raise ValueError(
                "Instrument tick_size must be finite and positive"
            )

        lot_size = int(lot_value)

        # Normalize expiry.
        expiry_value = item["expiry"]

        if isinstance(expiry_value, (int, float)):
            expiry = datetime.fromtimestamp(
                float(expiry_value) / 1000,
                tz=timezone.utc,
            ).date().isoformat()
        else:
            expiry = str(expiry_value)

        # Normalize only the specifically verified contract representation.
        normalized_tick_size = normalize_upstox_tick_size(
            raw_tick_size,
            instrument_type=item["instrument_type"],
            trading_symbol=item["trading_symbol"],
            underlying_symbol=item["underlying_symbol"],
            lot_size=lot_size,
        )

        return {
            "instrument_key": item["instrument_key"],
            "trading_symbol": item["trading_symbol"],
            "exchange": item.get("exchange", "MCX"),
            "segment": item.get("segment", "MCX_FO"),
            "instrument_type": item["instrument_type"],
            "expiry": expiry,
            "strike_price": item.get("strike_price"),
            "underlying_symbol": item["underlying_symbol"],
            "option_type": (
                item["instrument_type"]
                if item["instrument_type"] in {"CE", "PE"}
                else None
            ),
            "lot_size": lot_size,
            "tick_size": normalized_tick_size,
            "underlying_key": item.get("underlying_key"),
            "contract_status": (
                "ACTIVE"
                if expiry
                >= datetime.now(
                    ZoneInfo("Asia/Kolkata")
                ).date().isoformat()
                else "EXPIRED"
            ),
            # Preserve the original source values for audit purposes.
            "raw_metadata": dict(item),
            "metadata_source": "official_instrument_master",
        }
