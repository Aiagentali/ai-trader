"""Execution engine — simulation mode (V1 default). No real MT5 orders sent."""
import uuid
import logging
from datetime import datetime, timezone

log = logging.getLogger("ai-trader.execution")


class ExecutionEngine:
    """
    In simulation mode, orders are logged but never sent to MT5.
    Later: MT5DemoExecutionEngine sends real demo orders.
    """

    def __init__(self, repo, feed, execution_mode: str = "simulation",
                 live_trading_enabled: bool = False):
        self.repo = repo
        self.feed = feed
        self.mode = execution_mode
        self.live_enabled = live_trading_enabled

    def place_order(self, decision: dict) -> dict:
        """Returns execution result with unique request_id."""
        request_id = str(uuid.uuid4())[:12]
        symbol = decision["symbol"]
        direction = decision["decision"]
        entry_info = decision.get("entry", {})
        entry_price = entry_info.get("price") or self.feed.get_tick(symbol)["bid"]
        sl = decision.get("stop_loss")
        tps = decision.get("take_profit", [])
        lots = decision.get("position_size", {}).get("requested_lots", 0.01)

        # ---- safety gate ----
        if direction in ("BUY", "SELL") and not sl:
            log.error(f"REFUSE: no stop_loss in decision {request_id}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "no_stop_loss"}
        if self.mode == "live" and not self.live_enabled:
            log.error(f"REFUSE: live trading disabled {request_id}")
            return {"request_id": request_id, "status": "REJECTED",
                    "reason": "live_trading_disabled"}

        if self.mode == "simulation":
            return self._sim_order(request_id, symbol, direction, entry_price,
                                   sl, tps, lots, decision)

        if self.mode == "demo":
            return self._sim_order(request_id, symbol, direction, entry_price,
                                   sl, tps, lots, decision)

        raise ValueError(f"unknown execution mode: {self.mode}")

    def _sim_order(self, rid, symbol, direction, entry, sl, tps, lots, decision):
        # Persisted positions survive process restarts; counters do not.
        ticket = f"SIM-{uuid.uuid4()}"
        log.info(f"[SIM] {direction} {symbol} {lots} @ {entry} SL={sl} TP={tps} id={rid}")

        pos = {"ticket": ticket, "symbol": symbol, "direction": direction,
               "volume": lots, "entry_price": entry,
               "stop_loss": sl, "take_profit": tps[0] if tps else None}
        self.repo.save_position(pos)

        # trade_id == position ticket: closes by ticket hit the exact trade row
        trade = {
            "trade_id": ticket, "opened_ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "direction": direction,
            "entry": entry, "stop_loss": sl,
            "take_profit": str(tps),
            "position_size": lots,
            "risk_percent": decision.get("risk_percent", 0.5),
            "rr": decision.get("risk_reward", 0),
            "market_regime": decision.get("market_regime"),
            "setup": decision.get("setup"),
            "ai_confidence": decision.get("confidence"),
            "thesis": decision.get("thesis"),
            "invalidation": decision.get("invalidation"),
            "status": "open",
            "request_id": rid,
        }
        self.repo.save_trade(trade)

        return {"request_id": rid, "status": "EXECUTED_SIMULATED",
                "ticket": ticket, "entry": entry, "sl": sl,
                "tp": tps[0] if tps else None, "lots": lots}

    def close_position(self, ticket: str, reason: str = "manual") -> dict:
        """Simulate close at current tick price."""
        tick = None
        rows = self.repo.open_positions()
        pos = next((r for r in rows if r["ticket"] == ticket), None)
        if pos is None:
            return {"status": "ERROR", "reason": "position_not_found"}
        tick = self.feed.get_tick(pos["symbol"])
        if tick is None:
            return {"status": "ERROR", "reason": "tick_unavailable"}

        exit_price = tick["bid"] if pos["direction"] == "BUY" else tick["ask"]
        sl = pos["stop_loss"]
        entry = pos["entry_price"]
        if pos["direction"] == "BUY":
            pnl = (exit_price - entry) * pos["volume"] * 100_000
        else:
            pnl = (entry - exit_price) * pos["volume"] * 100_000
        risk = abs(entry - sl) * pos["volume"] * 100_000 if sl else 1
        r_mult = round(pnl / risk, 2) if risk > 0 else 0.0

        # ticket == trade_id: exact close, no fuzzy reconciliation
        self.repo.close_position(ticket)
        matched = self.repo.close_trade(ticket, exit_price, pnl, r_mult, reason)
        log.info(f"[SIM] CLOSE {ticket} @ {exit_price} PnL={pnl:.2f} R={r_mult} "
                 f"trade_id={matched}")
        return {"status": "CLOSED_SIMULATED", "trade_id": matched,
                "exit_price": exit_price,
                "pnl": round(pnl, 2), "r_mult": r_mult}

    def check_sl_tp(self):
        """Check open positions against live ticks (SL/TP hit detection)."""
        open_pos = self.repo.open_positions()
        if not open_pos:
            return
        for pos in open_pos:
            tick = self.feed.get_tick(pos["symbol"])
            if tick is None:
                continue
            bid, ask = tick["bid"], tick["ask"]
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            hit_reason = None
            if pos["direction"] == "BUY":
                if sl and bid <= sl:
                    hit_reason = "stop_loss_hit"
                elif tp and bid >= tp:
                    hit_reason = "take_profit_hit"
            else:
                if sl and ask >= sl:
                    hit_reason = "stop_loss_hit"
                elif tp and ask <= tp:
                    hit_reason = "take_profit_hit"
            if hit_reason:
                log.info(f"SL/TP HIT: {pos['ticket']} {hit_reason}")
                self.close_position(pos["ticket"], reason=hit_reason)
