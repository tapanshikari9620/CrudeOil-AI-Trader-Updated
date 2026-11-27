
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "crude_oil_trading.db"

if not DB_PATH.exists():
    raise FileNotFoundError(f"Database not found: {DB_PATH}")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

print("\n========== CRUDE OIL DATA COLLECTION REPORT ==========")
print(f"Database: {DB_PATH}\n")

cursor.execute("""
    SELECT name FROM sqlite_master
    WHERE type='table' AND name NOT LIKE 'sqlite_%'
    ORDER BY name
""")

tables = [row[0] for row in cursor.fetchall()]
total_records = 0

for table in tables:
    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
    count = cursor.fetchone()[0]
    total_records += count

    print(f"\nTable: {table}")
    print(f"Records: {count}")

    cursor.execute(f'PRAGMA table_info("{table}")')
    columns = [row[1] for row in cursor.fetchall()]

    date_columns = [
        col for col in columns
        if any(word in col.lower() for word in
               ("timestamp", "created_at", "observed_at",
                "last_trade_time", "candle_time", "datetime"))
    ]

    for col in date_columns:
        try:
            cursor.execute(
                f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}"'
            )
            minimum, maximum = cursor.fetchone()
            if minimum is not None:
                print(f"  {col}: {minimum} -> {maximum}")
        except sqlite3.Error:
            pass

print("\n======================================================")
print(f"TOTAL RECORDS ACROSS TABLES: {total_records}")
print("======================================================")

conn.close()
