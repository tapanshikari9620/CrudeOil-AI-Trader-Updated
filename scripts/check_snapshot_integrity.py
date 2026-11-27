
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "crude_oil_trading.db"
conn = sqlite3.connect(db)
cur = conn.cursor()

queries = {
    "Option contracts by expiry and type": """
        SELECT expiry, option_type,
               COUNT(DISTINCT instrument_key) AS contracts,
               COUNT(*) AS snapshots
        FROM option_snapshots
        GROUP BY expiry, option_type
        ORDER BY expiry, option_type
    """,
    "Daily unique option contracts": """
        SELECT substr(timestamp, 1, 10) AS day,
               COUNT(DISTINCT instrument_key) AS contracts,
               COUNT(*) AS snapshots
        FROM option_snapshots
        GROUP BY day ORDER BY day
    """,
    "Duplicate option snapshots": """
        SELECT COUNT(*) FROM (
            SELECT instrument_key, timestamp, COUNT(*) AS n
            FROM option_snapshots
            GROUP BY instrument_key, timestamp
            HAVING n > 1
        )
    """,
    "Option price and spread checks": """
        SELECT option_type,
               COUNT(*) AS total,
               SUM(CASE WHEN ltp <= 0 THEN 1 ELSE 0 END) AS bad_ltp,
               SUM(CASE WHEN bid_price > ask_price
                         AND bid_price > 0 AND ask_price > 0
                        THEN 1 ELSE 0 END) AS crossed_market,
               SUM(CASE WHEN bid_price IS NULL OR ask_price IS NULL
                        THEN 1 ELSE 0 END) AS missing_depth
        FROM option_snapshots
        GROUP BY option_type
    """,
}

for title, sql in queries.items():
    print(f"\n{'=' * 65}\n{title}")
    for row in cur.execute(sql):
        print(row)

conn.close()
