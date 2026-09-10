"""Agent state machine — persistent autonomous loop."""
import logging
import time
from enum import Enum

log = logging.getLogger("ai-trader.agent")


class AgentState(Enum):
    OBSERVING = "OBSERVING"
    ANALYZING = "ANALYZING"
    WAITING = "WAITING"
    WATCHING_SETUP = "WATCHING_SETUP"
    TRADE_READY = "TRADE_READY"
    EXECUTING = "EXECUTING"
    POSITION_OPEN = "POSITION_OPEN"
    MANAGING_POSITION = "MANAGING_POSITION"
    EXITING = "EXITING"
    REVIEWING = "REVIEWING"
    ERROR_RECOVERY = "ERROR_RECOVERY"


class Agent:
    """
    Core autonomous agent loop.
    States: OBSERVE → ANALYZE → WAIT / WATCH / TRADE → MANAGE → REVIEW → OBSERVE
    """

    def __init__(self, feed, repo, ai_client, budget, risk_engine, execution,
                 symbols, settings):
        self.feed = feed
        self.repo = repo
        self.ai = ai_client
        self.budget = budget
        self.risk = risk_engine
        self.execution = execution
        self.symbols = symbols
        self.settings = settings
        self.state = AgentState.OBSERVING
        self.running = False
        self._watch_queue = {}  # symbol -> next_check_timestamp

    def run_forever(self, initial_interval_sec: int = 10):
        """Main persistent loop. Runs until process killed."""
        import signal, sys
        self.running = True

        def _stop(*_):
            self.running = False
            log.info("shutdown signal received")
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        log.info(f"agent starting | state={self.state.value} | symbols={self.symbols}")
        interval = initial_interval_sec
        while self.running:
            try:
                self._tick()
            except Exception as exc:
                log.exception(f"tick error: {exc}")
                self.state = AgentState.ERROR_RECOVERY
                time.sleep(30)
                if self.feed.connected():
                    self.state = AgentState.OBSERVING
                continue
            time.sleep(interval)

        log.info("agent stopped")

    def _tick(self):
        # --- health gate ---
        if not self.feed.connected():
            self.state = AgentState.ERROR_RECOVERY
            log.warning("feed disconnected — no new trades")
            return

        # --- SL/TP hit check first ---
        self.execution.check_sl_tp()

        open_positions = self.repo.open_positions()
        open_symbols = {p["symbol"] for p in open_positions}

        now = time.time()

        for sym in self.symbols:
            # --- watch queue ---
            if sym in self._watch_queue:
                if now < self._watch_queue[sym]:
                    continue
                del self._watch_queue[sym]

            # --- decide analysis level ---
            if open_symbols and sym in open_symbols:
                level = "CRITICAL"
            elif self.repo.latest_thesis(sym):
                last_thesis = self.repo.latest_thesis(sym)
                bias = last_thesis.get("bias", "")
                if bias == "WATCHING_SETUP":
                    level = "WATCH"
                else:
                    level = "NORMAL"
            else:
                level = "NORMAL"

            # --- token gate ---
            if not self.budget.check_and_allow(f"analysis_{sym}", level=level):
                log.warning(f"token budget exhausted — skipping {sym}")
                continue

            # --- observe → analyze → decide ---
            self.state = AgentState.ANALYZING
            decision = self._analyze_symbol(sym, level)

            if decision is None:
                continue

            d = decision.get("decision", "WAIT")
            log.info(f"{sym} | state={self.state.value} | decision={d} | "
                     f"confidence={decision.get('confidence')}")

            # --- apply decision ---
            if d == "WAIT":
                self.state = AgentState.WAITING

            elif d == "WATCH":
                self.state = AgentState.WATCHING_SETUP
                interval = self.settings.get("analysis", {}).get(
                    "watch_interval_minutes", 10) * 60
                self._watch_queue[sym] = now + interval
                self.repo.save_thesis(sym, "WATCHING_SETUP", decision.get("market_regime", ""),
                                      decision.get("thesis", ""),
                                      decision.get("invalidation", ""), decision)

            elif d in ("BUY", "SELL"):
                account = self.feed.get_account_info()
                risk_result = self.risk.validate(decision, account)
                self.repo.save_decision(
                    request_id=f"decision_{sym}_{int(now)}",
                    symbol=sym, decision=d, approved=risk_result.approved,
                    reject_reasons=risk_result.reasons_reject, payload=decision,
                    versions=self.settings.get("versions", {}))
                if risk_result.approved:
                    self.state = AgentState.EXECUTING
                    res = self.execution.place_order(decision)
                    log.info(f"execution result: {res}")
                    if res.get("status") in ("EXECUTED_SIMULATED",):
                        self.state = AgentState.POSITION_OPEN
                else:
                    log.info(f"risk REJECTED: {risk_result.reasons_reject}")
                    self.state = AgentState.WAITING
                    self.repo.save_thesis(sym, "WAIT", decision.get("market_regime", ""),
                                          f"REJECTED: {risk_result.reasons_reject}",
                                          decision.get("invalidation", ""), decision)

            elif d in ("MODIFY", "PARTIAL_CLOSE", "CLOSE"):
                if open_positions:
                    pos = next((p for p in open_positions if p["symbol"] == sym), None)
                    if pos:
                        self.state = AgentState.MANAGING_POSITION
                        from app.execution.trade_manager import TradeManager
                        tm = TradeManager(self.repo, self.feed, self.execution)
                        res = tm.manage_position(decision, pos["ticket"])
                        log.info(f"trade management: {res}")
                        if res.get("status") in ("CLOSED_SIMULATED", "CLOSED_MT5_DEMO",
                                                  "PARTIAL_CLOSED_SIMULATED"):
                            self.state = AgentState.REVIEWING
                            self._review_trade(pos["ticket"])

            # --- periodic normal re-analysis ---
            self.state = AgentState.OBSERVING

    def _analyze_symbol(self, symbol: str, level: str):
        """Build context → call AI → validate → return decision or None."""
        from app.market.snapshot import build_snapshot
        from app.ai.prompts import build_messages, DECISION_TASK

        snapshot = build_snapshot(self.feed, symbol)
        if snapshot.get("error") == "DATA_UNAVAILABLE":
            log.warning(f"{symbol} DATA_UNAVAILABLE — skipping")
            return None

        ctx_parts = [f"Symbol: {symbol}"]
        ctx_parts.append(f"Tick: bid={snapshot['tick']['bid']} ask={snapshot['tick']['ask']} "
                         f"spread={snapshot['spread']}")
        for tf, tf_data in snapshot.get("timeframes", {}).items():
            if "error" in tf_data:
                ctx_parts.append(f"{tf}: {tf_data['error']}")
                continue
            ctx_parts.append(
                f"{tf}: close={tf_data['last_close']} sma20={tf_data['sma20']} "
                f"ema50={tf_data['ema50']} rsi={tf_data['rsi']} "
                f"adx={tf_data['adx']} atr%={tf_data['atr_pct']} "
                f"structure={tf_data['structure']} "
                f"support={[round(x,5) for x in tf_data['levels']['support']]} "
                f"resistance={[round(x,5) for x in tf_data['levels']['resistance']]}")
        ctx_parts.append(f"Regime: {snapshot.get('regime', 'UNKNOWN')}")
        market_context = "\n".join(ctx_parts)

        news = self.repo.recent_news(limit=10)
        news_lines = [f"- [{n.get('importance','LOW')}] {n['title']}" for n in news
                      if symbol[:3] in str(n.get('currencies', []))]
        news_context = "\n".join(news_lines) if news_lines else "No relevant news."

        calendar_ctx = "Economic calendar: no upcoming events in window."
        positions = self.repo.open_positions(symbol)
        pos_ctx = ""
        if positions:
            pos_ctx = "\n".join(
                f"{p['direction']} {p['volume']} @ {p['entry_price']} "
                f"SL={p['stop_loss']} TP={p['take_profit']}" for p in positions)

        prev = self.repo.latest_thesis(symbol)
        prev_ctx = ""
        if prev:
            prev_ctx = f"bias={prev['bias']} regime={prev['regime']} " \
                       f"thesis={prev['thesis']} invalidation={prev['invalidation']}"

        system, user, max_tokens = build_messages(
            level, market_context, news_context, calendar_ctx,
            pos_ctx, prev_ctx, DECISION_TASK)

        # --- call AI ---
        import os
        from app.ai.client import estimate_cost
        model_normal = os.environ.get("AI_MODEL_NORMAL", "openai/gpt-4o-mini")
        model_critical = os.environ.get("AI_MODEL_CRITICAL", "openai/gpt-4o")
        model_name = model_critical if level == "CRITICAL" else model_normal
        try:
            result = self.ai.chat(system, user, model_name, max_tokens=max_tokens)
        except Exception as exc:
            log.error(f"AI call failed: {exc}")
            self.repo.log_ai_request(f"analysis_{symbol}", model_name, 0, 0, 0, symbol, "error")
            return None

        # --- record tokens ---
        cost = estimate_cost(model_name, result["input_tokens"], result["output_tokens"])
        self.budget.record(f"analysis_{symbol}", result["input_tokens"], result["output_tokens"])
        self.repo.log_ai_request(f"analysis_{symbol}", result["model"],
                                 result["input_tokens"], result["output_tokens"],
                                 cost, symbol)

        # --- parse + validate ---
        from app.ai.schemas import parse_json, validate_decision, SchemaError
        try:
            obj = parse_json(result["content"])
            decision = validate_decision(obj)
            return decision
        except SchemaError as e:
            log.error(f"AI output validation failed: {e}")
            self.repo.save_decision(
                request_id=f"bad_{symbol}_{int(time.time())}",
                symbol=symbol, decision="PARSE_ERROR", approved=False,
                reject_reasons=[str(e)], payload={"raw": result["content"][:500]},
                versions=self.settings.get("versions", {}))
            return None

    def _review_trade(self, trade_id: str):
        """AI post-trade review."""
        trade = self.repo.get_trade(trade_id)
        if not trade:
            return
        from app.ai.prompts import build_messages, REVIEW_TASK
        prompt = f"""Trade to review:
{json.dumps(trade, indent=2, default=str)}

Full thesis history:
{self.repo.thesis_history(trade['symbol'], limit=24)}"""
        system, user, max_tokens = build_messages(
            "CRITICAL", prompt, "", "", "", "", REVIEW_TASK)
        try:
            result = self.ai.chat(system, user, "openai/gpt-4o-mini",
                                  max_tokens=500)
            from app.ai.schemas import parse_json
            obj = parse_json(result["content"])
            cls = obj.get("classification", "DATA_ERROR")
            review = obj.get("review", "")
            self.repo.save_review(trade_id, cls, review)
            log.info(f"review {trade_id}: {cls}")
        except Exception as exc:
            log.error(f"review failed: {exc}")


# needed for _review_trade json.dumps
import json
