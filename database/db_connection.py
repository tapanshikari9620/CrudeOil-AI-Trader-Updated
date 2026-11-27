from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config.settings import PROJECT_ROOT, settings


class Base(DeclarativeBase):
    pass


def _resolve_database_url(database_url: str):
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, ":memory:"):
        db_path = Path(url.database)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        url = url.set(database=str(db_path))
    return url


def _build_engine():
    database_url = _resolve_database_url(settings.database_url)
    if database_url.get_backend_name() == "sqlite":
        if database_url.database not in (None, ":memory:"):
            Path(database_url.database).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    return create_engine(database_url, pool_pre_ping=True)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)



def _assert_single_open_position(target_engine) -> None:
    """Refuse the global one-open-position constraint if conflicting rows exist."""
    if target_engine.dialect.name != "sqlite":
        return

    if not inspect(target_engine).has_table("paper_trades"):
        return

    with target_engine.connect() as connection:
        count = connection.execute(text(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'"
        )).scalar_one()

    if count > 1:
        raise RuntimeError(
            "Cannot enforce one global OPEN paper position: "
            f"found {count} existing OPEN trades. "
            "No trades were changed; reconcile them before restarting."
        )


def _assert_no_stuck_closing_positions(target_engine) -> None:
    """Stop startup if any PAPER trade remains in an interrupted CLOSING state."""
    if not inspect(target_engine).has_table("paper_trades"):
        return

    with target_engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT COUNT(*) FROM paper_trades "
                "WHERE status = :status"
            ),
            {"status": "CLOSING"},
        ).scalar_one()

    if count:
        raise RuntimeError(
            f"Found {count} PAPER trade(s) stuck in CLOSING status. "
            "Startup stopped; no trade records were changed. "
            "Reconcile these trades manually before restarting."
        )


def initialize_database() -> None:
    """Create all SQLAlchemy model tables used by the project."""
    from database import models  # noqa: F401
    _assert_single_open_position(engine)
    Base.metadata.create_all(bind=engine)
    _migrate_paper_trade_columns(engine)
    _migrate_phase2_columns(engine)
    _assert_no_stuck_closing_positions(engine)


def _migrate_paper_trade_columns(target_engine) -> None:
    """Add paper-monitor state columns without rebuilding or discarding old rows."""
    columns = {column["name"] for column in inspect(target_engine).get_columns("paper_trades")}
    additions = {
        "option_type": "VARCHAR(2)",
        "last_mark_price": "FLOAT",
        "initial_stop_loss": "FLOAT",
        "peak_mark_price": "FLOAT",
        "unrealized_pnl": "FLOAT",
        "last_quote_at": "DATETIME",
        "exit_reason": "VARCHAR(30)",
        "created_at": "DATETIME",
    }
    _assert_single_open_position(target_engine)

    with target_engine.begin() as connection:
        duplicate = connection.execute(text(
            "SELECT instrument_key FROM paper_trades WHERE status = 'OPEN' "
            "GROUP BY instrument_key HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate is not None:
            raise RuntimeError(
                "Cannot enforce one open paper position per instrument: existing duplicate open positions "
                f"were preserved for {duplicate[0]!r}; reconcile them before restarting"
            )
        for name, column_type in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE paper_trades ADD COLUMN {name} {column_type}"))
        connection.execute(text("UPDATE paper_trades SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        if target_engine.dialect.name == "sqlite":
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_paper_trade_instrument "
                "ON paper_trades (instrument_key) WHERE status = 'OPEN'"
            ))
            connection.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_single_open_paper_trade "
                "ON paper_trades (1) WHERE status = 'OPEN'"
            ))


def _migrate_phase2_columns(target_engine) -> None:
    """Add Phase 2 metadata and full-quote columns without replacing stored rows."""
    additions_by_table = {
        "verified_instruments": {
            "underlying_symbol": "VARCHAR(80)",
            "contract_status": "VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN'",
        },
        "market_quote_snapshots": {
            "open": "FLOAT",
            "high": "FLOAT",
            "low": "FLOAT",
            "close": "FLOAT",
            "previous_close": "FLOAT",
            "last_trade_time": "DATETIME",
            "trading_symbol": "VARCHAR(120)",
            "expiry": "VARCHAR(20)",
            "option_type": "VARCHAR(2)",
            "underlying_instrument_key": "VARCHAR(150)",
            "response_timestamp": "DATETIME",
            "quality_status": "VARCHAR(20) NOT NULL DEFAULT 'VALID_FRESH'",
            "bid_quantity": "FLOAT",
            "ask_quantity": "FLOAT",
        },
        "option_snapshots": {
            "underlying_instrument_key": "VARCHAR(150)",
            "response_timestamp": "DATETIME",
            "last_trade_time": "DATETIME",
            "quality_status": "VARCHAR(20) NOT NULL DEFAULT 'VALID_FRESH'",
            "bid_quantity": "FLOAT",
            "ask_quantity": "FLOAT",
        },
    }
    inspector = inspect(target_engine)
    with target_engine.begin() as connection:
        for table_name, additions in additions_by_table.items():
            if not inspector.has_table(table_name):
                continue
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            for name, column_type in additions.items():
                if name not in columns:
                    connection.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}"
                    ))
        if target_engine.dialect.name == "sqlite":
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_market_quote_instrument_timestamp "
                "ON market_quote_snapshots (instrument_key, timestamp)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_market_quote_expiry ON market_quote_snapshots (expiry)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_option_instrument_timestamp "
                "ON option_snapshots (instrument_key, timestamp)"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_option_expiry_type ON option_snapshots (expiry, option_type)"
            ))


def init_db() -> None:
    """Compatibility wrapper for older imports that expect init_db()."""
    initialize_database()


def get_session():
    return SessionLocal()
