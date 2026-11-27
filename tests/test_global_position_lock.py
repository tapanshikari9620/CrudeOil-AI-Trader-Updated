import pytest

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.db_connection import Base
from database.models import PaperTrade


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_second_open_position_on_different_instrument_is_rejected(session):
    first = PaperTrade(
        instrument_key="MCX_FO|CRUDEOIL_CE_TEST",
        side="BUY",
        quantity=1,
        status="OPEN",
    )
    session.add(first)
    session.commit()

    second = PaperTrade(
        instrument_key="MCX_FO|CRUDEOIL_PE_TEST",
        side="BUY",
        quantity=1,
        status="OPEN",
    )
    session.add(second)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()

    open_count = session.scalar(
        select(func.count()).select_from(PaperTrade).where(
            PaperTrade.status == "OPEN"
        )
    )

    assert open_count == 1

    preserved = session.scalar(
        select(PaperTrade).where(PaperTrade.status == "OPEN")
    )
    assert preserved.instrument_key == "MCX_FO|CRUDEOIL_CE_TEST"


def test_new_open_position_is_allowed_after_previous_trade_closes(session):
    first = PaperTrade(
        instrument_key="MCX_FO|CRUDEOIL_CE_TEST",
        side="BUY",
        quantity=1,
        status="OPEN",
    )
    session.add(first)
    session.commit()

    first.status = "CLOSED"
    session.commit()

    second = PaperTrade(
        instrument_key="MCX_FO|CRUDEOIL_PE_TEST",
        side="BUY",
        quantity=1,
        status="OPEN",
    )
    session.add(second)
    session.commit()

    open_trades = session.scalars(
        select(PaperTrade).where(PaperTrade.status == "OPEN")
    ).all()

    assert len(open_trades) == 1
    assert open_trades[0].instrument_key == "MCX_FO|CRUDEOIL_PE_TEST"
