"""Decision AI — Native Google Gemini Decision 1 Engine.

Implements native support for Google DeepMind's specialized `gemini-decision-1` model
via the `/v1beta/decisions` Decisions API:
- Zero output tokens, sub-second execution.
- Evaluates inputs using calibrated probabilities (`noul` questions) and rubric scoring (`score`).
- 100% OPTIONAL & NON-BLOCKING: If disabled, offline, or facing temporary API issues,
  it falls back smoothly to pure QuantEngine with ZERO errors or disruption.
"""
import json
import logging
import urllib.request
import urllib.error
from typing import Optional

log = logging.getLogger("ai-trader.decision_ai")


class DecisionAI:
    def __init__(self, settings: dict = None, client=None):
        self.settings = (settings or {}).get("decision_ai", {})
        self.enabled = self.settings.get("enabled", False)
        self.model = self.settings.get("model", "gemini-decision-1")
        self.min_confidence = self.settings.get("min_confidence", 60)
        self.client = client

    def evaluate_setup(self, decision: dict, market_context: dict = None) -> dict:
        """Evaluate a trade decision using gemini-decision-1 with zero-error fallback."""
        action = decision.get("decision", "WAIT")
        if action not in ("BUY", "SELL"):
            return {
                "approve": True,
                "confidence": 100,
                "reason": "Non-trade observation",
                "source": "passthrough"
            }

        # If disabled or client unavailable, approve immediately (pure quant mode)
        if not self.enabled or not self.client:
            return {
                "approve": True,
                "confidence": decision.get("confidence", 70),
                "reason": "Decision AI disabled — pure quant approval",
                "source": "quant_standalone"
            }

        market_context = market_context or {}
        sym = decision.get("symbol", "")

        try:
            # 1. Native Google Decisions API with gemini-decision-1
            if "decision" in self.model.lower():
                base_url = getattr(self.client, "base_url", "https://generativelanguage.googleapis.com/v1beta")
                url = f"{base_url}/decisions"
                api_key = getattr(self.client, "api_key", "")

                lessons = market_context.get("lessons") or []
                if not lessons:
                    try:
                        from pathlib import Path
                        p = Path("data/learned_profiles.json")
                        if not p.exists():
                            p = Path(__file__).resolve().parents[2] / "data" / "learned_profiles.json"
                        if p.exists():
                            p_data = json.loads(p.read_text(encoding="utf-8"))
                            lessons = p_data.get(sym, {}).get("lessons", [])
                    except Exception:
                        pass
                lessons_str = f" Learned rules from historical training: {'; '.join(lessons[:2])}." if lessons else ""

                summary = (
                    f"{sym} {action} setup: H1 trend is {market_context.get('trend')}, "
                    f"M15 ADX is {market_context.get('adx')}, RSI is {market_context.get('rsi')}, "
                    f"price={decision.get('entry', {}).get('price')}, SL={decision.get('stop_loss')}, "
                    f"TP={decision.get('take_profit', [])}, RR={decision.get('risk_reward', 1.8)}, "
                    f"DOM imbalance={market_context.get('imbalance', 0.5)}. "
                    f"Thesis: {decision.get('thesis', '')}.{lessons_str}"
                )

                payload = {
                    "model": "gemini-decision-1",
                    "input": summary,
                    "questions": {
                        "approve_trade": {
                            "type": "noul",
                            "instructions": f"Should this {action} order on {sym} be approved given the momentum and setup geometry?"
                        },
                        "setup_quality": {
                            "type": "score",
                            "instructions": "Rate setup quality on an institutional rubric scale from 0 to 2",
                            "criteria": [
                                "0: Weak momentum or counter-trend risk",
                                "1: Solid standard institutional setup",
                                "2: Exceptional high-probability A+ setup"
                            ]
                        }
                    }
                }

                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": api_key
                    }
                )

                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                answers = data.get("answers", {})
                noul_ans = answers.get("approve_trade", {})
                score_ans = answers.get("setup_quality", {})

                prob = float(noul_ans.get("noul", noul_ans.get("confidence", 0.5)))
                quality_score = round(float(score_ans.get("score", 1.0)), 2)
                confidence = int(prob * 100)

                threshold = self.min_confidence / 100.0
                if prob >= threshold:
                    return {
                        "approve": True,
                        "confidence": confidence,
                        "reason": f"gemini-decision-1 approved (prob={prob*100:.1f}%, quality={quality_score}/2.0)",
                        "source": "gemini-decision-1"
                    }
                else:
                    return {
                        "approve": False,
                        "confidence": confidence,
                        "reason": f"gemini-decision-1 rejected (prob {prob*100:.1f}% < threshold {self.min_confidence}%)",
                        "source": "gemini-decision-1"
                    }

            # 2. Fallback to Generative Chat Completion (if another model configured)
            system_instruction = (
                "You are a low-latency financial decision engine. "
                "Validate if the quantitative trade setup has high probability. "
                "Respond strictly with a single JSON object having keys: "
                "approve (boolean), confidence (integer 0-100), and reason (short string max 25 words)."
            )
            resp = self.client.chat(
                system=system_instruction,
                user=f"Evaluate setup: {json.dumps(decision)}",
                model=self.model,
                max_tokens=150,
                temperature=0.1
            )
            raw = resp.get("content", "").strip()
            if raw.startswith("```"):
                lines = raw.splitlines()
                raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            data = json.loads(raw)
            approved = bool(data.get("approve", False))
            conf = int(data.get("confidence", 70))
            reason = str(data.get("reason", "Decision AI evaluated setup"))
            return {
                "approve": approved,
                "confidence": conf,
                "reason": reason,
                "source": "decision_ai"
            }

        except Exception as exc:
            # Guarantee: Zero crash / blocking. Gracefully falls back to pure quant decision.
            log.warning(f"Decision AI evaluation skipped ({exc}) — falling back smoothly to Quant decision")
            return {
                "approve": True,
                "confidence": decision.get("confidence", 70),
                "reason": f"Quant fallback (AI unavailable: {exc})",
                "source": "fallback_quant"
            }
