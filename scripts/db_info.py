"""Print the schema and a few counts from the SQLite database.

Usage:
    python scripts/db_info.py
"""
from pathlib import Path
import sqlite3

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "sports_analyzer.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print(f"DB: {DB_PATH}")
    print(f"TABLES: {tables}")

    for table in tables:
        print(f"\n--- {table} ---")
        cur.execute(f"PRAGMA table_info({table})")
        for row in cur.fetchall():
            # row = (cid, name, type, notnull, default, pk)
            _, name, coltype, notnull, default, pk = row
            flags = []
            if notnull:
                flags.append("NOT NULL")
            if pk:
                flags.append("PK")
            if default is not None:
                flags.append(f"DEFAULT {default}")
            print(f"  {name:24} {coltype:8} {' '.join(flags)}")
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  rows: {cur.fetchone()[0]}")
        except sqlite3.Error as exc:
            print(f"  count failed: {exc}")

        cur.execute(f"PRAGMA index_list({table})")
        for idx in cur.fetchall():
            # (seq, name, unique, origin, partial)
            _, idx_name, unique, origin, _ = idx
            tag = "UNIQUE" if unique else "index"
            cur.execute(f"PRAGMA index_info({idx_name})")
            cols = ", ".join(r[2] for r in cur.fetchall())
            print(f"  [{tag}] {idx_name} ({cols})")

    # Duplicate check on the identity the API enforces.
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM matches
            GROUP BY league, season, date, home_team, away_team
            HAVING COUNT(*) > 1
        )
    """)
    dupes = cur.fetchone()[0]
    print(f"\nDuplicate fixtures (league+season+date+both teams): {dupes}")

    conn.close()


if __name__ == "__main__":
    main()
