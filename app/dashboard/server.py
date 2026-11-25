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
from app.strategy.trainer import is_mt5_running

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



def _format_rejection_reason(reasons) -> str:
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except Exception:
            reasons = [reasons]
    if not reasons:
        return "عدم تأیید توسط مدیریت ریسک"
    parts = []
    for r in reasons:
        r_lower = str(r).lower()
        if "position size rounds to zero" in r_lower or "rounds to zero" in r_lower:
            parts.append("سایز پوزیشن برای بالانس حساب کوچک‌تر از حداقل 0.01 لات شد")
        elif "daily loss" in r_lower:
            parts.append("حد ضرر مجاز روزانه فعال شده است")
        elif "consecutive losses" in r_lower:
            parts.append("تعداد زیان‌های متوالی به سقف رسید")
        elif "spread" in r_lower:
            parts.append("اسپرد از حد مجاز فراتر رفت")
        elif "correlated" in r_lower:
            parts.append("سقف ریسک همبستگی ارزها تکمیل شده است")
        elif "survival floor" in r_lower:
            parts.append("بالانس حساب به حداقل کف ایمنی برای ترید رسیده است")
        elif "risk reward" in r_lower or "rr" in r_lower:
            parts.append("نسبت سود به زیان (R:R) کمتر از حد مجاز است")
        else:
            parts.append(str(r))
    return " | ".join(parts)


def _format_thesis_persian(bias: str, regime: str, thesis_text: str) -> dict:
    t_lower = (thesis_text or "").lower()
    r_lower = (regime or "").lower()
    if "ranging_no_clear_trend" in r_lower or "mixed or flat" in t_lower:
        return {
            "desc": "روند کلی بازار رِنج/خنثی است؛ میانگین‌های متحرک جهت مشخصی ندارند (عدم ورود برای پرهیز از تله نوسانات)",
            "category": "روند خنثی"
        }
    if "lacking directional momentum" in t_lower or "adx" in t_lower:
        return {
            "desc": "شاخص مومنتوم ADX در سطح پایینی است؛ بازار فاقد جهت‌گیری و شتاب حرکتی لازم برای ورود است",
            "category": "مومنتوم ناکافی"
        }
    if "lacks institutional volume" in t_lower or "tick volume" in t_lower or "low_vol" in r_lower:
        return {
            "desc": "حجم تیک‌های معاملاتی کمتر از میانگین است؛ نقدینگی نهادی کافی برای جهش وجود ندارد",
            "category": "حجم پایین"
        }
    if "rollover" in t_lower or "rollover" in r_lower:
        return {
            "desc": "ساعت تغییر شیفت روزانه بازار (Rollover) با اسپرد بسیار بالا؛ معاملات موقتاً قفل است",
            "category": "ساعت تعویض شیفت"
        }
    if "outside allowed" in t_lower:
        return {
            "desc": "خارج از ساعات مجاز کاری بازارهای لندن و نیویورک",
            "category": "خارج از نشست معاملاتی"
        }
    if "rejected" in t_lower:
        return {
            "desc": "فرصت معاملاتی شناسایی شد اما توسط فیلتر مدیریت ریسک و سرمایه رد گردید",
            "category": "رد شده توسط ریسک"
        }
    if "watching_pullback" in r_lower or "waiting for high-probability" in t_lower:
        return {
            "desc": "جهت روند بازار شناسایی شده؛ در کمین پولبک به میانگین متحرک و کندل تأیید ورود هستیم",
            "category": "در انتظار پولبک ورود"
        }
    if bias in ("BUY", "SELL"):
        return {
            "desc": f"موقعیت ورود فعال به صورت {bias} شناسایی شد",
            "category": f"سیگنال {bias}"
        }
    return {
        "desc": thesis_text or "در حال پایش دیتای زنده مارکت...",
        "category": "تحلیل مارکت"
    }


@dashboard.route("/")
def index():
    return render_template("index.html")


