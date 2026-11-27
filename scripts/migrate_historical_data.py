import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SOURCE = Path(r"C:\Users\tapan\PycharmProjects\CrudeOil-AI-Trader\data\crude_oil_trading.db")
TARGET = BASE / "data" / "crude_oil_trading.db"

TABLES = [
    "verified_instruments",
    "raw_instrument_metadata",
    "market_candles",
    "paper_trades",
]


def q(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def columns(conn, table):
    return [row[1] for row in conn.execute(
        f"PRAGMA table_info({q(table)})"
    ).fetchall()]


def count(conn, table):
    return conn.execute(
        f"SELECT COUNT(*) FROM {q(table)}"
    ).fetchone()[0]


def backup_database():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = TARGET.with_name(
        f"{TARGET.stem}.pre_migration_{stamp}.db"
    )

    source_conn = sqlite3.connect(str(TARGET))
    backup_conn = sqlite3.connect(str(backup))
    try:
        source_conn.backup(backup_conn)
    finally:
        backup_conn.close()
        source_conn.close()

    print(f"Destination backup created: {backup}")
    return backup


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not SOURCE.exists() or not TARGET.exists():
        raise RuntimeError("Source or destination database does not exist.")

    source_uri = SOURCE.resolve().as_uri() + "?mode=ro"
    src = sqlite3.connect(source_uri, uri=True)
    dst = sqlite3.connect(str(TARGET), timeout=30)

    try:
        print("========== SOURCE VALIDATION ==========")
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Source database integrity check failed.")

        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Destination database integrity check failed.")

        source_counts = {}
        for table in TABLES:
            source_columns = columns(src, table)
            target_columns = columns(dst, table)

            if not source_columns or not target_columns:
                raise RuntimeError(f"Missing or empty table: {table}")

            missing = set(source_columns) - set(target_columns)
            if missing:
                raise RuntimeError(
                    f"{table}: destination is missing columns {sorted(missing)}"
                )

            source_counts[table] = count(src, table)
            destination_count = count(dst, table)

            print(
                f"{table}: source={source_counts[table]}, "
                f"destination={destination_count}"
            )

            if destination_count != 0:
                raise RuntimeError(
                    f"{table} is not empty in destination. Migration stopped."
                )

        open_trades = src.execute("""
            SELECT COUNT(*) FROM paper_trades
            WHERE status != 'CLOSED'
               OR opened_at IS NULL
               OR closed_at IS NULL
        """).fetchone()[0]

        if open_trades:
            raise RuntimeError(
                f"Found {open_trades} non-closed or incomplete paper trades."
            )

        missing_metadata = src.execute("""
            SELECT COUNT(*)
            FROM market_candles c
            LEFT JOIN verified_instruments v
              ON c.instrument_key = v.instrument_key
            WHERE v.instrument_key IS NULL
        """).fetchone()[0]

        if missing_metadata:
            raise RuntimeError(
                f"{missing_metadata} candles have no instrument metadata."
            )

        print("\n========== CONTRACT STATUS PLAN ==========")
        today = date.today()
        status_counts = {"EXPIRED": 0, "UNKNOWN": 0}

        instrument_rows = src.execute("""
            SELECT instrument_key, expiry
            FROM verified_instruments
        """).fetchall()

        for instrument_key, expiry_value in instrument_rows:
            try:
                expiry = date.fromisoformat(str(expiry_value)[:10])
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"Invalid expiry for instrument {instrument_key}: {expiry_value}"
                )

            status = "EXPIRED" if expiry < today else "UNKNOWN"
            status_counts[status] += 1

        print("Migration date:", today.isoformat())
        print("Expired contracts:", status_counts["EXPIRED"])
        print("Future/unverified contracts:", status_counts["UNKNOWN"])

        if args.dry_run:
            print("\n========== DRY RUN ==========")
        else:
            print("\n========== APPLY MIGRATION ==========")
            backup_database()

        dst.execute("BEGIN IMMEDIATE")

        try:
            for table in TABLES:
                source_columns = columns(src, table)
                column_sql = ", ".join(q(c) for c in source_columns)
                placeholders = ", ".join("?" for _ in source_columns)

                insert_sql = (
                    f"INSERT INTO {q(table)} "
                    f"({column_sql}) VALUES ({placeholders})"
                )

                cursor = src.execute(f"SELECT * FROM {q(table)}")
                inserted = 0

                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break

                    prepared = []
                    for original_row in batch:
                        row = dict(zip(source_columns, original_row))

                        if table == "verified_instruments":
                            expiry = date.fromisoformat(
                                str(row["expiry"])[:10]
                            )
                            row["contract_status"] = (
                                "EXPIRED" if expiry < today else "UNKNOWN"
                            )

                        prepared.append(tuple(row[c] for c in source_columns))

                    dst.executemany(insert_sql, prepared)
                    inserted += len(prepared)

                actual = count(dst, table)
                if actual != source_counts[table] or inserted != actual:
                    raise RuntimeError(
                        f"{table}: expected {source_counts[table]}, "
                        f"inserted {inserted}, destination has {actual}"
                    )

                print(f"{table}: {inserted} records inserted into transaction")

            for table in TABLES:
                actual = count(dst, table)
                if actual != source_counts[table]:
                    raise RuntimeError(
                        f"Final count mismatch for {table}: {actual}"
                    )

            integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"Destination integrity check failed: {integrity}"
                )

            if args.dry_run:
                dst.rollback()
                print("\nDRY RUN PASSED ? all inserts rolled back.")
                print("Destination database remains unchanged.")
            else:
                dst.commit()
                print("\nMIGRATION COMMITTED SUCCESSFULLY.")
                print("Original database remains untouched.")

        except Exception:
            dst.rollback()
            raise

    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
