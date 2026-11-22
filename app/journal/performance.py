"""Performance metrics — aggregates trade journal statistics."""
from datetime import datetime, timezone


def get_asset_class(symbol: str) -> str:
    s = (symbol or "").upper()
    if any(c in s for c in ("BTC", "ETH", "SOL", "BNB", "LTC", "XRP", "DOGE", "CRYPTO")):
        return "crypto"
    if any(c in s for c in ("XAU", "GOLD", "XAG", "SILVER")):
        return "metals"
    if any(c in s for c in ("BRENT", "OIL", "WTI", "NATGAS")):
        return "commodities"
    if any(c in s for c in ("US30", "WS30", "DJ", "SPX", "NAS", "NDX", "DAX", "UK100")):
        return "indices"
    return "forex"


def _calc_stats(trades_subset: list, initial_balance: float = 30.0) -> dict:
    if not trades_subset:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "profit_factor": 0.0, "return_percent": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0
        }
    wins = [t for t in trades_subset if (t.get("profit_loss") or 0) > 0]
    losses = [t for t in trades_subset if (t.get("profit_loss") or 0) <= 0]
    pnls = [(t.get("profit_loss") or 0) for t in trades_subset]
    total_pnl = sum(pnls)
    win_rate = (len(wins) / len(trades_subset) * 100) if trades_subset else 0.0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    ret_pct = round((total_pnl / initial_balance * 100), 2) if initial_balance > 0 else 0.0
    avg_win = sum((t.get("profit_loss") or 0) for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum((t.get("profit_loss") or 0) for t in losses) / len(losses) if losses else 0.0
    return {
        "trades": len(trades_subset),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "profit_factor": pf,
        "return_percent": ret_pct,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


class PerformanceTracker:
    """Calculate and break down trading performance metrics."""

    def __init__(self, repo):
        self.repo = repo

    def _closed_trades(self) -> list:
        rows = self.repo.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts ASC").fetchall()
        return [dict(r) for r in rows]

    def summary(self, current_balance: float = None) -> dict:
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

        # Base balance for percentage returns
        base_bal = 30.0
        if current_balance and current_balance > 0:
            diff = current_balance - total_pnl
            if diff > 5.0:
                base_bal = diff
        return_pct = round((total_pnl / base_bal * 100), 2)

        # Asset class breakdowns
        crypto_trades = [t for t in trades if get_asset_class(t.get("symbol")) == "crypto"]
        tradfi_trades = [t for t in trades if get_asset_class(t.get("symbol")) != "crypto"]
        forex_trades = [t for t in trades if get_asset_class(t.get("symbol")) == "forex"]
        metals_trades = [t for t in trades if get_asset_class(t.get("symbol")) == "metals"]
        commodities_trades = [t for t in trades if get_asset_class(t.get("symbol")) == "commodities"]
        indices_trades = [t for t in trades if get_asset_class(t.get("symbol")) == "indices"]

        # Max drawdown
        equity_curve = []
        running = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            running += p
            equity_curve.append(round(running, 2))
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
            "return_percent": return_pct,
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 99.0,
            "expectancy": round(expectancy, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_r_multiple": round(avg_r, 2),
            "max_drawdown": round(max_dd, 2),
            "max_consecutive_losses": max_consec_loss,
            "max_consecutive_wins": max_consec_win,
            "avg_holding_time_hours": round(avg_hold, 2),
            "equity_curve": equity_curve,
            "crypto_stats": _calc_stats(crypto_trades, base_bal),
            "tradfi_stats": _calc_stats(tradfi_trades, base_bal),
            "by_asset_class": {
                "crypto": _calc_stats(crypto_trades, base_bal),
                "forex": _calc_stats(forex_trades, base_bal),
                "metals": _calc_stats(metals_trades, base_bal),
                "commodities": _calc_stats(commodities_trades, base_bal),
                "indices": _calc_stats(indices_trades, base_bal),
            }
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
