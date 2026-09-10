"""MT5-native execution mode — the terminal itself places orders.

When settings.yaml has execution.mode = "mt5", the whole pipeline runs
against the real MT5 terminal (which must be open & logged into a DEMO
account on the same Windows machine):

- feed: real MT5 candles/ticks (MT5Feed)
- execution: real MT5 demo orders (MT5DemoExecutor) — orders appear in
  the MT5 terminal's Trade tab exactly like manual trades
- Algo-trading button in the terminal must be ENABLED for order_send
  to succeed

Safety (unchanged, constitution #13):
- live_trading_enabled=false is enforced
- account must be ACCOUNT_TRADE_MODE_DEMO
"""
import logging

log = logging.getLogger("ai-trader.mt5native")


def build_mt5_stack(repo, feed_kind_unused=None):
    """Create MT5DemoExecutor and verify the terminal connection.

    Returns (executor, feed) or raises RuntimeError with a clear message.
    """
    from app.execution.mt5_demo import MT5DemoExecutor

    executor = MT5DemoExecutor(repo, feed=None, live_trading_enabled=False)
    if not executor.connect():
        raise RuntimeError(
            "MT5 connection failed. Check:\n"
            "  1. MetaTrader 5 terminal is OPEN and logged into a DEMO account\n"
            "  2. 'pip install MetaTrader5' was run on Windows\n"
            "  3. Tools > Options > Expert Advisors > 'Allow algorithmic trading' is ON"
        )
    # MT5DemoExecutor has its own check_sl_tp using MT5 position state.
    # For the data feed, reuse the same MT5 instance via MT5Feed.
    from app.market.mt5 import MT5Feed
    feed = MT5Feed()
    log.info("MT5 native stack ready (terminal-connected, demo-verified)")
    return executor, feed