@dashboard.route("/favicon.ico")
def favicon():
    """Silence browser favicon 404s."""
    return "", 204


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

    # Recent rejections (last 5 setups vetoed by risk or AI)
    rej_rows = repo.conn.execute(
        "SELECT symbol, decision, reject_reasons, ts, payload FROM trade_decisions "
        "WHERE decision IN ('BUY', 'SELL') AND approved = 0 "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    recent_rejections = []
    for r in rej_rows:
        reasons = []
        try:
            reasons = json.loads(r["reject_reasons"] or "[]")
        except Exception:
            reasons = [r["reject_reasons"]] if r["reject_reasons"] else []
        recent_rejections.append({
            "symbol": r["symbol"],
            "decision": r["decision"],
            "ts": r["ts"],
            "reasons": reasons,
            "persian_reason": _format_rejection_reason(reasons)
        })

    acc_info = {"balance": None, "equity": None, "source": "unavailable", "server": None, "login": None}
    if not current_app.config.get("TESTING") and is_mt5_running():
        try:
            import MetaTrader5 as mt5
            term = getattr(mt5, "terminal_info", lambda: None)()
            if term is not None or mt5.initialize(timeout=1000):
                acc = mt5.account_info()
                if acc:
                    acc_info = {
                        "balance": round(acc.balance, 2),
                        "equity": round(acc.equity, 2),
                        "login": acc.login,
                        "server": acc.server,
                        "currency": acc.currency,
                        "source": "mt5_demo"
                    }
                # Sync MT5 open positions count
                mt5_p = mt5.positions_get()
                if mt5_p is not None:
                    open_pos = mt5_p
                # Sync MT5 today & total closed deals
                now_dt = datetime.now(timezone.utc)
                start_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc)
                deals = mt5.history_deals_get(start_dt, now_dt)
                if deals:
                    mt5_today = round(sum(d.profit + d.swap + d.commission for d in deals if d.entry in (1, 2)), 2)
                    if mt5_today != 0:
                        today_pnl = mt5_today
                all_deals = mt5.history_deals_get(datetime(2020, 1, 1, tzinfo=timezone.utc), now_dt)
                if all_deals:
                    mt5_total = round(sum(d.profit + d.swap + d.commission for d in all_deals if d.entry in (1, 2)), 2)
                    if any(d.entry in (1, 2) for d in all_deals):
                        total_pnl = mt5_total
        except Exception:
            pass

    bal = acc_info.get("balance") or 30.0
    base_capital = max(10.0, bal - total_pnl)
    today_pnl_pct = round((today_pnl / base_capital * 100), 2)
    total_pnl_pct = round((total_pnl / base_capital * 100), 2)

    return jsonify({
        "account": acc_info,
        "today_pnl": round(today_pnl, 2),
        "today_pnl_percent": today_pnl_pct,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_percent": total_pnl_pct,
        "open_positions": len(open_pos),
        "token_usage": token,
        "system_events": [{"event": e["event"], "ts": e["ts"]} for e in events],
        "recent_rejections": recent_rejections,
    })


@dashboard.route("/api/positions")
def api_positions():
    """Open positions."""
    repo = _get_repo()
    pos = repo.open_positions()
    if not current_app.config.get("TESTING") and is_mt5_running():
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None or mt5.initialize(timeout=1000):
                mt5_pos = mt5.positions_get()
                if mt5_pos is not None:
                    pos = [{
                        "ticket": str(p.ticket),
                        "symbol": p.symbol,
                        "direction": "BUY" if p.type == 0 else "SELL",
                        "volume": round(p.volume, 2),
                        "entry_price": p.price_open,
                        "stop_loss": p.sl,
                        "take_profit": p.tp,
                        "profit_loss": round(p.profit, 2),
                        "time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat()
                    } for p in mt5_pos]
        except Exception:
            pass
    return jsonify(pos)


@dashboard.route("/api/symbols")
def api_symbols():
    """Get active watchlist symbols from config and MT5 Market Watch."""
    from app.config.config_loader import load_symbols, load_settings
    sym_cfg = load_symbols()
    cfg_symbols = [s for s, v in sym_cfg["symbols"].items() if v.get("enabled", True)]
    settings = load_settings()
    mw_symbols = []
    if not current_app.config.get("TESTING") and is_mt5_running():
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None or mt5.initialize(timeout=1000):
                symbols_info = mt5.symbols_get()
                if symbols_info:
                    mw_symbols = [s.name for s in symbols_info if s.visible]
        except Exception:
            pass
    all_symbols = []
    source_list = mw_symbols if (mw_symbols and settings.get("trading", {}).get("use_market_watch")) else cfg_symbols
    for s in source_list:
        if s not in all_symbols:
            all_symbols.append(s)
    if not all_symbols:
        all_symbols = ["EURUSD", "XAUUSD", "UKBRENT.F27", "US30CASH", "GBPUSD", "USDJPY"]
    return jsonify({"symbols": all_symbols})


