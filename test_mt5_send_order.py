#!/usr/bin/env python3
"""Send a single tiny test demo order (0.01 lot) to MT5 to visually verify on screen."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from app.database.repository import Repository
from app.execution.mt5_demo import MT5DemoExecutor


def main():
    print("=" * 60)
    print("      Testing Live Order Placement on MetaTrader 5 Demo     ")
    print("=" * 60)

    repo = Repository()
    executor = MT5DemoExecutor(repo, feed=None, live_trading_enabled=False)

    if not executor.connect():
        print("[FAIL] Could not connect to MT5 terminal")
        return 1

    symbol = "EURUSD"
    tick = executor._mt5.symbol_info_tick(symbol)
    if not tick:
        print(f"[FAIL] No tick for {symbol}")
        return 1

    price = tick.ask
    # 15 pip SL and 27 pip TP (RR = 1.8)
    sl = round(price - 0.0015, 5)
    tp = round(price + 0.0027, 5)

    decision = {
        "symbol": symbol,
        "decision": "BUY",
        "entry": {"price": price},
        "stop_loss": sl,
        "take_profit": [tp],
        "position_size": {"requested_lots": 0.01},
        "confidence": 85,
        "risk_reward": 1.8,
        "thesis": "Live manual test to verify MT5 order execution and visual lines",
        "setup": "LIVE_VERIFICATION_TEST",
        "invalidation": "Stop loss hit"
    }

    print(f"\nSending 0.01 lot BUY order on {symbol} @ {price}...")
    print(f"SL: {sl} | TP: {tp}")

    result = executor.place_order(decision)
    print("\nResult from MT5 Execution Engine:")
    print(result)

    if result.get("status") in ("EXECUTED_DEMO",) or result.get("ticket"):
        print("\n[SUCCESS] Order placed in MT5! Check your MetaTrader 5 window now!")
        print(f"Look at the Trade tab for ticket #{result.get('ticket') or result.get('order')}")
    else:
        print(f"\nStatus: {result.get('status')} | Reason: {result.get('reason')}")
        if "retcode" in str(result) or "Algo" in str(result):
            print("Tip: Make sure the 'Algo Trading' button in the top toolbar of MT5 is GREEN.")

    executor.disconnect()
    repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
