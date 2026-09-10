"""Performance metrics — aggregates trade journal statistics."""
from datetime import datetime, timezone


class PerformanceTracker:
    """Calculate and break down trading performance metrics."""

    def __init__(self, repo):
        self.repo = repo

    def _closed_trades(self) -> list:
        rows = self.repo.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts ASC").fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        trades = self._closed_trades()
        if not trades:
            return {"total_trades": 0, "message": "no closed trades yet"}

        wins = [t for t in trades if (t.get("profit_loss") or 0) > 0]
        losses = [t for t in trades if (t.get("profit_loss") or 0) <= 0]
        pnls = [(t.get("profit_loss") or 0) for t in trades]
        r_multiples = [(t.get("r_multiple") or 0) for t in trades]

        total_pnl = sum(pnls)
        win_rate = len(wins) / len(trades) if trades else 0
        avg_win = sum((t.get("profit_loss") or 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum((t.get("profit_loss") or 0) for t in losses) / len(losses) if losses else 0
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0
        expectancy = total_pnl / len(trades) if trades else 0

        # Max drawdown
        equity_curve = []
        running = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            running += p
            equity_curve.append(running)
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        # Consecutive losses
        max_consec_loss = 0
        curr_consec = 0
        for t in trades:
            if (t.get("profit_loss") or 0) <= 0:
                curr_consec += 1
                max_consec_loss = max(max_consec_loss, curr_consec)
            else:
                curr_consec = 0

        max_consec_win = 0
        curr_consec = 0
        for t in trades:
            if (t.get("profit_loss") or 0) > 0:
                curr_consec += 1
                max_consec_win = max(max_consec_win, curr_consec)
            else:
                curr_consec = 0

        # Average holding time
        hold_times = []
        for t in trades:
            try:
                opened = datetime.fromisoformat(t["opened_ts"].replace("Z", "+00:00"))
                closed = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
                hold_times.append((closed - opened).total_seconds() / 3600)
            except (ValueError, TypeError, KeyError):
                pass
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "expectancy": round(expectancy, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_r_multiple": round(avg_r, 2),
            "max_drawdown": round(max_dd, 2),
            "max_consecutive_losses": max_consec_loss,
            "max_consecutive_wins": max_consec_win,
            "avg_holding_time_hours": round(avg_hold, 2),
        }

    def breakdown_by(self, field: str) -> dict:
        """Break down performance by a trade field (symbol, setup, market_regime, etc.)."""
        trades = self._closed_trades()
        groups = {}
        for t in trades:
            key = t.get(field, "unknown") or "unknown"
            if key not in groups:
                groups[key] = {"trades": 0, "wins": 0, "total_pnl": 0, "r_multiples": []}
            groups[key]["trades"] += 1
            pnl = t.get("profit_loss") or 0
            groups[key]["total_pnl"] += pnl
            if pnl > 0:
                groups[key]["wins"] += 1
            r = t.get("r_multiple") or 0
            groups[key]["r_multiples"].append(r)

        result = {}
        for key, g in groups.items():
            avg_r = sum(g["r_multiples"]) / len(g["r_multiples"]) if g["r_multiples"] else 0
            result[key] = {
                "trades": g["trades"],
                "win_rate": round(g["wins"] / g["trades"] * 100, 1) if g["trades"] else 0,
                "total_pnl": round(g["total_pnl"], 2),
                "avg_r": round(avg_r, 2),
            }
        return result

    def daily_breakdown(self) -> list:
        """PnL grouped by day."""
        trades = self._closed_trades()
        days = {}
        for t in trades:
            try:
                day = t["exit_ts"][:10]
            except (KeyError, TypeError):
                continue
            if day not in days:
                days[day] = {"pnl": 0, "trades": 0, "wins": 0}
            days[day]["pnl"] += t.get("profit_loss") or 0
            days[day]["trades"] += 1
            if (t.get("profit_loss") or 0) > 0:
                days[day]["wins"] += 1
        result = []
        for day, g in sorted(days.items()):
            result.append({"date": day, "pnl": round(g["pnl"], 2),
                           "trades": g["trades"],
                           "win_rate": round(g["wins"] / g["trades"] * 100, 1) if g["trades"] else 0})
        return result

    def review_summary(self) -> dict:
        """Summarize AI trade review classifications."""
        rows = self.repo.conn.execute(
            "SELECT classification, COUNT(*) as cnt FROM trade_reviews "
            "GROUP BY classification").fetchall()
        return {r["classification"]: r["cnt"] for r in rows}
