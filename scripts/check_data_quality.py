
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crude_oil_trading.db"

conn = sqlite3.connect(DB)
cur = conn.cursor()

def columns(table):
    return [row[1] for row in cur.execute(
        f'PRAGMA table_info("{table}")'
    )]

def show_table(table):
    cols = columns(table)
    print(f"\n{'=' * 65}\nTABLE: {table}")
    print("Columns:", ", ".join(cols))

    for field in ("option_type", "instrument_type", "classification",
                  "quality_status", "reason"):
        if field in cols:
            print(f"\nBreakdown by {field}:")
            rows = cur.execute(
                f'SELECT "{field}", COUNT(*) FROM "{table}" '
                f'GROUP BY "{field}" ORDER BY COUNT(*) DESC LIMIT 20'
            ).fetchall()
            for row in rows:
                print(row)

    time_col = next(
        (c for c in ("timestamp", "observed_at", "created_at")
         if c in cols), None
    )

    if time_col:
        print(f"\nDaily records by {time_col}:")
        rows = cur.execute(
            f'''SELECT substr("{time_col}", 1, 10), COUNT(*)
                FROM "{table}"
                WHERE "{time_col}" IS NOT NULL
                GROUP BY substr("{time_col}", 1, 10)
                ORDER BY 1'''
        ).fetchall()
        for row in rows:
            print(row)

    key_col = next(
        (c for c in ("instrument_key", "instrument_token")
         if c in cols), None
    )
    if key_col:
        print("\nUnique instruments:",
              cur.execute(
                  f'SELECT COUNT(DISTINCT "{key_col}") FROM "{table}"'
              ).fetchone()[0])

for table in (
    "market_quote_snapshots",
    "option_snapshots",
    "collection_quality_events",
    "market_candles",
):
    show_table(table)

conn.close()
