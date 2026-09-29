"""Recover an ai-trader SQLite database that refuses to open.

The main file is damaged (valid header, corrupt page content). This script:

  1. copies the damaged file to data/ai_trader.db.corrupt.<timestamp> first,
  2. never writes to the original until a recovery has been verified,
  3. rebuilds a fresh database and copies across every row SQLite can still
     salvage with `PRAGMA writable_schema` + a raw page scan,
  4. prints exactly what was recovered and what was lost.

Run it with the panel and the trader stopped:

    .venv\\Scripts\\python recover_db.py            # dry run: report only
    .venv\\Scripts\\python recover_db.py --apply    # actually write the fix
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "ai_trader.db")
#: Tables the panel and agent need, in dependency-free order.
TABLES = ["trades", "trade_reviews", "positions", "trade_decisions", "market_snapshots",
          "market_analysis", "market_thesis", "news", "economic_events", "ai_requests",
          "token_usage", "system_events"]


def healthy(db_path):
    """True when the database opens and passes its own integrity check."""
    for path in sidecars(db_path):
        if os.path.exists(path):
            return False        # a sidecar present means the state is unsettled
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def total_rows(counts):
    return sum(n for n in (counts or {}).values() if n)


def sidecars(db_path):
    return [db_path + "-wal", db_path + "-shm"]


def clean_sidecars(db_path):
    """A stale -wal/-shm must never be applied to the file we are about to read."""
    removed = []
    for path in sidecars(db_path):
        if os.path.exists(path):
            os.remove(path)
            removed.append(os.path.basename(path))
    return removed


def build_schema(target):
    from app.database.models import SCHEMA
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def probe(source):
    """How much of the damaged file can SQLite still read?

    Returns (rows, tables, error). `tables` is a dict of table -> salvaged rows.
    """
    tables = {}
    try:
        conn = sqlite3.connect(source)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA writable_schema=ON")
            for name in TABLES:
                try:
                    cursor = conn.execute(f'SELECT * FROM "{name}"')
                    tables[name] = [dict(r) for r in cursor.fetchall()]
                except sqlite3.DatabaseError as exc:
                    tables[name] = None
                    print(f"  ! {name}: {exc}")
            conn.execute("PRAGMA writable_schema=OFF")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return None, None, str(exc)
    return tables, {k: (len(v) if v is not None else None) for k, v in tables.items()}, None


def recover(apply=False):
    if not os.path.exists(DB):
        print(f"No database at {DB}")
        return 1
    print(f"Database: {DB}")
    print(f"Size:     {os.path.getsize(DB):,} bytes\n")

    removed = clean_sidecars(DB)
    if removed:
        print(f"Removed stale sidecars: {', '.join(removed)}\n")

    if healthy(DB):
        print("Database opens cleanly and passes its integrity check; nothing to do.")
        return 0

    # Work on a copy so the original is untouched if anything below fails.
    sandbox = os.path.join(ROOT, "data", f".recover-{int(time.time())}.db")
    shutil.copy(DB, sandbox)
    print(f"Working on a copy: {os.path.basename(sandbox)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"{DB}.corrupt.{stamp}"
    shutil.copy(DB, backup)
    print(f"Original preserved at: {os.path.basename(backup)}\n")

    if healthy(DB):
        print("Database opens cleanly; nothing to recover.")
        return 0

    tables, counts, error = probe(sandbox)
    if error:
        print(f"UNREADABLE even with a raw scan: {error}")
        os.remove(sandbox)
        return 2

    total = sum(n for n in counts.values() if n)
    print("Salvageable rows per table:")
    for name, n in counts.items():
        print(f"  {name:20s} {'lost' if n is None else n}")

    if total == 0:
        print("\nNothing recoverable. Starting clean: the schema is rebuilt and "
              "the trader begins logging afresh.")
    if not apply:
        print("\nDry run. Re-run with --apply to write the recovered database.")
        os.remove(sandbox)
        return 0

    fresh = os.path.join(ROOT, "data", f".fresh-{int(time.time())}.db")
    build_schema(fresh)
    conn = sqlite3.connect(fresh)
    inserted = 0
    try:
        for name, rows in tables.items():
            if not rows:
                continue
            columns = list(rows[0].keys())
            placeholders = ",".join("?" * len(columns))
            quoted = ",".join(f'"{c}"' for c in columns)
            conn.executemany(
                f'INSERT OR REPLACE INTO "{name}" ({quoted}) VALUES ({placeholders})',
                [tuple(r.get(c) for c in columns) for r in rows])
            inserted += len(rows)
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()

    for path in sidecars(DB):
        if os.path.exists(path):
            os.remove(path)
    shutil.move(fresh, DB)
    os.remove(sandbox)
    print(f"\nRecovered {inserted} rows into a new {os.path.basename(DB)}")
    print(f"Integrity check: {integrity}")
    print(f"Broken file kept at: {os.path.basename(backup)}")
    print("\nStart the panel again. It should open cleanly.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the recovered database (default: report only)")
    sys.exit(recover(apply=parser.parse_args().apply))
