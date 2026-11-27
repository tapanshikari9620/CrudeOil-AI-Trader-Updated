"""Start the read-only Upstox + local PAPER trading controller."""

import logging
from pathlib import Path

from config.logging_config import configure_logging
from utils.process_lock import AlreadyRunningError, single_instance_lock
from config.settings import settings
from database.db_connection import initialize_database
from database.repository import Repository
from execution.upstox_client import UpstoxClient
from paper_trading.paper_execution_engine import PaperExecutionEngine
from paper_trading.auto_controller import PaperTradingController


def main() -> None:
    configure_logging()
    logger = logging.getLogger("crude_oil_trader")
    lock_path = Path(__file__).resolve().parents[1] / "data" / "paper_trading.lock"

    try:
        with single_instance_lock(lock_path):
            _run_paper_trading(logger)
    except AlreadyRunningError as exc:
        logger.error("%s", exc)
        raise SystemExit(str(exc)) from exc


def _run_paper_trading(logger) -> None:
    settings.validate()
    if settings.trading_mode != "PAPER":
        raise SystemExit("Safety stop: TRADING_MODE must be PAPER. LIVE mode is disabled.")
    if not settings.upstox_access_token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN is required; token value will not be displayed.")

    initialize_database()
    client = UpstoxClient(
        settings.upstox_access_token,
        base_url=settings.upstox_api_base,
        timeout=settings.live_api_timeout_seconds,
        retry_count=settings.live_retry_count,
        retry_backoff_seconds=settings.live_retry_backoff_seconds,
    )
    controller = PaperTradingController(client, Repository(), PaperExecutionEngine(), settings)
    try:
        controller.prepare()
        controller.run_forever()
    except KeyboardInterrupt:
        logger.info("Ctrl+C received; PAPER trading stopped")
    finally:
        try:
            controller.collector.shutdown()
        except Exception:
            logger.exception("Collector shutdown reported an error")
        client.close()
        logger.info("Upstox read-only client closed; no live orders were sent")


if __name__ == "__main__":
    main()
