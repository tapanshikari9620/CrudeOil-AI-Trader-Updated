from __future__ import annotations

import pandas as pd
from sqlalchemy import inspect

from database.db_connection import engine


def load_trade_history():
    with engine.connect() as connection:
        if "paper_trades" not in inspect(connection).get_table_names():
            return pd.DataFrame(columns=[
                "id", "instrument_key", "side", "quantity", "entry_price", "exit_price", "stop_loss", "target",
                "gross_pnl", "charges", "net_pnl", "status", "notes", "opened_at", "closed_at", "created_at"
            ])
    return pd.read_sql("SELECT * FROM paper_trades ORDER BY opened_at DESC", engine)
