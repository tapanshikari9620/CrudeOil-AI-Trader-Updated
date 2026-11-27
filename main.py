from config.settings import settings
from config.logging_config import configure_logging
from database.db_connection import initialize_database


def main() -> None:
    logger = configure_logging()
    settings.validate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    initialize_database()
    logger.info("%s started in %s mode", settings.app_name, settings.trading_mode)
    print(f"{settings.app_name}: FOUNDATION READY")
    print(f"Trading mode: {settings.trading_mode}")
    print("No broker connection or live order execution is enabled in Phase 1.")


if __name__ == "__main__":
    main()
