#!/usr/bin/env python3
"""Run real historical backtest for QuantEngine using MT5 candles."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from app.strategy.quant_engine import QuantEngine
from app.risk.engine import pip_size


def fetch_mt5_candles(symbol: str, count: int = 1500):
    if not mt5.initialize():
        print("[FAIL] Could not initialize MT5")
        return None, None
    
    rates_m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, count)
    rates_h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, count // 4)
    mt5.shutdown()

    if rates_m15 is None or rates_h1 is None:
        return None, None

    def convert(r):
        return [
            {
                "time": datetime.fromtimestamp(row["time"], tz=timezone.utc).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": int(row["tick_volume"]),
            }
            for row in r
        ]

    return convert(rates_m15), convert(rates_h1)


class ReplayFeed:
    def __init__(self, symbol, m15_slice, h1_slice, spread=0.00015):
        self.symbol = symbol
        self.m15 = m15_slice
        self.h1 = h1_slice
        self.spread = spread

    def get_candles(self, sym, tf, count):
        data = self.m15 if tf == "M15" else self.h1
        return data[-count:]

    def get_tick(self, sym):
        if not self.m15:
            return None
        last_c = self.m15[-1]["close"]
        return {"bid": last_c, "ask": round(last_c + self.spread, 5), "symbol": sym}

    def get_spread(self, sym):
        return self.spread


def main():
    import argparse
    parser = argparse.ArgumentParser(description="QuantEngine real market backtester")
    parser.add_argument("--symbol", default="EURUSD", help="Forex symbol (default: EURUSD)")
    parser.add_argument("--count", type=int, default=2000, help="Number of M15 candles (default: 2000)")
    parser.add_argument("--balance", type=float, default=30.0, help="Initial balance (default: 30.0)")
    args = parser.parse_args()

    symbol = args.symbol
    count = args.count
    initial_balance = args.balance

    print(f"============================================================")
    print(f"       Running QuantEngine Backtest on Real Market Data     ")
    print(f"       Symbol: {symbol} | Initial Balance: ${initial_balance:.2f} (Small Capital)")
    print(f"============================================================")

    m15_all, h1_all = fetch_mt5_candles(symbol, count=count)
    if not m15_all:
        print("[FAIL] Failed to fetch MT5 historical data")
        return 1

    print(f"[OK] Fetched {len(m15_all)} real M15 candles and {len(h1_all)} H1 candles from MT5")

    engine = QuantEngine(min_rr=1.8)
    initial_balance = 30.0
    balance = initial_balance
    lot_size = 0.01  # micro lot for $30 balance

    trades = []
    active_position = None

    # Step through historical data candle by candle (window lookback 100)
    for i in range(100, len(m15_all) - 1):
        m15_window = m15_all[:i]
        curr_candle = m15_all[i]
        next_candle = m15_all[i + 1]

        # Approximate corresponding H1 slice
        h1_idx = min(len(h1_all), i // 4)
        h1_window = h1_all[:max(30, h1_idx)]

        # 1. Manage active position if open
        if active_position:
            high = next_candle["high"]
            low = next_candle["low"]
            pos = active_position

            # Move to Breakeven at 1R profit
            if not pos.get("be_moved"):
                if pos["direction"] == "BUY" and high >= pos["entry"] + pos["risk_dist"]:
                    pos["sl"] = pos["entry"]
                    pos["be_moved"] = True
                elif pos["direction"] == "SELL" and low <= pos["entry"] - pos["risk_dist"]:
                    pos["sl"] = pos["entry"]
                    pos["be_moved"] = True

            pnl = 0.0
            closed = False
            exit_price = 0.0
            reason = ""

            if pos["direction"] == "BUY":
                if low <= pos["sl"]:
                    exit_price = pos["sl"]
                    pnl = (exit_price - pos["entry"]) * 100_000 * lot_size
                    closed = True
                    reason = "SL"
                elif high >= pos["tp"]:
                    exit_price = pos["tp"]
                    pnl = (exit_price - pos["entry"]) * 100_000 * lot_size
                    closed = True
                    reason = "TP"
            else:  # SELL
                if high >= pos["sl"]:
                    exit_price = pos["sl"]
                    pnl = (pos["entry"] - exit_price) * 100_000 * lot_size
                    closed = True
                    reason = "SL"
                elif low <= pos["tp"]:
                    exit_price = pos["tp"]
                    pnl = (pos["entry"] - exit_price) * 100_000 * lot_size
                    closed = True
                    reason = "TP"

            if closed:
                balance += pnl
                trades.append({
                    "direction": pos["direction"],
                    "entry": pos["entry"],
                    "exit": exit_price,
                    "pnl": round(pnl, 2),
                    "reason": reason,
                    "balance": round(balance, 2)
                })
                active_position = None
            continue

        # 2. Check for new setup if no position open
        feed = ReplayFeed(symbol, m15_window, h1_window)
        dec = engine.analyze_symbol(symbol, feed)

        if dec["decision"] in ("BUY", "SELL"):
            active_position = {
                "direction": dec["decision"],
                "entry": dec["entry"]["price"],
                "sl": dec["stop_loss"],
                "tp": dec["take_profit"][0],
                "risk_dist": abs(dec["entry"]["price"] - dec["stop_loss"]),
                "time": curr_candle["time"]
            }

    # Summarize results
    total_trades = len(trades)
    if total_trades == 0:
        print("\nNo trades triggered in this sample period (Conservative filter).")
        return 0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = (len(wins) / total_trades) * 100
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    net_pnl = gross_profit - gross_loss
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    print("-" * 60)
    print(f"Total Trades Taken : {total_trades}")
    print(f"Winning Trades     : {len(wins)} ({win_rate:.1f}%)")
    print(f"Losing Trades      : {len(losses)} ({100 - win_rate:.1f}%)")
    print(f"Gross Profit       : +${gross_profit:.2f}")
    print(f"Gross Loss         : -${gross_loss:.2f}")
    print(f"Net Profit / Loss  : {('+$' if net_pnl >= 0 else '-$')}{abs(net_pnl):.2f}")
    print(f"Profit Factor      : {profit_factor:.2f}")
    print(f"Final Balance      : ${balance:.2f} (from initial ${initial_balance:.2f})")
    print(f"Return on Capital  : {((balance - initial_balance) / initial_balance) * 100:.1f}%")
    print("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
