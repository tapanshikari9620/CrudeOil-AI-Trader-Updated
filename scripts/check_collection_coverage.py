import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "crude_oil_trading.db"
conn = sqlite3.connect(db)
cur = conn.cursor()

queries = {
    "Snapshot frequency by day": """
        SELECT substr(timestamp, 1, 10) AS day,
               COUNT(*) AS snapshots,
               COUNT(DISTINCT instrument_key) AS contracts,
               MIN(timestamp), MAX(timestamp)
        FROM option_snapshots
        GROUP BY day ORDER BY day
    """,

    "Expiry distribution by day": """
        SELECT substr(timestamp, 1, 10) AS day,
               expiry,
               COUNT(DISTINCT instrument_key) AS contracts,
               COUNT(*) AS snapshots
        FROM option_snapshots
        GROUP BY day, expiry
        ORDER BY day, expiry
    """,

    "Snapshot interval sample": """
        SELECT instrument_key, timestamp, last_trade_time, ltp
        FROM option_snapshots
        ORDER BY timestamp DESC
        LIMIT 20
    """,

    "Latest option snapshots": """
        SELECT option_type,
               COUNT(*) AS snapshots,
               COUNT(DISTINCT instrument_key) AS contracts,
               MIN(ltp), MAX(ltp)
        FROM option_snapshots
        WHERE timestamp = (
            SELECT MAX(timestamp) FROM option_snapshots
        )
        GROUP BY option_type
    """
}

for title, sql in queries.items():
    print(f"\n{'=' * 65}\n{title}")
    for row in cur.execute(sql):
        print(row)

conn.close()