from __future__ import annotations

from sqlalchemy import inspect, text

from database.db_connection import engine


def daily_summary():
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if "paper_trades" not in tables:
            return {"trades": 0, "net_pnl": 0.0}
        rows = connection.execute(
            text("SELECT COUNT(*), COALESCE(SUM(net_pnl), 0) FROM paper_trades WHERE date(opened_at) = date('now')")
        ).one()
    return {"trades": rows[0], "net_pnl": float(rows[1])}
