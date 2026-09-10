"""Trade management — MODIFY, PARTIAL_CLOSE, CLOSE handled by the agent loop.

This module provides the execution layer for position management decisions.
The agent loop calls these when AI decides HOLD/MODIFY/PARTIAL_CLOSE/CLOSE.
"""
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger("ai-trader.trade_manager")


class TradeManager:
    """Handles position lifecycle management (modify, partial close, close)."""

    def __init__(self, repo, feed, execution):
        self.repo = repo
        self.feed = feed
        self.execution = execution

    def modify_sl_tp(self, ticket: str, new_sl=None, new_tp=None) -> dict:
        """Modify SL and/or TP on an open position."""
        pos = self.repo.open_positions()
        position = next((p for p in pos if p["ticket"] == str(ticket)), None)
        if position is None:
            return {"status": "ERROR", "reason": "position_not_found"}

        # For MT5 demo, delegate to real modify
        if hasattr(self.execution, 'modify_position'):
            result = self.execution.modify_position(ticket, new_sl=new_sl, new_tp=new_tp)
            return result

        # Simulation: just update DB
        self.repo.update_position(str(ticket), stop_loss=new_sl, take_profit=new_tp)
        log.info(f"[SIM] MODIFY ticket={ticket} SL={new_sl} TP={new_tp}")
        return {"status": "MODIFIED_SIMULATED", "ticket": str(ticket),
                "sl": new_sl, "tp": new_tp}

    def partial_close(self, ticket: str, close_fraction: float = 0.5) -> dict:
        """Close a fraction of the open position."""
        pos = self.repo.open_positions()
        position = next((p for p in pos if p["ticket"] == str(ticket)), None)
        if position is None:
            return {"status": "ERROR", "reason": "position_not_found"}

        symbol = position["symbol"]
        tick = self.feed.get_tick(symbol)
        if tick is None:
            return {"status": "ERROR", "reason": "tick_unavailable"}

        volume = position["volume"]
        close_vol = round(volume * close_fraction, 2)
        if close_vol <= 0:
            return {"status": "ERROR", "reason": "close_volume_zero"}

        # For MT5 demo, close partial on MT5
        if hasattr(self.execution, '_mt5') and self.execution._connected:
            mt5 = self.execution._mt5
            if position["direction"] == "BUY":
                price = tick["bid"]
                close_type = mt5.ORDER_TYPE_SELL
            else:
                price = tick["ask"]
                close_type = mt5.ORDER_TYPE_BUY

            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": close_vol,
                "type": close_type,
                "position": int(ticket),
                "price": price,
                "deviation": 20,
                "magic": 0,
                "comment": "ai-trader:partial_close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(req)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else "unknown"
                return {"status": "ERROR", "reason": f"partial_close_failed: {err}"}

        # Simulation: just log
        exit_price = tick["bid"] if position["direction"] == "BUY" else tick["ask"]
        entry = position["entry_price"]
        if position["direction"] == "BUY":
            pnl = (exit_price - entry) * close_vol * 100_000
        else:
            pnl = (entry - exit_price) * close_vol * 100_000

        remaining = round(volume - close_vol, 2)

        # Update position volume
        self.repo.update_position(str(ticket), stop_loss=position.get("stop_loss"))
        # Log partial close as a separate trade record
        partial_trade = {
            "trade_id": f"PARTIAL-{str(uuid.uuid4())[:8]}",
            "opened_ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "direction": position["direction"],
            "entry": exit_price, "stop_loss": position.get("stop_loss", 0),
            "take_profit": "[]", "position_size": close_vol,
            "risk_percent": 0, "rr": 0,
            "market_regime": "", "setup": "partial_close",
            "ai_confidence": 0, "thesis": "partial close",
            "invalidation": "", "status": "closed",
            "request_id": "partial",
        }
        self.repo.save_trade(partial_trade)
        self.repo.close_trade(partial_trade["trade_id"], exit_price, pnl, 0,
                              "partial_close")

        log.info(f"[SIM] PARTIAL_CLOSE {ticket}: {close_vol}/{volume} @ {exit_price} "
                 f"PnL={pnl:.2f}")

        return {"status": "PARTIAL_CLOSED_SIMULATED", "ticket": str(ticket),
                "closed_volume": close_vol, "remaining_volume": remaining,
                "exit_price": exit_price, "pnl": round(pnl, 2)}

    def close_position(self, ticket: str, reason: str = "manual") -> dict:
        """Close an entire position."""
        return self.execution.close_position(ticket, reason=reason)

    def manage_position(self, decision: dict, ticket: str) -> dict:
        """Route a HOLD/MODIFY/PARTIAL_CLOSE/CLOSE decision to the right handler."""
        d = decision.get("decision", "HOLD")

        if d == "HOLD":
            log.info(f"HOLD {ticket} — no action")
            return {"status": "HOLD", "ticket": ticket}

        elif d == "MODIFY":
            new_sl = decision.get("modified_sl") or decision.get("stop_loss")
            new_tp = decision.get("modified_tp") or (
                decision["take_profit"][0] if decision.get("take_profit") else None)
            return self.modify_sl_tp(ticket, new_sl=new_sl, new_tp=new_tp)

        elif d == "PARTIAL_CLOSE":
            fraction = decision.get("close_fraction", 0.5)
            return self.partial_close(ticket, close_fraction=fraction)

        elif d == "CLOSE":
            return self.close_position(ticket, reason="ai_close")

        return {"status": "ERROR", "reason": f"unknown decision: {d}"}
