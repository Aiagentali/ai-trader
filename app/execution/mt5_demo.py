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
from app.risk.engine import contract_size

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

    def connect(self, max_wait_sec: int = 35) -> bool:
        """Initialize MT5 connection. Retries gracefully to allow user terminal startup/login."""
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        except ImportError:
            log.error("MetaTrader5 package not available — run on Windows")
            return False

        import time as _t
        deadline = _t.time() + max_wait_sec
        log.info(f"Connecting to MetaTrader 5 (allowing up to {max_wait_sec}s for terminal startup/login)...")

        while _t.time() < deadline:
            if mt5.terminal_info() is not None or mt5.initialize(timeout=5000):
                acc = mt5.account_info()
                if acc is not None:
                    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
                        log.error(f"Account is NOT demo (trade_mode={acc.trade_mode}). "
                                  "MT5DemoExecutor refuses live accounts.")
                        mt5.shutdown()
                        return False

                    log.info(f"MT5 Demo connected: login={acc.login} balance={acc.balance} server={acc.server}")
                    self._connected = True
                    return True
                else:
                    log.info("MT5 terminal detected; waiting for account login to complete...")
            _t.sleep(2)

        log.error(f"MT5 connect failed after {max_wait_sec}s. Ensure MT5 is running and logged in.")
        return False

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

        # Build request.
        # MT5 takes sl/tp as ABSOLUTE PRICE LEVELS for TRADE_ACTION_DEAL, not as
        # point distances — sending distances puts the stop far outside the chart
        # and every order ships without a real stop. Snap both to the symbol's tick
        # grid and refuse outright if either lands on the wrong side of the price.
        info = mt5.symbol_info(symbol)
        if info is None:
            log.error(f"no symbol info for {symbol} {request_id}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "no_symbol_info"}
        point = info.point
        digits = getattr(info, "digits", None)

        def snap(value):
            """Snap a price onto the symbol's tick grid, free of float drift."""
            if not value:
                return 0.0
            snapped = round(value / point) * point
            return round(snapped, digits) if digits is not None else snapped

        sl_level = snap(sl)
        tp_level = snap(tps[0]) if tps else 0.0

        if direction == "BUY":
            if sl and not sl_level < price:
                log.error(f"REFUSE: BUY SL {sl_level} not below {price} {request_id}")
                return {"request_id": request_id, "status": "REJECTED",
                        "reason": "sl_wrong_side"}
            if tp_level and not tp_level > price:
                log.error(f"REFUSE: BUY TP {tp_level} not above {price} {request_id}")
                return {"request_id": request_id, "status": "REJECTED",
                        "reason": "tp_wrong_side"}
        else:
            if sl and not sl_level > price:
                log.error(f"REFUSE: SELL SL {sl_level} not above {price} {request_id}")
                return {"request_id": request_id, "status": "REJECTED",
                        "reason": "sl_wrong_side"}
            if tp_level and not tp_level < price:
                log.error(f"REFUSE: SELL TP {tp_level} not below {price} {request_id}")
                return {"request_id": request_id, "status": "REJECTED",
                        "reason": "tp_wrong_side"}

        # Determine filling mode dynamically based on symbol / broker support
        fm = getattr(info, "filling_mode", 0)
        ioc = getattr(mt5, "ORDER_FILLING_IOC", 1)
        fok = getattr(mt5, "ORDER_FILLING_FOK", 0)
        ret = getattr(mt5, "ORDER_FILLING_RETURN", 2)

        if fm == 1:
            filling_type = fok
        elif fm == 2:
            filling_type = ioc
        elif fm == 3:
            filling_type = ioc
        else:
            filling_type = getattr(mt5, "ORDER_FILLING_IOC", ioc)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": sl_level,
            "tp": tp_level,
            "deviation": 20,
            "magic": 0,
            "comment": f"ai-trader:{request_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        log.info(f"[MT5 DEMO] {direction} {symbol} {lots} @ {price} "
                 f"SL={sl_level} TP={tp_level or 'none'} id={request_id}")

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
        # trade_id == position ticket: closes by ticket hit the exact trade row
        trade = {
            "trade_id": str(ticket), "opened_ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "direction": direction,
            "entry": result.price, "stop_loss": sl,
            "take_profit": str(tps), "position_size": lots,
            "risk_percent": decision.get("risk_percent", 0.5),
            "rr": decision.get("risk_reward", 0),
            "market_regime": decision.get("market_regime"),
            "setup": decision.get("setup"),
            "ai_confidence": decision.get("confidence"),
            "gemini_prob": decision.get("gemini_prob"),
            "entry_spread": (decision.get("market_context") or {}).get("spread"),
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
        sym_contract = contract_size(position["symbol"])
        deals_fn = getattr(mt5, "history_deals_get", None)
        deals = deals_fn(position=int(ticket)) if callable(deals_fn) else None
        if deals:
            realized_pnl = sum(d.profit + d.swap + d.commission for d in deals if d.entry in (1, 2))
            pnl = realized_pnl if realized_pnl != 0 else (
                ((price - entry) if position["direction"] == "BUY" else (entry - price)) * position["volume"] * sym_contract
            )
        else:
            pnl = ((price - entry) if position["direction"] == "BUY" else (entry - price)) * position["volume"] * sym_contract
        risk = abs(entry - sl) * position["volume"] * sym_contract if sl else 1
        r_mult = round(pnl / risk, 2) if risk > 0 else 0.0

        self.repo.close_position(str(ticket))
        self.repo.close_trade(str(ticket), price, pnl, r_mult, reason)

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

        # sl/tp are absolute price levels here too, not point distances
        digits = getattr(info, "digits", None)

        def snap(value):
            if not value:
                return 0.0
            snapped = round(value / point) * point
            return round(snapped, digits) if digits is not None else snapped

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(ticket),
            "sl": snap(new_sl),
            "tp": snap(new_tp),
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
                sym_contract = contract_size(pos["symbol"])
                deals_fn = getattr(mt5, "history_deals_get", None)
                deals = deals_fn(position=ticket) if callable(deals_fn) else None
                if deals:
                    realized_pnl = sum(d.profit + d.swap + d.commission for d in deals if d.entry in (1, 2))
                    pnl = realized_pnl if realized_pnl != 0 else (
                        ((exit_price - entry) if pos["direction"] == "BUY" else (entry - exit_price)) * pos["volume"] * sym_contract
                    )
                else:
                    pnl = ((exit_price - entry) if pos["direction"] == "BUY" else (entry - exit_price)) * pos["volume"] * sym_contract
                risk = abs(entry - sl) * pos["volume"] * sym_contract if sl else 1
                r_mult = round(pnl / risk, 2) if risk > 0 else 0.0

                self.repo.close_position(pos["ticket"])
                self.repo.close_trade(pos["ticket"], exit_price, pnl, r_mult,
                                      "mt5_sl_tp_hit")
                log.info(f"MT5 SL/TP hit: {pos['ticket']} @ {exit_price}")
