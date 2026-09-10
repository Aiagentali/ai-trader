"""Dashboard server — Flask-based local web UI for ai-trader monitoring.

Reads directly from SQLite. No Cloudflare dependency.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from flask import Flask, jsonify, render_template
from app.database.repository import Repository
from app.journal.performance import PerformanceTracker

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "static"))

_repo = None


def _get_repo():
    global _repo
    if _repo is None:
        _repo = Repository()
    return _repo


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    """Account summary, token usage, system health."""
    repo = _get_repo()
    acc_rows = repo.conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT 100"
    ).fetchall()
    trades = [dict(r) for r in acc_rows]
    token = repo.token_usage_today()

    # Daily PnL
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum((t.get("profit_loss") or 0) for t in trades
                     if (t.get("exit_ts") or "")[:10] == today)
    total_pnl = sum((t.get("profit_loss") or 0) for t in trades)

    # Open positions
    open_pos = repo.open_positions()

    # System events (last 10)
    events = repo.conn.execute(
        "SELECT * FROM system_events ORDER BY id DESC LIMIT 10").fetchall()

    return jsonify({
        "account": {"balance": 10000.0, "equity": 10000.0 + total_pnl},
        "today_pnl": round(today_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "open_positions": len(open_pos),
        "token_usage": token,
        "system_events": [{"event": e["event"], "ts": e["ts"]} for e in events],
    })


@app.route("/api/positions")
def api_positions():
    """Open positions."""
    repo = _get_repo()
    pos = repo.open_positions()
    return jsonify(pos)


@app.route("/api/trades")
def api_trades():
    """Recent closed trades."""
    repo = _get_repo()
    rows = repo.conn.execute(
        "SELECT * FROM trades ORDER BY opened_ts DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/performance")
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


@app.route("/api/thesis")
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


@app.route("/api/news")
def api_news():
    """Recent news."""
    repo = _get_repo()
    news = repo.recent_news(limit=20)
    return jsonify(news)


@app.route("/api/calendar")
def api_calendar():
    """Upcoming economic events."""
    repo = _get_repo()
    events = repo.upcoming_events(within_hours=48)
    return jsonify(events)


@app.route("/api/decisions")
def api_decisions():
    """Recent trade decisions."""
    repo = _get_repo()
    rows = repo.conn.execute(
        "SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tokens")
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


def create_app():
    """Factory for external use."""
    return app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
