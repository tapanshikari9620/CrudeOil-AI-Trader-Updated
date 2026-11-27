
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.db_connection import Base
from database.models import VerifiedInstrument
from dashboard.dashboard_data import latest_active_future
from config.settings import settings


@pytest.fixture
def dashboard_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    TestSession = sessionmaker(bind=engine)
    session = TestSession()

    try:
        future_expiry = (
            datetime.now().date() + timedelta(days=30)
        ).isoformat()

        session.add_all([
            VerifiedInstrument(
                instrument_key="MCX|REGULAR_TEST",
                trading_symbol="CRUDEOIL_TEST",
                exchange="MCX",
                segment="MCX_FO",
                instrument_type="FUT",
                expiry=future_expiry,
                strike_price=0.0,
                lot_size=100,
                tick_size=1.0,
                underlying_key=None,
                underlying_symbol="CRUDEOIL",
                contract_status="ACTIVE",
            ),
            VerifiedInstrument(
                instrument_key="MCX|MINI_TEST",
                trading_symbol="CRUDEOILM_TEST",
                exchange="MCX",
                segment="MCX_FO",
                instrument_type="FUT",
                expiry=future_expiry,
                strike_price=0.0,
                lot_size=10,
                tick_size=1.0,
                underlying_key=None,
                underlying_symbol="CRUDEOILM",
                contract_status="ACTIVE",
            ),
        ])

        session.commit()
        yield session

    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("product", "expected_key"),
    [
        ("CRUDEOIL", "MCX|REGULAR_TEST"),
        ("CRUDEOILM", "MCX|MINI_TEST"),
    ],
)
def test_dashboard_selects_only_configured_product(
    dashboard_session,
    monkeypatch,
    product,
    expected_key,
):
    monkeypatch.setattr(settings, "mcx_product", product)

    selected = latest_active_future(dashboard_session)

    assert selected is not None
    assert selected.instrument_key == expected_key
    assert selected.underlying_symbol == product
