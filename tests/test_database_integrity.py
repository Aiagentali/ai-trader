"""A corrupt database must be reported, not exploded as a traceback.

The `-wal`/`-shm` sidecars were once committed to git, so a `git pull` applied
one machine's WAL over another's live database and SQLite refused to open it.
That produced a 500 traceback on every 15-second poll and a blank panel.
"""
import os
import sqlite3

import pytest

from app.database.models import DatabaseCorrupt, get_connection, init_db
from app.dashboard.server import create_app


def _corrupt(path):
    """A file that is not a database at all: the same open-time failure."""
    with open(path, "wb") as fh:
        fh.write(b"not a database" * 100)


def test_corrupt_database_raises_a_named_error_with_a_recovery_hint(tmp_path):
    db = str(tmp_path / "ai_trader.db")
    _corrupt(db)
    with pytest.raises(DatabaseCorrupt) as excinfo:
        get_connection(db)
    message = str(excinfo.value)
    assert "malformed" in message or "not a database" in message
    # the hint must name the actual files and path
    assert f"{db}-wal" in message
    assert f"{db}-shm" in message


def test_a_healthy_database_is_unaffected(tmp_path):
    db = str(tmp_path / "ai_trader.db")
    init_db(db)
    conn = get_connection(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


@pytest.fixture
def broken_app(tmp_path):
    db = tmp_path / "ai_trader.db"
    _corrupt(str(db))
    app = create_app({"TESTING": True, "DATABASE": str(db)})
    app.config["TESTING"] = True
    return app


def test_data_endpoints_report_a_broken_database_as_503(broken_app):
    """No traceback per poll: the operator gets JSON naming the problem."""
    client = broken_app.test_client()
    for path in ("/api/overview", "/api/positions", "/api/trades", "/api/decisions"):
        response = client.get(path)
        assert response.status_code == 503, path
        body = response.get_json()
        assert body["database_broken"] is True
        assert body["error"]


def test_debug_endpoints_report_a_broken_database_as_503(broken_app):
    client = broken_app.test_client()
    for path in ("/api/last_error", "/api/debug"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.get_json()["database_broken"] is True


def test_the_panel_shows_a_banner_element_for_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert 'id="db-alert"' in (root / "app/dashboard/templates/index.html").read_text()
    assert "db-alert" in (root / "app/dashboard/static/panel.js").read_text()


def test_sidecar_files_can_never_be_committed_again():
    """The root cause: .gitignore let data/ through."""
    from pathlib import Path
    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    assert "data/" in ignored
