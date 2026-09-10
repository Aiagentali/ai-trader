"""MT5 Demo Executor — sends real demo orders on MetaTrader 5 (Windows only).

Safety gates:
- mode must be "demo" (not "live")
- live_trading_enabled must be False
- verify account is demo before sending
"""
import os
import uuid
import logging
from datetime import datetime, timezone

log = logging.getLogger("ai-trader.execution.mt5demo")

CONTRACT_SIZE = 100_000


class MT5DemoExecutor:
    """Sends real demo orders to MetaTrader 5."""

    def __init__(self, repo, feed, live_trading_enabled: bool = False):
        self.repo = repo
        self.feed = feed
        self.live_enabled = live_trading_enabled
        self._mt5 = None
        self._connected = False

        # Live trading MUST be disabled for demo executor
        if live_trading_enabled:
            raise RuntimeError("MT5DemoExecutor: live_trading_enabled=True — refused")

    def connect(self) -> bool:
        """Initialize MT5 connection. Returns True on success."""
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        except ImportError:
            log.error("MetaTrader5 package not available — run on Windows")
            return False

        if not mt5.initialize():
            log.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        acc = mt5.account_info()
        if acc is None:
            log.error("MT5 account_info failed")
            mt5.shutdown()
            return False

        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            log.error(f"Account is NOT demo (trade_mode={acc.trade_mode}). "
                      "MT5DemoExecutor refuses live accounts.")
            mt5.shutdown()
            return False

        log.info(f"MT5 Demo connected: login={acc.login} balance={acc.balance}")
        self._connected = True
        return True

    def disconnect(self):
        if self._mt5 and self._connected:
            self._mt5.shutdown()
            self._connected = False
            log.info("MT5 disconnected")

    def place_order(self, decision: dict) -> dict:
        """Send a real demo order to MT5."""
        request_id = str(uuid.uuid4())[:12]
        symbol = decision["symbol"]
        direction = decision["decision"]
        entry_info = decision.get("entry", {})
        requested_price = entry_info.get("price")
        sl = decision.get("stop_loss")
        tps = decision.get("take_profit", [])
        lots = decision.get("position_size", {}).get("requested_lots", 0.01)

        # ---- safety gates ----
        if direction in ("BUY", "SELL") and not sl:
            log.error(f"REFUSE: no stop_loss {request_id}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "no_stop_loss"}

        if not self._connected or not self._mt5:
            log.error(f"MT5 not connected {request_id}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "mt5_not_connected"}

        mt5 = self._mt5

        # Get current tick for market order
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error(f"no tick for {symbol}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "no_tick"}

        if direction == "BUY":
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        # Build request
        point = mt5.symbol_info(symbol).point
        sl_points = round(abs(price - sl) / point) if sl else 0
        tp_points = round(abs(tps[0] - price) / point) if tps else 0

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": sl_points,
            "tp": tp_points,
            "deviation": 20,
            "magic": 0,
            "comment": f"ai-trader:{request_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        log.info(f"[MT5 DEMO] {direction} {symbol} {lots} @ {price} "
                 f"SL={sl} TP={tps[0] if tps else 'none'} id={request_id}")

        result = mt5.order_send(req)
        if result is None:
            log.error(f"order_send returned None: {mt5.last_error()}")
            return {"request_id": request_id, "status": "ERROR",
                    "reason": "order_send_failed"}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"order rejected: {result.retcode} {result.comment}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": f"mt5_error_{result.retcode}",
                    "comment": result.comment}

        ticket = result.order

        # Store position
        pos = {"ticket": str(ticket), "symbol": symbol, "direction": direction,
               "volume": lots, "entry_price": result.price,
               "stop_loss": sl, "take_profit": tps[0] if tps else None}
        self.repo.save_position(pos)

        # Store trade
        trade = {
            "trade_id": request_id, "opened_ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "direction": direction,
            "entry": result.price, "stop_loss": sl,
            "take_profit": str(tps), "position_size": lots,
            "risk_percent": decision.get("risk_percent", 0.5),
            "rr": decision.get("risk_reward", 0),
            "market_regime": decision.get("market_regime"),
            "setup": decision.get("setup"),
            "ai_confidence": decision.get("confidence"),
            "thesis": decision.get("thesis"),
            "invalidation": decision.get("invalidation"),
            "status": "open", "request_id": request_id,
        }
        self.repo.save_trade(trade)

        log.info(f"MT5 order OK: ticket={ticket} price={result.price}")
        return {"request_id": request_id, "status": "EXECUTED_MT5_DEMO",
                "ticket": str(ticket), "entry": result.price,
                "sl": sl, "tp": tps[0] if tps else None, "lots": lots}

    def close_position(self, ticket: str, reason: str = "manual") -> dict:
        """Close an open position on MT5 demo."""
        if not self._connected or not self._mt5:
            return {"status": "ERROR", "reason": "mt5_not_connected"}

        mt5 = self._mt5
        pos = self.repo.open_positions()
        position = next((p for p in pos if p["ticket"] == str(ticket)), None)
        if position is None:
            return {"status": "ERROR", "reason": "position_not_found"}

        symbol = position["symbol"]
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"status": "ERROR", "reason": "tick_unavailable"}

        if position["direction"] == "BUY":
            price = tick.bid
            close_type = mt5.ORDER_TYPE_SELL
        else:
            price = tick.ask
            close_type = mt5.ORDER_TYPE_BUY

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(position["volume"]),
            "type": close_type,
            "position": int(ticket),
            "price": price,
            "deviation": 20,
            "magic": 0,
            "comment": f"ai-trader:close:{reason}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(req)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else "unknown"
            log.error(f"close failed: {err}")
            return {"status": "ERROR", "reason": f"mt5_close_failed: {err}"}

        # Calculate PnL
        entry = position["entry_price"]
        sl = position.get("stop_loss")
        if position["direction"] == "BUY":
            pnl = (price - entry) * position["volume"] * CONTRACT_SIZE
        else:
            pnl = (entry - price) * position["volume"] * CONTRACT_SIZE
        risk = abs(entry - sl) * position["volume"] * CONTRACT_SIZE if sl else 1
        r_mult = round(pnl / risk, 2) if risk > 0 else 0.0

        self.repo.close_position(str(ticket))
        self.repo.close_trade(ticket, price, pnl, r_mult, reason)

        log.info(f"MT5 close OK: ticket={ticket} price={price} PnL={pnl:.2f}")
        return {"status": "CLOSED_MT5_DEMO", "exit_price": price,
                "pnl": round(pnl, 2), "r_mult": r_mult}

    def modify_position(self, ticket: str, new_sl=None, new_tp=None) -> dict:
        """Modify SL/TP on an open MT5 position."""
        if not self._connected or not self._mt5:
            return {"status": "ERROR", "reason": "mt5_not_connected"}

        mt5 = self._mt5
        pos = self.repo.open_positions()
        position = next((p for p in pos if p["ticket"] == str(ticket)), None)
        if position is None:
            return {"status": "ERROR", "reason": "position_not_found"}

        symbol = position["symbol"]
        info = mt5.symbol_info(symbol)
        point = info.point if info else 0.0001

        sl_pts = round(new_sl / point) if new_sl else 0
        tp_pts = round(new_tp / point) if new_tp else 0

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(ticket),
            "sl": sl_pts,
            "tp": tp_pts,
        }

        result = mt5.order_send(req)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else "unknown"
            log.error(f"modify failed: {err}")
            return {"status": "ERROR", "reason": f"mt5_modify_failed: {err}"}

        self.repo.update_position(str(ticket), stop_loss=new_sl, take_profit=new_tp)
        log.info(f"MT5 modify OK: ticket={ticket} SL={new_sl} TP={new_tp}")
        return {"status": "MODIFIED_MT5_DEMO", "ticket": str(ticket),
                "sl": new_sl, "tp": new_tp}

    def check_sl_tp(self):
        """Check open positions for SL/TP hits via MT5 position status."""
        if not self._connected or not self._mt5:
            return

        mt5 = self._mt5
        open_pos = self.repo.open_positions()
        for pos in open_pos:
            # Check if position still exists on MT5
            ticket = int(pos["ticket"])
            mt5_pos = mt5.positions_get(ticket=ticket)
            if mt5_pos is None or len(mt5_pos) == 0:
                # Position closed by SL/TP on MT5 side
                tick = mt5.symbol_info_tick(pos["symbol"])
                exit_price = tick.bid if pos["direction"] == "BUY" else tick.ask
                entry = pos["entry_price"]
                sl = pos.get("stop_loss")
                if pos["direction"] == "BUY":
                    pnl = (exit_price - entry) * pos["volume"] * CONTRACT_SIZE
                else:
                    pnl = (entry - exit_price) * pos["volume"] * CONTRACT_SIZE
                risk = abs(entry - sl) * pos["volume"] * CONTRACT_SIZE if sl else 1
                r_mult = round(pnl / risk, 2) if risk > 0 else 0.0

                self.repo.close_position(pos["ticket"])
                self.repo.close_trade(pos["ticket"], exit_price, pnl, r_mult,
                                      "mt5_sl_tp_hit")
                log.info(f"MT5 SL/TP hit: {pos['ticket']} @ {exit_price}")
