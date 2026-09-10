"""Backtesting engine — historical replay with no look-ahead bias.

Plays through candles chronologically, feeds them to the same agent
pipeline, and collects trade results.
"""
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("ai-trader.backtest")


class HistoricalCandleFeed:
    """Feed that replays historical data candle-by-candle.

    No future data is ever available. The AI only sees what would have
    been available at that exact historical timestamp.
    """

    def __init__(self, symbol: str, candle_data: dict):
        """
        candle_data: {
            "D1": [candle, ...],   # oldest first
            "H4": [...],
            "H1": [...],
            "M15": [...],
            "M5": [...],
        }
        Each candle: {"time": ISO str, "open": float, "high": float,
                      "low": float, "close": float, "tick_volume": int}
        """
        self.symbol = symbol
        self.data = candle_data
        self._current_idx = {tf: 0 for tf in candle_data}
        self._timeframes = list(candle_data.keys())
        self.spread = 0.00012  # fixed spread for backtest

    def _find_idx_at_time(self, tf: str, target_time: datetime) -> int:
        """Binary search: latest candle where candle.time <= target_time."""
        candles = self.data.get(tf, [])
        lo, hi = 0, len(candles) - 1
        result = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            try:
                ct = datetime.fromisoformat(candles[mid]["time"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                lo = mid + 1
                continue
            if ct <= target_time:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    def set_current_time(self, time: datetime):
        """Set the 'current' time for the backtest — only data up to this point is visible."""
        for tf in self._timeframes:
            idx = self._find_idx_at_time(tf, time)
            self._current_idx[tf] = max(0, idx)

    def get_candles(self, symbol: str, timeframe: str, count: int):
        """Return up to `count` candles ending at current time."""
        if timeframe not in self._current_idx:
            return []
        idx = self._current_idx[timeframe]
        candles = self.data.get(timeframe, [])
        start = max(0, idx - count + 1)
        end = idx + 1
        return candles[start:end]

    def get_tick(self, symbol: str) -> dict:
        """Return the latest candle's close as the tick."""
        for tf in ["M5", "M15", "H1"]:
            candles = self.data.get(tf, [])
            idx = self._current_idx.get(tf, 0)
            if idx < len(candles):
                c = candles[idx]
                return {"symbol": symbol, "bid": c["close"],
                        "ask": round(c["close"] + self.spread, 5),
                        "time": c["time"]}
        return None

    def get_spread(self, symbol: str) -> float:
        return self.spread

    def get_account_info(self) -> dict:
        return {"login": 0, "balance": 10000.0, "equity": 10000.0,
                "currency": "USD", "leverage": 100, "margin_free": 10000.0}

    def connected(self) -> bool:
        return True


class BacktestEngine:
    """Replay historical data through the agent pipeline."""

    def __init__(self, candle_data: dict, symbol: str = "EURUSD"):
        """
        candle_data: {"D1": [...], "H4": [...], "H1": [...], "M15": [...]}
        Must be sorted oldest-first within each timeframe.
        """
        self.symbol = symbol
        self.candle_data = candle_data
        self._timeframes = sorted(candle_data.keys())
        self.trades = []
        self.equity_curve = []

    def _get_simulation_time(self, candle_data_timeframes: dict) -> list:
        """Extract all unique timestamps across all timeframes, sorted."""
        all_times = set()
        for tf, candles in candle_data_timeframes.items():
            for c in candles:
                try:
                    t = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
                    all_times.add(t)
                except (ValueError, TypeError):
                    continue
        return sorted(all_times)

    def run(self, risk_engine=None, decision_fn=None) -> dict:
        """
        Run the backtest.

        decision_fn: callable(candles_by_tf, current_time) -> dict or None
            Must return a decision dict (like AI would), or None for WAIT.
            Must NOT have access to future data.
        """
        from app.market.snapshot import build_snapshot
        from app.market.indicators import sma, ema, rsi, atr, adx, swing_levels

        sim_times = self._get_simulation_time(self.candle_data)
        if not sim_times:
            return {"error": "no data"}

        balance = 10000.0
        equity = 10000.0
        open_positions = []
        results = []
        trades_log = []

        # Rolling close list for indicators — use first available timeframe
        primary_tf = self._timeframes[0] if self._timeframes else "H1"
        primary_closes = []

        for t in sim_times:
            # Build feed snapshot up to current time
            feed = HistoricalCandleFeed(self.symbol, self.candle_data)
            feed.set_current_time(t)

            # Get current M15 candle index for simulating position PnL
            primary_candles = feed.get_candles(self.symbol, primary_tf, 200)
            primary_closes.extend([c["close"] for c in primary_candles[-1:]])

            tick = feed.get_tick(self.symbol)
            if tick is None:
                continue

            # ---- Check SL/TP for open positions ----
            closed_this_tick = []
            for pos in open_positions:
                if pos["direction"] == "BUY":
                    if tick["bid"] <= pos["stop_loss"]:
                        pnl = (pos["stop_loss"] - pos["entry"]) * pos["volume"] * 100_000
                        pos["pnl"] = pnl
                        pos["exit_price"] = pos["stop_loss"]
                        pos["exit_reason"] = "stop_loss_hit"
                        pos["exit_time"] = t.isoformat()
                        closed_this_tick.append(pos)
                    elif tick["bid"] >= pos.get("take_profit", 999):
                        pnl = (pos["take_profit"] - pos["entry"]) * pos["volume"] * 100_000
                        pos["pnl"] = pnl
                        pos["exit_price"] = pos["take_profit"]
                        pos["exit_reason"] = "take_profit_hit"
                        pos["exit_time"] = t.isoformat()
                        closed_this_tick.append(pos)
                else:
                    if tick["ask"] >= pos["stop_loss"]:
                        pnl = (pos["entry"] - pos["stop_loss"]) * pos["volume"] * 100_000
                        pos["pnl"] = pnl
                        pos["exit_price"] = pos["stop_loss"]
                        pos["exit_reason"] = "stop_loss_hit"
                        pos["exit_time"] = t.isoformat()
                        closed_this_tick.append(pos)
                    elif tick["ask"] <= pos.get("take_profit", 0):
                        pnl = (pos["entry"] - pos["take_profit"]) * pos["volume"] * 100_000
                        pos["pnl"] = pnl
                        pos["exit_price"] = pos["take_profit"]
                        pos["exit_reason"] = "take_profit_hit"
                        pos["exit_time"] = t.isoformat()
                        closed_this_tick.append(pos)

            for pos in closed_this_tick:
                open_positions.remove(pos)
                equity += pos["pnl"]
                trades_log.append(pos)

            # ---- Call decision function ----
            if decision_fn:
                decision = decision_fn(primary_closes[-200:], t, tick, open_positions)
            else:
                decision = None

            # ---- Execute decision ----
            if decision and decision.get("decision") in ("BUY", "SELL"):
                d = decision["decision"]
                entry_price = tick["ask"] if d == "BUY" else tick["bid"]
                sl = decision.get("stop_loss", 0)
                tps = decision.get("take_profit", [])
                tp = tps[0] if tps else None

                # Risk check
                if risk_engine and sl:
                    risk_dist = abs(entry_price - sl)
                    if risk_dist <= 0:
                        continue
                    risk_amount = balance * 0.005
                    per_lot = risk_dist * 100_000
                    lots = int(risk_amount / per_lot * 100) / 100
                    if lots < 0.01:
                        continue
                else:
                    lots = 0.1

                pos = {
                    "direction": d, "entry": entry_price,
                    "stop_loss": sl, "take_profit": tp,
                    "volume": lots, "open_time": t.isoformat(),
                    "pnl": 0, "exit_price": None, "exit_reason": None,
                }
                open_positions.append(pos)
                balance -= lots * entry_price * 0.01  # margin approx

            results.append({"time": t.isoformat(), "equity": round(equity, 2),
                            "balance": round(balance, 2),
                            "open_positions": len(open_positions)})

            # Record equity curve point
            self.equity_curve.append({"time": t.isoformat(), "equity": round(equity, 2)})

        # Close remaining positions at last tick
        if open_positions and primary_closes:
            last_close = primary_closes[-1]
            for pos in open_positions:
                if pos["direction"] == "BUY":
                    pnl = (last_close - pos["entry"]) * pos["volume"] * 100_000
                else:
                    pnl = (pos["entry"] - last_close) * pos["volume"] * 100_000
                pos["pnl"] = pnl
                pos["exit_price"] = last_close
                pos["exit_reason"] = "backtest_end"
                pos["exit_time"] = sim_times[-1].isoformat()
                trades_log.append(pos)

        self.trades = trades_log
        return self._compute_stats(trades_log, results)

    def _compute_stats(self, trades: list, equity_curve: list) -> dict:
        if not trades:
            return {"total_trades": 0, "message": "no trades executed"}

        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max drawdown from equity curve
        peak = 0
        max_dd = 0
        for pt in equity_curve:
            eq = pt["equity"]
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "profit_factor": round(profit_factor, 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "max_drawdown": round(max_dd, 2),
            "sl_exits": len([t for t in trades if t.get("exit_reason") == "stop_loss_hit"]),
            "tp_exits": len([t for t in trades if t.get("exit_reason") == "take_profit_hit"]),
        }

    def get_trades(self) -> list:
        return self.trades

    def get_equity_curve(self) -> list:
        return self.equity_curve
