
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, DecimalException
from datetime import datetime
import math


@dataclass
class PaperFill:
    instrument_key: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    simulated: bool = True


class PaperBroker:
    def __init__(self, slippage_ticks: int = 1):
        if (
            isinstance(slippage_ticks, bool)
            or not isinstance(slippage_ticks, int)
            or slippage_ticks < 0
        ):
            raise ValueError("slippage_ticks must be a non-negative integer")

        self.slippage_ticks = slippage_ticks

    @staticmethod
    def _validate_positive_number(value, name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")

    def market_fill(
        self,
        instrument_key: str,
        side: str,
        quantity: int,
        bid: float,
        ask: float,
        tick_size: float,
        timestamp: datetime,
    ) -> PaperFill:

        if not isinstance(instrument_key, str) or not instrument_key.strip():
            raise ValueError("instrument_key is required")

        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValueError("quantity must be a positive integer")

        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        self._validate_positive_number(bid, "bid")
        self._validate_positive_number(ask, "ask")
        self._validate_positive_number(tick_size, "tick_size")

        if ask < bid:
            raise ValueError("ask must be greater than or equal to bid")

        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError("timestamp must be timezone-aware")

        base = ask if side == "BUY" else bid
        slip = self.slippage_ticks * tick_size
        price = (
            base + slip
            if side == "BUY"
            else max(tick_size, base - slip)
        )

        if not math.isfinite(price) or price <= 0:
            raise ValueError("Calculated paper fill price is invalid")

        try:
            units = Decimal(str(price)) / Decimal(str(tick_size))
            rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
            rounded = float(
                units.to_integral_value(rounding=rounding)
                * Decimal(str(tick_size))
            )
        except (DecimalException, ValueError, OverflowError) as exc:
            raise ValueError("Unable to calculate tick-aligned paper fill") from exc

        if not math.isfinite(rounded) or rounded <= 0:
            raise ValueError("Calculated paper fill price is invalid")

        return PaperFill(
            instrument_key=instrument_key,
            side=side,
            quantity=quantity,
            price=rounded,
            timestamp=timestamp,
        )
