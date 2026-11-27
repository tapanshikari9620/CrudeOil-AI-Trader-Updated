
from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, Iterable
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("Asia/Kolkata")
SUPPORTED_PRODUCTS = {"CRUDEOIL", "CRUDEOILM"}


def _get(instrument: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dictionary or an instrument object."""
    if isinstance(instrument, dict):
        return instrument.get(key, default)

    return getattr(instrument, key, default)


def _active(instrument: Any, today: date) -> bool:
    """Return whether a verified contract is active and unexpired."""
    status = str(_get(instrument, "contract_status", "")).upper()

    if status != "ACTIVE":
        return False

    try:
        expiry = date.fromisoformat(str(_get(instrument, "expiry"))[:10])
    except (TypeError, ValueError):
        return False

    return expiry >= today


def _validate_product(product: str) -> str:
    if not isinstance(product, str):
        raise ValueError("MCX product must be CRUDEOIL or CRUDEOILM")

    normalized = product.upper().strip()

    if normalized not in SUPPORTED_PRODUCTS:
        raise ValueError("MCX product must be CRUDEOIL or CRUDEOILM")

    return normalized


class CrudeContractSelector:
    """Select active futures and matching options for one MCX crude product."""

    def __init__(self, repository, *, today: date | None = None):
        self.repository = repository
        self.today = today or datetime.now(MARKET_TZ).date()

    def select(
        self,
        *,
        target_price: float | None = None,
        product: str = "CRUDEOIL",
    ) -> dict[str, Any]:
        """Select the active futures contract and matching CE/PE options."""
        instruments = self.repository.get_verified_instruments()

        return self.select_from(
            instruments,
            today=self.today,
            target_price=target_price,
            product=product,
        )

    @classmethod
    def select_from(
        cls,
        instruments: Iterable[Any],
        *,
        today: date | None = None,
        target_price: float | None = None,
        product: str = "CRUDEOIL",
    ) -> dict[str, Any]:
        """Select futures and paired options for the explicitly requested product."""

        current_day = today or datetime.now(MARKET_TZ).date()
        selected_product = _validate_product(product)

        if target_price is not None:
            if (
                isinstance(target_price, bool)
                or not math.isfinite(float(target_price))
                or float(target_price) <= 0
            ):
                raise ValueError("target_price must be finite and positive")

        # Exact product matching prevents Regular/Mini contract mixing.
        active = [
            item
            for item in instruments
            if _get(item, "exchange") == "MCX"
            and _get(item, "segment") == "MCX_FO"
            and str(_get(item, "underlying_symbol", "")).upper().strip()
            == selected_product
            and _active(item, current_day)
        ]

        futures = [
            item
            for item in active
            if _get(item, "instrument_type") == "FUT"
        ]

        futures.sort(
            key=lambda item: (
                date.fromisoformat(str(_get(item, "expiry"))[:10]),
                str(_get(item, "trading_symbol", "")),
            )
        )

        if not futures:
            raise ValueError(
                f"No unexpired verified MCX {selected_product} futures contract is available"
            )

        future = futures[0]

        # Only options belonging to the selected product are considered.
        options = [
            item
            for item in active
            if _get(item, "instrument_type") in {"CE", "PE"}
            and str(_get(item, "underlying_symbol", "")).upper().strip()
            == selected_product
        ]

        expiries = sorted(
            {
                str(_get(item, "expiry"))[:10]
                for item in options
                if _active(item, current_day)
            }
        )

        selected_call = None
        selected_put = None

        for expiry in expiries:
            expiry_options = [
                item
                for item in options
                if str(_get(item, "expiry"))[:10] == expiry
            ]

            calls = [
                item
                for item in expiry_options
                if _get(item, "instrument_type") == "CE"
            ]

            puts = [
                item
                for item in expiry_options
                if _get(item, "instrument_type") == "PE"
            ]

            common_strikes = {
                float(_get(item, "strike_price"))
                for item in calls
            } & {
                float(_get(item, "strike_price"))
                for item in puts
            }

            if not common_strikes:
                continue

            sorted_strikes = sorted(common_strikes)

            target_strike = (
                float(target_price)
                if target_price is not None
                else sorted_strikes[len(sorted_strikes) // 2]
            )

            strike = min(
                sorted_strikes,
                key=lambda value: (abs(value - target_strike), value),
            )

            selected_call = next(
                item
                for item in calls
                if float(_get(item, "strike_price")) == strike
            )

            selected_put = next(
                item
                for item in puts
                if float(_get(item, "strike_price")) == strike
            )

            break

        return {
            "future": future,
            "call": selected_call,
            "put": selected_put,
        }
