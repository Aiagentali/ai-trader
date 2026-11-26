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
        self.use_market_watch = self.settings.get("trading", {}).get("use_market_watch", False)
        self._last_mw_sync = 0
        self.state = AgentState.OBSERVING
        self.running = False
        self._watch_queue = {}  # symbol -> next_check_timestamp
        self._normal_queue = {}
        self._closed_market_noted = {}
        engine_cfg = self.settings.get("trading", {}).get("engine")
        self.engine_mode = engine_cfg if engine_cfg else ("ai" if self.ai else "quant")
        from app.strategy.quant_engine import QuantEngine
        self.quant = QuantEngine(settings=self.settings)
        from app.strategy.decision_ai import DecisionAI
        self.decision_ai = DecisionAI(settings=self.settings, client=self.ai)

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
        closed_ids = self.execution.check_sl_tp() or []
        for tid in closed_ids:
            self._post_mortem_loss(tid)

        now = time.time()

        # --- dynamic market watch sync ---
        if self.use_market_watch and (now - self._last_mw_sync > 30):
            self._last_mw_sync = now
            if hasattr(self.feed, "get_market_watch_symbols"):
                mw_syms = self.feed.get_market_watch_symbols()
                if mw_syms and set(mw_syms) != set(self.symbols):
                    log.info(f"Market Watch updated dynamically: {mw_syms}")
                    self.symbols = mw_syms

        open_positions = self.repo.open_positions()
        open_symbols = {p["symbol"] for p in open_positions}
        from app.risk.engine import is_market_open
        from datetime import datetime, timezone

        for sym in self.symbols:
            # --- weekend / market-closed filter (only Crypto 24/7 on weekends) ---
            if not is_market_open(sym) and sym not in open_symbols:
                if now - self._closed_market_noted.get(sym, 0) > 1800:
                    self._closed_market_noted[sym] = now
                    closed_payload = {
                        "symbol": sym,
                        "decision": "WAIT",
                        "market_regime": "MARKET_CLOSED",
                        "analyzed_at": datetime.now(timezone.utc).isoformat(),
                        "persian_reason": "بازار در تعطیلات آخر هفته بسته است؛ در حال حاضر فقط رمز ارزها (۲۴/۷) تحلیل می‌شوند.",
                        "pipeline_stages": [
                            {"key": "market", "label": "بازار", "status": "fail", "detail": "تعطیلات (بسته)"}
                        ],
                    }
                    self.repo.save_thesis(
                        sym, "MARKET_CLOSED", "MARKET_CLOSED",
                        "Market closed (weekend) - only 24/7 crypto scanned",
                        "N/A", closed_payload)
                continue
            # --- watch queue ---
            if sym in open_symbols:
                self._watch_queue.pop(sym, None)
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

            if level == "NORMAL":
                if now < self._normal_queue.get(sym, 0):
                    continue
                default_interval = 1 if self.engine_mode == "quant" else 60
                interval_min = self.settings.get("analysis", {}).get(
                    "normal_interval_minutes", default_interval)
                self._normal_queue[sym] = now + interval_min * 60

            # --- analyze & decide (Quant vs AI) ---
            if self.engine_mode == "quant" or not self.ai:
                self.state = AgentState.ANALYZING
                decision = self.quant.analyze_symbol(sym, self.feed)
            else:
                # --- token gate for AI ---
                if self.budget and not self.budget.check_and_allow(f"analysis_{sym}", level=level):
                    log.warning(f"token budget exhausted — skipping {sym}")
                    continue
                self.state = AgentState.ANALYZING
                decision = self._analyze_symbol(sym, level)

            if decision is None:
                continue

            d = decision.get("decision", "WAIT")

            # --- Optional Decision AI Gate (Jev-inspired fast classification) ---
            if d in ("BUY", "SELL") and getattr(self, "decision_ai", None) and self.decision_ai.enabled:
                ai_eval = self.decision_ai.evaluate_setup(decision, decision.get("market_context", {}))
                self.repo.log_ai_request(
                    f"decision_{sym}", self.decision_ai.model, 0, 0, 0.0, sym,
                    "error" if ai_eval.get("source") == "error_fallback" else "ok")
                stages = decision.setdefault("pipeline_stages", [])
                if not ai_eval.get("approve", True):
                    log.info(f"{sym} | Decision AI rejected: {ai_eval.get('reason')}")
                    d = "WAIT"
                    decision["decision"] = "WAIT"
                    reason_str = ai_eval.get("reason", "تایید نشد")
                    decision["thesis"] = f"[Decision AI: {reason_str}] " + decision.get("thesis", "")
                    decision["persian_reason"] = f"ستاپ {decision.get('setup', '')} توسط Decision AI رد شد: {reason_str}"
                    stages.append({"key": "ai", "label": "Decision AI", "status": "fail", "detail": str(reason_str)[:40]})
                else:
                    log.info(f"{sym} | Decision AI approved: {ai_eval.get('reason')}")
                    decision["thesis"] += f" | Decision AI: {ai_eval.get('reason')}"
                    decision["gemini_prob"] = ai_eval.get("confidence")
                    stages.append({"key": "ai", "label": "Decision AI", "status": "pass", "detail": f"تایید ({ai_eval.get('confidence')}%)"})

            log.info(f"{sym} | state={self.state.value} | decision={d} | "
                     f"confidence={decision.get('confidence')}")

            # Non-trade observations are diagnostic records, not approved orders.
            if d in ("WAIT", "HOLD", "WATCH"):
                self.repo.save_decision(
                    request_id=f"decision_{sym}_{time.time_ns()}",
                    symbol=sym, decision=d, approved=None, reject_reasons=[],
                    payload=decision, versions=self.settings.get("versions", {}))
                self.repo.save_thesis(
                    sym, "WATCHING_SETUP" if d == "WATCH" else d,
                    decision.get("market_regime", ""), decision.get("thesis", ""),
                    decision.get("invalidation", ""), decision)

            # --- apply decision ---
            if d == "WAIT":
                self.state = AgentState.WAITING

            elif d == "WATCH":
                self.state = AgentState.WATCHING_SETUP
                interval = self.settings.get("analysis", {}).get(
                    "watch_interval_minutes", 10) * 60
                self._watch_queue[sym] = now + interval

            elif d in ("BUY", "SELL"):
                # Nullable AI sizing is unspecified, not an executable size.
                decision["position_size"] = decision.get("position_size") or {}
                account = self.feed.get_account_info()
                risk_result = self.risk.validate(decision, account)
                stages = decision.setdefault("pipeline_stages", [])
                if risk_result.approved:
                    stages.append({"key": "risk", "label": "ریسک و اسپرد", "status": "pass",
                                   "detail": f"{risk_result.calculated_lots} lot ({risk_result.risk_percent}%)"})
                else:
                    stages.append({"key": "risk", "label": "ریسک و اسپرد", "status": "fail",
                                   "detail": "; ".join(risk_result.reasons_reject)[:60]})
                    decision["persian_reason"] = f"ستاپ {d} آماده شد اما مدیریت ریسک رد کرد: " + "; ".join(risk_result.reasons_reject)

                self.repo.save_decision(
                    request_id=f"decision_{sym}_{int(now)}",
                    symbol=sym, decision=d, approved=risk_result.approved,
                    reject_reasons=risk_result.reasons_reject, payload=decision,
                    versions=self.settings.get("versions", {}))
                if risk_result.approved:
                    self.state = AgentState.EXECUTING
                    # Deterministic sizing: risk engine's calculated lots ALWAYS
                    # override whatever the AI requested (constitution #11).
                    decision.setdefault("position_size", {})[
                        "requested_lots"] = risk_result.calculated_lots
                    decision["risk_percent"] = risk_result.risk_percent
                    res = self.execution.place_order(decision)
                    log.info(f"execution result: {res}")
                    self.repo.save_thesis(
                        sym, d, decision.get("market_regime", ""),
                        decision.get("thesis", ""),
                        decision.get("invalidation", ""), decision)
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

        reviews = self.repo.recent_reviews(limit=8)
        lessons_ctx = "\n".join(reviews) if reviews else ""

        system, user, max_tokens = build_messages(
            level, market_context, news_context, calendar_ctx,
            pos_ctx, prev_ctx, DECISION_TASK, lessons=lessons_ctx)

        # --- call AI ---
        import os
        from app.ai.client import estimate_cost
        model_normal = os.environ.get("AI_MODEL_NORMAL", "openai/gpt-4o-mini")
        model_critical = os.environ.get("AI_MODEL_CRITICAL", "openai/gpt-4o")
        model_watch = os.environ.get("AI_MODEL_WATCH", model_normal)
        model_name = {"CRITICAL": model_critical, "WATCH": model_watch}.get(level, model_normal)
        from app.ai.client import AIError
        try:
            result = self.ai.chat(system, user, model_name, max_tokens=max_tokens)
        except AIError as exc:
            log.error(f"AI call failed: {exc}")
            self.repo.log_ai_request(f"analysis_{symbol}", model_name, 0, 0, 0, symbol, "error")
            if exc.retryable:
                # one bounded retry — router blips (502/503) are transient
                import time as _t
                _t.sleep(5)
                try:
                    result = self.ai.chat(system, user, model_name, max_tokens=max_tokens)
                    log.info("AI retry succeeded")
                except AIError as exc2:
                    log.error(f"AI retry failed: {exc2}")
                    self.repo.log_ai_request(f"analysis_{symbol}", model_name, 0, 0, 0, symbol, "error")
                    return self._salvage_truncated(exc2, system, user, model_name, symbol)
                except Exception as exc2:
                    log.error(f"AI retry failed: {exc2}")
                    self.repo.log_ai_request(f"analysis_{symbol}", model_name, 0, 0, 0, symbol, "error")
                    return None
            else:
                return self._salvage_truncated(exc, system, user, model_name, symbol)
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
            if decision.get("symbol") != symbol:
                raise SchemaError("AI symbol does not match requested symbol")
            return decision
        except SchemaError as e:
            log.error(f"AI output validation failed: {e}")
            self.repo.save_decision(
                request_id=f"bad_{symbol}_{int(time.time())}",
                symbol=symbol, decision="PARSE_ERROR", approved=False,
                reject_reasons=[str(e)], payload={"raw": result["content"][:500]},
                versions=self.settings.get("versions", {}))
            return None

    def _salvage_truncated(self, exc, system: str, user: str, model: str, symbol: str):
        """Last resort: rebuild the decision from a budget-truncated completion.

        Only reached when every budget escalation still ran out of tokens. The
        partial text is repaired into a validated decision so the reasoning is not
        lost, and the outcome is logged as RECOVERED. A recovered answer can never
        open or close a position: it is always downgraded to WAIT, because the
        provider itself told us the completion was cut short.
        """
        partial = getattr(exc, "partial", None)
        if not partial or not partial.strip():
            return None
        from app.ai.schemas import parse_json, validate_decision, SchemaError
        reasoning = getattr(exc, "reasoning_tokens", 0)
        try:
            obj = parse_json(partial)
        except SchemaError as e:
            log.warning(f"truncated response unusable for {symbol}: {e}")
            return None
        # A completion the provider flagged as truncated is missing whatever it was
        # still writing, so it is NEVER allowed to become a trade. The salvage exists
        # to preserve the analysis and the audit trail, not to place orders: every
        # recovered answer is downgraded to WAIT before validation, whatever it asked.
        if obj.get("decision") in ("BUY", "SELL"):
            if not _trade_fields_present(obj):
                log.warning(f"truncated trade answer for {symbol} missing levels -> WAIT")
            else:
                log.warning(f"truncated {symbol} answer asked for {obj['decision']} -> WAIT")
            obj = dict(obj, decision="WAIT", entry=None, stop_loss=None,
                       take_profit=[], position_size=None, risk_reward=None)
        try:
            decision = validate_decision(obj)
        except SchemaError as e:
            log.warning(f"truncated response failed validation for {symbol}: {e}")
            return None
        if decision.get("symbol") != symbol:
            log.warning(f"truncated response symbol mismatch for {symbol}")
            return None
        log.info(f"recovered truncated {symbol} analysis (reasoning_tokens={reasoning}) "
                 f"-> {decision.get('decision')}")
        self.repo.save_decision(
            request_id=f"recovered_{symbol}_{int(time.time())}",
            symbol=symbol, decision="RECOVERED", approved=False,
            reject_reasons=[f"recovered from truncated completion; "
                            f"reasoning_tokens={reasoning}"],
            payload={"symbol": symbol, "decision": decision.get("decision")},
            versions=self.settings.get("versions", {}))
        decision["_recovered_from_truncation"] = True
        return decision

    def _post_mortem_loss(self, trade_id: str):
        """Deterministic post-trade loss analysis & adaptive parameter learning."""
        trade = self.repo.get_trade(trade_id)
        if not trade:
            return
        pnl = trade.get("profit_loss")
        if pnl is None or pnl >= 0:
            return
        sym = trade.get("symbol", "")
        exit_reason = str(trade.get("exit_reason") or "").lower()
        opened_ts = trade.get("opened_ts") or ""
        exit_ts = trade.get("exit_ts") or ""
        duration_min = None
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(str(opened_ts).replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
            duration_min = max(0.0, (t1 - t0).total_seconds() / 60.0)
        except Exception:
            pass

        from app.risk.engine import is_crypto_symbol
        if duration_min is not None and duration_min < 15.0:
            cls = "SL_TOO_TIGHT"
            review = (f"پوزیشن {sym} در کمتر از {duration_min:.0f} دقیقه با حد ضرر بسته شد (ضرر {pnl:.2f}$). "
                      "علت احتمالی: نوسان شدید لحظه‌ای یا فاصله کم حد ضرر نسبت به اسپرد/ATR.")
        elif is_crypto_symbol(sym) and "stop_loss" in exit_reason:
            cls = "CRYPTO_VOLATILITY"
            review = (f"برخورد با حد ضرر در بازار پرنوسان کریپتو ({sym}، ضرر {pnl:.2f}$). "
                      "توصیه: نیاز به مومنتوم قوی‌تر و تایید کندل پولبک.")
        elif "stop_loss" in exit_reason:
            cls = "TREND_REVERSAL"
            review = (f"بازگشت قیمت برخلاف جهت معامله {trade.get('direction')} در {sym} و فعال شدن حد ضرر ({pnl:.2f}$). "
                      f"ستاپ ورود: {trade.get('setup') or 'Quant'}.")
        else:
            cls = "LOSS_CLOSED"
            review = f"معامله {sym} با زیان {pnl:.2f}$ بسته شد ({exit_reason or 'بسته شدن دستی/سیستمی'})."

        try:
            self.repo.save_review(trade_id, cls, review)
            log.info(f"post-mortem saved for {trade_id}: {cls}")
        except Exception as exc:
            log.warning(f"failed saving post-mortem: {exc}")

        # Adaptive learning: if 2+ consecutive losses on this symbol, raise adx_min slightly
        try:
            recent = self.repo.conn.execute(
                "SELECT profit_loss FROM trades WHERE symbol=? AND status='closed' "
                "ORDER BY exit_ts DESC LIMIT 2", (sym,)).fetchall()
            if len(recent) == 2 and all((r["profit_loss"] or 0) < 0 for r in recent):
                from pathlib import Path
                import json as _json
                p = Path("data/learned_profiles.json")
                profiles = {}
                if p.exists():
                    profiles = _json.loads(p.read_text(encoding="utf-8"))
                sym_prof = profiles.setdefault(sym, {"params": {}})
                curr_adx = float(sym_prof["params"].get("adx_min", 18.0))
                new_adx = min(25.0, curr_adx + 1.5)
                if new_adx > curr_adx:
                    sym_prof["params"]["adx_min"] = new_adx
                    sym_prof["last_lesson"] = f"افزایش خودکار حداقل ADX به {new_adx:.1f} پس از ۲ زیان متوالی"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(_json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
                    if hasattr(self, "quant"):
                        self.quant.learned_profiles = profiles
                    log.info(f"Adaptive learning: raised {sym} adx_min to {new_adx:.1f}")
        except Exception as exc:
            log.warning(f"adaptive learning update skipped: {exc}")

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
        import os
        model_normal = os.environ.get("AI_MODEL_NORMAL", "openai/gpt-4o-mini")
        try:
            result = self.ai.chat(system, user, model_normal,
                                  max_tokens=500)
            from app.ai.schemas import parse_json
            obj = parse_json(result["content"])
            cls = obj.get("classification", "DATA_ERROR")
            review = obj.get("review", "")
            lessons = obj.get("lessons") or []
            if lessons:
                review = (review + " Lessons: " + "; ".join(str(l) for l in lessons)).strip()
            self.repo.save_review(trade_id, cls, review)
            log.info(f"review {trade_id}: {cls}")
        except Exception as exc:
            log.error(f"review failed: {exc}")


# needed for _review_trade json.dumps
import json


def _trade_fields_present(decision: dict) -> bool:
    """A BUY/SELL is only tradable with entry price, stop and at least one target."""
    from app.ai.schemas import REQUIRED_FOR_TRADE
    if not REQUIRED_FOR_TRADE.issubset(set(decision)):
        return False
    entry = decision.get("entry") or {}
    if not isinstance(entry, dict) or entry.get("price") is None:
        return False
    if decision.get("stop_loss") is None:
        return False
    return bool(decision.get("take_profit"))
