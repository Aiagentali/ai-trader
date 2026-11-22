"""Recovery of a damaged ai-trader database.

The main file is corrupted, not the sidecars: `database disk image is malformed`
comes from a file whose header is intact but whose page content is not. A foreign
`-wal` applied to a healthy main file, by contrast, opens without error (verified
while diagnosing the real incident), so deleting sidecars is not the fix.

recover_db.py must therefore salvage whatever pages still parse, never destroy the
evidence, and leave a working database behind.
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def _run(project, *args):
    return subprocess.run([sys.executable, "recover_db.py", *args], cwd=project,
                          capture_output=True, text=True)


def _populated(project, trades=60):
    sys.path.insert(0, ROOT)
    from app.database.repository import Repository
    db = os.path.join(project, "data", "ai_trader.db")
    repo = Repository(db)
    for i in range(trades):
        repo.save_trade({"opened_ts": datetime.now(timezone.utc).isoformat(),
                         "symbol": f"EURUSD{i}", "direction": "BUY",
                         "entry": 1.1 + i * 0.0001, "stop_loss": 1.099,
                         "take_profit": 1.102, "position_size": 0.01,
                         "status": "closed", "profit_loss": round(i * 0.25, 2)})
    repo.log_ai_request("analysis_EURUSD", "cl/nvidia/nemotron:free", 100, 50, 0.0, "EURUSD", "ok")
    repo.close()
    return db


def _damage_tail(db, start, length):
    """Corrupt later pages only, so earlier table data still parses."""
    with open(db, "r+b") as fh:
        raw = bytearray(fh.read())
    raw[start:start + length] = os.urandom(length)
    with open(db, "wb") as fh:
        fh.write(bytes(raw))


def _project(tmp_path):
    import shutil
    project = str(tmp_path / "proj")
    shutil.copytree(ROOT, project, symlinks=True,
                    ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "data"))
    os.makedirs(os.path.join(project, "data"), exist_ok=True)
    return project


def test_a_foreign_wal_alone_does_not_corrupt_a_healthy_database(tmp_path):
    """The premise of the earlier advice was wrong; prove it so nobody repeats it."""
    import shutil
    sys.path.insert(0, ROOT)
    from app.database.models import get_connection, init_db
    a = str(tmp_path / "a.db")
    other = str(tmp_path / "other.db")
    keep = sqlite3.connect(other)
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("CREATE TABLE news(id INTEGER PRIMARY KEY, body TEXT)")
    keep.executemany("INSERT INTO news(body) VALUES (?)", [("x" * 500,) for _ in range(800)])
    keep.commit()
    init_db(a)
    conn = get_connection(a)
    conn.execute("INSERT INTO system_events(ts, event) VALUES ('now', 'x')")
    conn.commit()
    conn.close()
    shutil.copy(other + "-wal", a + "-wal")
    shutil.copy(other + "-shm", a + "-shm")
    reopened = sqlite3.connect(a)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()
    keep.close()


def test_dry_run_never_touches_the_original(tmp_path):
    project = _project(tmp_path)
    db = _populated(project)
    _damage_tail(db, 6000, 3000)
    before = open(db, "rb").read()
    result = _run(project)
    assert result.returncode == 0
    assert open(db, "rb").read() == before
    assert "Dry run" in result.stdout


def test_apply_recovers_rows_from_a_partially_damaged_database(tmp_path):
    project = _project(tmp_path)
    db = _project_db = _populated(project, trades=60)
    _damage_tail(db, 6000, 3000)
    result = _run(project, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(_project_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        recovered = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        conn.close()
    assert recovered == 60, f"expected every salvageable trade, got {recovered}"


def test_apply_always_leaves_a_working_database(tmp_path):
    """Total damage must still end with an openable, schema-complete file."""
    project = _project(tmp_path)
    db = _populated(project, trades=20)
    _damage_tail(db, 100, 2000)      # destroys the schema itself
    result = _run(project, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "trades" in tables and "ai_requests" in tables


def test_the_broken_file_is_always_kept_as_evidence(tmp_path):
    project = _project(tmp_path)
    db = _populated(project, trades=10)
    _damage_tail(db, 6000, 3000)
    _run(project, "--apply")
    backups = [f for f in os.listdir(os.path.join(project, "data"))
               if ".corrupt." in f]
    assert len(backups) == 1, backups


def test_recovery_is_idempotent_and_leaves_no_temp_files(tmp_path):
    project = _project(tmp_path)
    db = _populated(project, trades=10)
    _damage_tail(db, 6000, 3000)
    _run(project, "--apply")
    first = _run(project)
    assert "already healthy" in first.stdout.lower() or first.returncode == 0
    leftovers = [f for f in os.listdir(os.path.join(project, "data"))
                 if f.startswith((".recover-", ".fresh-"))]
    assert leftovers == [], leftovers
