"""Public collector names retained at the original live-market module path."""

from datetime import datetime
from typing import Any

from data_collector.continuous_live_collector import (
    ContinuousMarketDataCollector,
    normalize_live_quote,
)


class LiveMarketCollector(ContinuousMarketDataCollector):
    """Integrated REST-based MCX futures and options collector."""


class FuturesLiveCollector:
    """Futures polling surface backed by the integrated collection service."""

    def __init__(self, collector: ContinuousMarketDataCollector):
        self.collector = collector

    def collect_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        return self.collector.run_cycle(now)


class OptionsSnapshotCollector:
    """CE/PE polling surface backed by the integrated collection service."""

    def __init__(self, collector: ContinuousMarketDataCollector):
        self.collector = collector

    def collect_cycle(self, now: datetime | None = None) -> dict[str, int]:
        return self.collector.collect_options_cycle(now)


__all__ = [
    "FuturesLiveCollector",
    "LiveMarketCollector",
    "OptionsSnapshotCollector",
    "normalize_live_quote",
]
