from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from database.db_connection import Base, _resolve_database_url
from config.settings import PROJECT_ROOT
from database import models  # noqa: F401


def test_tables_can_be_created():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    table_names = set(inspect(engine).get_table_names())
    assert {"market_candles", "option_snapshots", "system_events"} <= table_names


def test_relative_sqlite_database_url_resolves_from_project_root():
    url = _resolve_database_url("sqlite:///data/crude_oil_trading.db")
    assert url.database == str(PROJECT_ROOT / "data" / "crude_oil_trading.db")


def test_in_memory_sqlite_url_is_not_rewritten():
    url = _resolve_database_url("sqlite:///:memory:")
    assert url.database == ":memory:"