@dashboard.route("/api/trades")
def api_trades():
    """Recent trades with asset class classification and post-mortem review."""
    repo = _get_repo()
    from app.journal.performance import get_asset_class
    from app.journal.weekly_audit import _classify_loss_source
    rows = repo.conn.execute(
        "SELECT t.*, r.classification AS review_class, r.review AS review_text "
        "FROM trades t LEFT JOIN trade_reviews r ON t.trade_id = r.trade_id "
        "ORDER BY t.opened_ts DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["asset_class"] = get_asset_class(d.get("symbol"))
        if (d.get("profit_loss") or 0) < 0 and not d.get("review_text"):
            cat, note = _classify_loss_source(d, d.get("review_class") or "")
            d["review_class"] = d.get("review_class") or cat
            d["review_text"] = note
        out.append(d)
    return jsonify(out)


@dashboard.route("/api/performance")
def api_performance():
    """Performance metrics, loss post-mortems, and learned profiles."""
    repo = _get_repo()
    from app.journal.weekly_audit import _classify_loss_source
    curr_bal = None
    if not current_app.config.get("TESTING") and is_mt5_running():
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None or mt5.initialize(timeout=500):
                acc = mt5.account_info()
                if acc:
                    curr_bal = float(acc.balance)
        except Exception:
            pass

    tracker = PerformanceTracker(repo)
    sum_data = tracker.summary(current_balance=curr_bal)

    # Recent loss reviews (backfill deterministic post-mortem if no DB review exists)
    loss_rows = repo.conn.execute(
        "SELECT t.*, r.classification, r.review FROM trades t "
        "LEFT JOIN trade_reviews r ON t.trade_id = r.trade_id "
        "WHERE t.status='closed' AND t.profit_loss < 0 "
        "ORDER BY t.exit_ts DESC LIMIT 10"
    ).fetchall()
    loss_audits = []
    for r in loss_rows:
        d = dict(r)
        if not d.get("review"):
            cat, note = _classify_loss_source(d, d.get("classification") or "")
            d["classification"] = d.get("classification") or cat
            d["review"] = note
        loss_audits.append({
            "trade_id": d.get("trade_id"),
            "symbol": d.get("symbol"),
            "direction": d.get("direction"),
            "profit_loss": d.get("profit_loss"),
            "exit_ts": d.get("exit_ts"),
            "setup": d.get("setup"),
            "classification": d.get("classification"),
            "review": d.get("review"),
        })

    learned = {}
    try:
        from pathlib import Path
        lp = Path("data/learned_profiles.json")
        if lp.exists():
            learned = json.loads(lp.read_text(encoding="utf-8"))
    except Exception:
        pass

    return jsonify({
        "summary": sum_data,
        "by_symbol": tracker.breakdown_by("symbol"),
        "by_setup": tracker.breakdown_by("setup"),
        "by_regime": tracker.breakdown_by("market_regime"),
        "by_asset_class": sum_data.get("by_asset_class", {}),
        "daily": tracker.daily_breakdown(),
        "reviews": tracker.review_summary(),
        "loss_audits": loss_audits,
        "learned_profiles": learned,
    })


@dashboard.route("/api/thesis")
def api_thesis():
    """Latest thesis & pipeline audit per active symbol (including Crypto & Market Watch)."""
    repo = _get_repo()
    from app.config.config_loader import load_symbols, load_settings
    from app.risk.engine import is_crypto_symbol, is_market_open
    sym_cfg = load_symbols()
    cfg_symbols = [s for s, v in sym_cfg["symbols"].items() if v.get("enabled", True)]
    settings = load_settings()

    mw_symbols = []
    if not current_app.config.get("TESTING") and is_mt5_running():
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None or mt5.initialize(timeout=1000):
                symbols_info = mt5.symbols_get()
                if symbols_info:
                    mw_symbols = [s.name for s in symbols_info if s.visible]
        except Exception:
            pass

    # Merge config/MW symbols with any symbols recently recorded in market_thesis
    db_syms = [r["symbol"] for r in repo.conn.execute(
        "SELECT DISTINCT symbol FROM market_thesis ORDER BY id DESC LIMIT 30").fetchall()]

    symbols = []
    base_list = mw_symbols if (mw_symbols and settings.get("trading", {}).get("use_market_watch")) else cfg_symbols
    for s in list(base_list) + list(cfg_symbols) + list(db_syms):
        if s and s not in symbols:
            symbols.append(s)

    result = {}
    for sym in symbols:
        t = repo.latest_thesis(sym)
        crypto = is_crypto_symbol(sym)
        mkt_open = is_market_open(sym)
        if t:
            payload = {}
            try:
                payload = json.loads(t.get("payload") or "{}")
            except Exception:
                payload = {}
            p_info = _format_thesis_persian(t["bias"], t["regime"], t["thesis"])
            persian_desc = payload.get("persian_reason") or p_info["desc"]
            cat = p_info["category"]
            if t["bias"] in ("BUY", "SELL"):
                cat = f"ورود فعال ({t['bias']})"
            elif t["bias"] == "MARKET_CLOSED" or not mkt_open:
                cat = "بازار بسته (تعطیلات)"
                if not mkt_open and "تعطیلات" not in persian_desc:
                    persian_desc = "بازار در تعطیلات آخر هفته بسته است؛ در حال حاضر فقط رمز ارزها (۲۴/۷) پایش می‌شوند."
            result[sym] = {
                "bias": t["bias"],
                "regime": t["regime"],
                "thesis": t["thesis"],
                "invalidation": t["invalidation"],
                "ts": t["ts"],
                "candle_time": payload.get("candle_time", ""),
                "analyzed_at": payload.get("analyzed_at", t["ts"]),
                "entry_tf": payload.get("entry_tf", ""),
                "trend_tf": payload.get("trend_tf", ""),
                "pipeline_stages": payload.get("pipeline_stages", []),
                "market_context": payload.get("market_context", {}),
                "is_crypto": crypto,
                "market_open": mkt_open,
                "persian_desc": persian_desc,
                "persian_category": cat,
            }
        else:
            result[sym] = {
                "bias": "NO_DATA",
                "regime": "UNKNOWN" if mkt_open else "MARKET_CLOSED",
                "thesis": "No analysis yet",
                "ts": None,
                "candle_time": "",
                "analyzed_at": None,
                "pipeline_stages": [
                    {"key": "market", "label": "بازار", "status": "pass" if mkt_open else "fail",
                     "detail": "24/7" if crypto else ("باز" if mkt_open else "تعطیلات")}
                ],
                "market_context": {},
                "is_crypto": crypto,
                "market_open": mkt_open,
                "persian_desc": "بازار در تعطیلات بسته است (فقط کریپتو فعال است)." if not mkt_open else "هنوز تحلیلی برای این نماد ثبت نشده است (منتظر تیک بعدی ربات).",
                "persian_category": "بازار بسته" if not mkt_open else "بدون دیتا",
            }
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


@dashboard.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    """Run walk-forward historical simulation on real MT5 candles."""
    from flask import request
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "EURUSD")).upper()
    count = min(5000, max(150, int(data.get("count", 1000))))
    balance = max(10.0, float(data.get("balance", 30.0)))
    
    from app.strategy.trainer import run_backtest_simulation
    res = run_backtest_simulation(symbol, count=count, initial_balance=balance)
    return jsonify(res)


