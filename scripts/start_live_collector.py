"""Start continuous, read-only MCX crude futures and options collection."""

import logging

from config.logging_config import configure_logging
from config.settings import settings
from data_collector.live_market_collector import LiveMarketCollector
from database.db_connection import initialize_database
from database.repository import Repository
from execution.upstox_client import UpstoxClient


def main() -> None:
    settings.validate()
    if settings.trading_mode != "PAPER":
        raise SystemExit("Continuous market-data collection requires TRADING_MODE=PAPER.")
    if not settings.upstox_access_token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN is required; the value will not be displayed.")

    configure_logging()
    logger = logging.getLogger("crude_oil_trader")
    initialize_database()
    client = UpstoxClient(
        settings.upstox_access_token,
        base_url=settings.upstox_api_base,
        timeout=settings.live_api_timeout_seconds,
        retry_count=settings.live_retry_count,
        retry_backoff_seconds=settings.live_retry_backoff_seconds,
    )
    collector = LiveMarketCollector(client, Repository(), config=settings)
    try:
        collector.prepare()
        logger.info(
            "Starting PAPER read-only MCX collectors (futures=%ss, options=%ss, max options=%d)",
            settings.live_futures_poll_seconds,
            settings.live_options_poll_seconds,
            settings.live_max_contracts_per_cycle,
        )
        collector.run_forever()
    except KeyboardInterrupt:
        logger.info("Ctrl+C received; shutting down collectors cleanly")
        collector.shutdown()
    finally:
        client.close()
        logger.info("Upstox read-only client closed")


if __name__ == "__main__":
    main()
