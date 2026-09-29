"""Dashboard server — Flask-based local web UI for ai-trader monitoring.

Reads directly from SQLite. No Cloudflare dependency.
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from flask import Flask, Blueprint, current_app, g, jsonify, render_template
from app.database.models import DatabaseCorrupt
from app.database.repository import Repository
from app.journal.performance import PerformanceTracker

dashboard = Blueprint("dashboard", __name__, template_folder="templates", static_folder="static")


_repo_lock = threading.Lock()


class ApiProblem(RuntimeError):
    """A failure the operator can act on; surfaced as JSON, not a traceback."""


def _get_repo():
    if "repo" not in g:
        # Repository.__init__ runs schema init on a second connection; serializing
        # avoids concurrent WAL pragma contention on the same file.
        with _repo_lock:
            try:
                g.repo = Repository(current_app.config.get("DATABASE"))
            except DatabaseCorrupt as exc:
                # A broken database must not print a traceback on every 15s poll
                # and blank the whole panel. Report it once, as data the UI shows.
                current_app.logger.error("database unavailable: %s", exc)
                raise ApiProblem(str(exc))
    return g.repo



@dashboard.route("/")
def index():
    return render_template("index.html")


@dashboard.route("/api/overview")
def api_overview():
    """Account summary, token usage, system health."""
    repo = _get_repo()
    closed = repo.conn.execute(
        "SELECT profit_loss, exit_ts FROM trades WHERE status='closed'").fetchall()
    total_pnl = round(sum((r["profit_loss"] or 0) for r in closed), 2)

    # Open positions
    open_pos = repo.open_positions()
    token = repo.token_usage_today()

    # Daily PnL
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = round(sum((r["profit_loss"] or 0) for r in closed
                          if (r["exit_ts"] or "")[:10] == today), 2)

    # System events (last 10)
    events = repo.conn.execute(
        "SELECT * FROM system_events ORDER BY id DESC LIMIT 10").fetchall()

    return jsonify({
        "account": {"balance": None, "equity": None, "source": "unavailable"},
        "today_pnl": round(today_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "open_positions": len(open_pos),
        "token_usage": token,
        "system_events": [{"event": e["event"], "ts": e["ts"]} for e in events],
    })


@dashboard.route("/api/positions")
def api_positions():
    """Open positions."""
    repo = _get_repo()
    pos = repo.open_positions()
    return jsonify(pos)


@dashboard.route("/api/trades")
def api_trades():
    """Recent closed trades."""
    repo = _get_repo()
    rows = repo.conn.execute(
        "SELECT * FROM trades ORDER BY opened_ts DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


@dashboard.route("/api/performance")
def api_performance():
    """Performance metrics."""
    repo = _get_repo()
    tracker = PerformanceTracker(repo)
    return jsonify({
        "summary": tracker.summary(),
        "by_symbol": tracker.breakdown_by("symbol"),
        "by_setup": tracker.breakdown_by("setup"),
        "by_regime": tracker.breakdown_by("market_regime"),
        "daily": tracker.daily_breakdown(),
        "reviews": tracker.review_summary(),
    })


@dashboard.route("/api/thesis")
def api_thesis():
    """Latest thesis per symbol."""
    repo = _get_repo()
    from app.config.config_loader import load_symbols
    sym_cfg = load_symbols()
    symbols = [s for s, v in sym_cfg["symbols"].items() if v.get("enabled", True)]
    result = {}
    for sym in symbols:
        t = repo.latest_thesis(sym)
        if t:
            result[sym] = {
                "bias": t["bias"],
                "regime": t["regime"],
                "thesis": t["thesis"],
                "invalidation": t["invalidation"],
                "ts": t["ts"],
            }
        else:
            result[sym] = {"bias": "NO_DATA", "regime": "UNKNOWN",
                           "thesis": "No analysis yet", "ts": None}
    return jsonify(result)


@dashboard.route("/api/news")
def api_news():
    """Recent news."""
    repo = _get_repo()
    news = repo.recent_news(limit=20)
    return jsonify(news)


@dashboard.route("/api/calendar")
def api_calendar():
    """Upcoming economic events."""
    repo = _get_repo()
    events = repo.upcoming_events(within_hours=48)
    return jsonify(events)


@dashboard.route("/api/decisions")
def api_decisions():
    """Recent trade decisions."""
    repo = _get_repo()
    rows = repo.conn.execute(
        "SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])


@dashboard.route("/api/tokens")
def api_tokens():
    """Token usage breakdown."""
    repo = _get_repo()
    usage = repo.token_usage_today()
    requests = repo.conn.execute(
        "SELECT task, COUNT(*) as cnt, SUM(total_tokens) as total, "
        "SUM(estimated_cost) as cost FROM ai_requests "
        "WHERE ts LIKE ? GROUP BY task",
        (f"{usage['day']}%",)).fetchall()
    return jsonify({
        "usage": usage,
        "by_task": [dict(r) for r in requests],
    })


def create_app(config=None):
    from pathlib import Path
    from app.dashboard.control import register_control
    app = Flask(__name__)
    root = Path(__file__).resolve().parents[2]
    app.config.update(PANEL_ROOT=str(root), PANEL_ENV=str(root / ".env"),
                      MAX_CONTENT_LENGTH=16384)
    if config:
        app.config.update(config)
    app.register_blueprint(dashboard)

    @app.errorhandler(ApiProblem)
    def _handle_api_problem(exc):
        """A broken database is operator-actionable data, not a server bug.

        Without this every poll returned a 500 traceback and the panel rendered
        blank, hiding the one thing the user needed to read.
        """
        return jsonify(error=str(exc), database_broken=True), 503

    @app.teardown_appcontext
    def close_repository(error=None):
        repo = g.pop("repo", None)
        if repo:
            repo.close()

    register_control(app, _get_repo)
    return app


app = create_app()

if __name__ == "__main__":  # prefer panel.py; kept for direct runs
    app.run(host="127.0.0.1", port=8000, debug=False, use_reloader=False)
