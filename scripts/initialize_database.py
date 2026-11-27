from config.logging_config import configure_logging
from database.db_connection import initialize_database, engine

logger = configure_logging()

if __name__ == "__main__":
    initialize_database()
    logger.info("Database initialized successfully at %s", engine.url)
    print("DATABASE INITIALIZATION: PASS")
