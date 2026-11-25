"""Weekly Markdown Trade Journal & Loss Root-Cause Audit Generator.

Distinguishes system/execution faults (fixable in code) from natural market
variance (do not overfit) and tracks confidence buckets (65-79% vs 80%+)
to empirically validate Decision AI thresholds.
"""
from datetime import datetime, timezone
from pathlib import Path
from app.journal.performance import PerformanceTracker, get_asset_class


def _classify_loss_source(trade: dict, review_cls: str = "") -> tuple[str, str]:
    """Return (category_tag, explanation) separating System Fault vs Market Variance."""
    setup = (trade.get("setup") or "").upper()
    if "TEST" in setup:
        return "SYSTEM_TEST", "معامله تست سیستمی؛ از ارزیابی استراتژی کنار گذاشته شود."

    cls = (review_cls or "").upper()
    if cls == "SL_TOO_TIGHT":
        return "SYSTEM_FAULT", "حد ضرر در کمتر از ۱۵ دقیقه فعال شد (احتمال نوسان اسپرد یا SL تنگ نسبت به ATR)."

    opened_ts = trade.get("opened_ts") or ""
    exit_ts = trade.get("exit_ts") or ""
    try:
        t0 = datetime.fromisoformat(str(opened_ts).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
        dur_min = (t1 - t0).total_seconds() / 60.0
        if dur_min < 10.0:
            return "SYSTEM_FAULT", f"بسته شدن سریع در {dur_min:.1f} دقیقه؛ نیاز به بررسی اسپرد/تایید کندل."
    except Exception:
        pass

    rr = float(trade.get("rr") or 0.0)
    if 0 < rr < 1.3:
        return "SYSTEM_FAULT", f"نسبت سود به زیان پایین (R:R={rr:.2f})؛ تنظیمات ورود باید سخت‌گیرانه‌تر شود."

    return "MARKET_VARIANCE", "ستاپ و مدیریت ریسک سالم بود؛ برگشت طبیعی بازار (نیاز به تغییر کد ندارد)."


def _bucket_stats(trades: list, key: str) -> list[dict]:
    buckets = [
        ("< 65%", lambda v: v < 65),
        ("65% - 79%", lambda v: 65 <= v < 80),
        ("80%+", lambda v: v >= 80),
    ]
    out = []
    for label, pred in buckets:
        sub = [t for t in trades if t.get(key) is not None and pred(float(t[key]))]
        wins = sum(1 for t in sub if (t.get("profit_loss") or 0) > 0)
        pnl = round(sum((t.get("profit_loss") or 0) for t in sub), 2)
        wr = round((wins / len(sub) * 100), 1) if sub else 0.0
        out.append({"label": label, "trades": len(sub), "wins": wins, "losses": len(sub) - wins, "win_rate": wr, "pnl": pnl})
    return out


def generate_weekly_markdown(repo, output_dir: str = "reports") -> dict:
    """Build weekly Markdown audit and save it under reports/weekly_YYYY-WW.md."""
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.isocalendar()
    filename = f"weekly_{iso_year}-W{iso_week:02d}.md"

    tracker = PerformanceTracker(repo)
    summary = tracker.summary()

    rows = repo.conn.execute(
        "SELECT t.*, r.classification AS review_class, r.review AS review_text "
        "FROM trades t LEFT JOIN trade_reviews r ON t.trade_id = r.trade_id "
        "WHERE t.status='closed' ORDER BY t.exit_ts DESC"
    ).fetchall()
    closed = [dict(r) for r in rows]

    lines = [
        f"# گزارش ممیزی هفتگی معاملات — هفته {iso_week} سال {iso_year}",
        "",
        f"- **تاریخ تولید گزارش (UTC):** {now.strftime('%Y-%m-%d %H:%M')}",
        f"- **کل معاملات بسته‌شده:** {len(closed)}",
        f"- **سود / زیان خالص کل:** {summary.get('total_pnl', 0):+.2f} $ ({summary.get('return_percent', 0):+.2f}%)",
        f"- **نرخ برد (Win Rate):** {summary.get('win_rate', 0)}% | **Profit Factor:** {summary.get('profit_factor', 0)} | **Sharpe:** {summary.get('sharpe_ratio', 0)}",
        f"- **حداکثر افت سرمایه (Max DD):** ${summary.get('max_drawdown', 0):.2f} ({summary.get('max_drawdown_pct', 0)}%)",
        "",
        "## ۱. تفکیک کریپتو در مقابل فارکس و کالاها (Crypto vs TradFi)",
        "",
        "| بازار | تعداد ترید | برد / باخت | وین‌ریت | سود خالص ($) | بازدهی (%) | Profit Factor |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    c_st = summary.get("crypto_stats", {})
    t_st = summary.get("tradfi_stats", {})
    lines.append(f"| Crypto (24/7) | {c_st.get('trades', 0)} | {c_st.get('wins', 0)} / {c_st.get('losses', 0)} | {c_st.get('win_rate', 0)}% | {c_st.get('total_pnl', 0):+.2f} $ | {c_st.get('return_percent', 0):+.2f}% | {c_st.get('profit_factor', 0)} |")
    lines.append(f"| TradFi (FX/Gold/Oil) | {t_st.get('trades', 0)} | {t_st.get('wins', 0)} / {t_st.get('losses', 0)} | {t_st.get('win_rate', 0)}% | {t_st.get('total_pnl', 0):+.2f} $ | {t_st.get('return_percent', 0):+.2f}% | {t_st.get('profit_factor', 0)} |")

    lines.extend([
        "",
        "## ۲. اعتبارسنجی آستانه اطمینان (Confidence Threshold Audit)",
        "",
        "### الف) بر اساس امتیاز Quant (`ai_confidence`)",
        "| بازه امتیاز | تعداد | برد / باخت | وین‌ریت | سود خالص ($) |",
        "|---|---:|---:|---:|---:|",
    ])
    for b in _bucket_stats(closed, "ai_confidence"):
        lines.append(f"| {b['label']} | {b['trades']} | {b['wins']} / {b['losses']} | {b['win_rate']}% | {b['pnl']:+.2f} $ |")

    gem_trades = [t for t in closed if t.get("gemini_prob") is not None]
    lines.extend([
        "",
        "### ب) بر اساس احتمال خالص Gemini (`gemini_prob`)",
    ])
    if gem_trades:
        lines.extend([
            "| بازه احتمال Gemini | تعداد | برد / باخت | وین‌ریت | سود خالص ($) |",
            "|---|---:|---:|---:|---:|",
        ])
        for b in _bucket_stats(gem_trades, "gemini_prob"):
            lines.append(f"| {b['label']} | {b['trades']} | {b['wins']} / {b['losses']} | {b['win_rate']}% | {b['pnl']:+.2f} $ |")
    else:
        lines.append("> هنوز معامله‌ای با ثبت مجزای `gemini_prob` بسته نشده است (از این پس برای تمام معاملات جدید ثبت می‌شود).")

    losses = [t for t in closed if (t.get("profit_loss") or 0) < 0]
    lines.extend([
        "",
        "## ۳. کالبدشکافی معاملات زیان‌ده (تفکیک خطای سیستم از نوسان طبیعی)",
        "",
    ])
    sys_faults = 0
    mkt_var = 0
    if losses:
        lines.append("| نماد | جهت | زیان ($) | Quant / Gemini | نوع علت | تحلیل و اقدام پیشنهادی |")
        lines.append("|---|---|---:|---|---|---|")
        for t in losses:
            cat, note = _classify_loss_source(t, t.get("review_class") or "")
            if cat == "SYSTEM_FAULT":
                sys_faults += 1
            elif cat == "MARKET_VARIANCE":
                mkt_var += 1
            q_conf = t.get("ai_confidence") if t.get("ai_confidence") is not None else "-"
            g_prob = t.get("gemini_prob") if t.get("gemini_prob") is not None else "-"
            rev = t.get("review_text") or note
            lines.append(f"| {t.get('symbol')} ({get_asset_class(t.get('symbol'))}) | {t.get('direction')} | {float(t.get('profit_loss') or 0):+.2f} $ | {q_conf}% / {g_prob}% | **{cat}** | {rev} |")
    else:
        lines.append("هیچ معامله زیان‌دهی در این بازه ثبت نشده است.")

    lines.extend([
        "",
        "## ۴. جمع‌بندی آخر هفته",
        f"- **خطاهای سیستمی/اجرایی نیازمند بررسی:** {sys_faults} مورد",
        f"- **ضررهای ناشی از نوسان طبیعی بازار (بدون نیاز به تغییر کد):** {mkt_var} مورد",
        "",
    ])

    md = "\n".join(lines)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(md, encoding="utf-8")
    return {"path": str(out_path), "filename": filename, "markdown": md}


if __name__ == "__main__":
    from app.database.repository import Repository
    r = Repository()
    res = generate_weekly_markdown(r)
    print(f"Generated: {res['path']}")