@dashboard.route("/api/train/run", methods=["POST"])
def api_train_run():
    """Run weekend walk-forward self-training & error minimization."""
    from flask import request
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "EURUSD")).upper()
    count = min(5000, max(200, int(data.get("count", 1500))))
    balance = max(10.0, float(data.get("balance", 30.0)))

    from app.strategy.trainer import self_train_and_optimize
    repo = _get_repo()
    res = self_train_and_optimize(symbol, count=count, initial_balance=balance, repo=repo)
    return jsonify(res)


@dashboard.route("/api/config/full", methods=["GET", "POST"])
def api_config_full():
    """Load or update settings.yaml directly from the dashboard."""
    import yaml
    from app.config.config_loader import SETTINGS_FILE
    from flask import request
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        if "trading" in data:
            cfg.setdefault("trading", {}).update(data["trading"])
        if "decision_ai" in data:
            cfg.setdefault("decision_ai", {}).update(data["decision_ai"])
        if "analysis" in data:
            cfg.setdefault("analysis", {}).update(data["analysis"])

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
        return jsonify({"ok": True, "message": "Settings updated successfully"})

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return jsonify(cfg)


@dashboard.route("/api/reports/weekly")
def api_weekly_report():
    """Generate and return the weekly Markdown audit report."""
    from app.journal.weekly_audit import generate_weekly_markdown
    repo = _get_repo()
    out_dir = current_app.config.get("PANEL_ROOT", ".")
    from pathlib import Path
    rep_dir = str(Path(out_dir) / "reports")
    res = generate_weekly_markdown(repo, output_dir=rep_dir)
    return jsonify({"ok": True, **res})


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
