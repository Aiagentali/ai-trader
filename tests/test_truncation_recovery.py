"""Truncation is the #1 failure mode on reasoning models: never lose a call to it.

Layers under test:
  1. budget escalation (doubling + reasoning headroom, capped)
  2. JSON repair of a cut-off completion
  3. agent-level salvage: a recovered trade answer must degrade to WAIT
"""
import io
import json

import pytest

from tests.test_core_regressions import agent, provider  # noqa: F401  shared fixtures

from app.ai.client import OpenAICompatProvider, AIError
from app.ai.schemas import parse_json, SchemaError, validate_decision


def _resp(payload):
    return io.BytesIO(json.dumps(payload).encode())


def _completion(content, finish_reason="stop", reasoning=0, completion_tokens=100):
    return {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": completion_tokens,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


def _provider(monkeypatch, bodies):
    """Provider whose urlopen replays `bodies` in order, recording the budgets sent."""
    sent = []

    def urlopen(req, **kwargs):
        sent.append(json.loads(req.data)["max_tokens"])
        return _resp(bodies[len(sent) - 1])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return OpenAICompatProvider(api_key="k", base_url="http://x/v1", timeout=5), sent


# --- layer 1: budget escalation -------------------------------------------------

def test_truncation_escalates_budget_using_reasoning_headroom(monkeypatch):
    p, sent = _provider(monkeypatch, [
        _completion('{"decision": "WA', "length", reasoning=1800),
        _completion('{"decision": "WAIT", "confidence": 70}', "stop"),
    ])
    result = p.chat("s", "u", "m", max_tokens=2000)
    assert json.loads(result["content"])["decision"] == "WAIT"
    assert sent[0] == 2000
    # 2*2000=4000 vs 2000+1800*1.5+512=5212 -> headroom wins, proving reasoning is read
    assert sent[1] == 5212
    assert result["reasoning_tokens"] == 0  # this call's own usage, not the truncated one's


def test_escalation_is_capped_and_reports_partial(monkeypatch):
    p, sent = _provider(monkeypatch, [
        _completion('{"symbol": "EURUSD", "decision": "B', "length", reasoning=900)
        for _ in range(3)
    ])
    with pytest.raises(AIError) as err:
        p.chat("s", "u", "m", max_tokens=6000)
    assert sent == [6000, 8000, 8000]  # capped by MAX_TOKEN_BUDGET
    assert err.value.partial.startswith('{"symbol"')
    assert err.value.reasoning_tokens == 900


def test_retryable_http_error_is_retried_at_same_budget(monkeypatch):
    import urllib.error
    p = OpenAICompatProvider(api_key="k", base_url="http://x/v1", timeout=5)
    calls = []

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data)["max_tokens"])
        if len(calls) == 1:
            raise urllib.error.HTTPError("http://x", 503, "boom", None, None)
        return _resp(_completion('{"ok": true}'))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert json.loads(p.chat("s", "u", "m", max_tokens=1200)["content"]) == {"ok": True}
    assert calls == [1200, 1200]


# --- layer 2: JSON repair -------------------------------------------------------

def test_repair_closes_a_cut_off_text_field():
    raw = '{"symbol": "EURUSD", "decision": "WAIT", "confidence": 80, "thesis": "trend up'
    out = parse_json(raw)
    assert out["symbol"] == "EURUSD"
    assert out["thesis"].startswith("trend up")
    validate_decision(out)


def test_repair_closes_an_unterminated_array_element():
    raw = ('{"symbol": "GBPUSD", "decision": "WAIT", "confidence": 65, '
           '"take_profit": [], "conditions": ["M15 close above 1.28", "RSI > 55')
    out = parse_json(raw)
    assert out["conditions"] == ["M15 close above 1.28", "RSI > 55"]
    validate_decision(out)


def test_repair_drops_an_incomplete_trailing_object():
    raw = '{"symbol": "GBPUSD", "decision": "WAIT", "risks": ["a"], {"x": 1'
    assert parse_json(raw)["risks"] == ["a"]


def test_repair_ignores_braces_inside_strings():
    raw = '{"symbol": "X", "thesis": "close {1.08} then hold", "decision": "WAIT"}'
    assert parse_json(raw)["thesis"] == "close {1.08} then hold"


def test_unrecoverable_garbage_raises_schema_error():
    with pytest.raises(SchemaError):
        parse_json("I cannot decide; please try again.")


# --- layer 3: agent-level salvage ----------------------------------------------

def _wait_decision(symbol="EURUSD", decision="WAIT", **extra):
    obj = {"symbol": symbol, "decision": decision, "confidence": 70,
           "market_regime": "TRENDING_UP", "setup": "none", "time_horizon": "intraday",
           "entry": None, "stop_loss": None, "take_profit": [],
           "position_size": None, "risk_reward": None,
           "thesis": "structure intact, no trigger", "technical_analysis": "ok",
           "price_action_analysis": "ok", "fundamental_analysis": "none",
           "news_analysis": "none", "invalidation": "structure break",
           "conditions": [], "risks": []}
    obj.update(extra)
    return obj


def test_truncated_wait_analysis_is_recovered(agent, provider, monkeypatch):
    body = json.dumps(_wait_decision())
    cut = body[: body.rindex('"risks"')] + '"risks": ["spread wid'
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, **kw: _resp(_completion(cut, "length", reasoning=1500)))
    agent.ai = provider
    decision = agent._analyze_symbol("EURUSD", "NORMAL")
    assert decision is not None
    assert decision["decision"] == "WAIT"
    assert decision.get("_recovered_from_truncation") is True
    # the recovery is auditable, never silent
    import json as _json
    row = agent.repo.conn.execute(
        "SELECT decision, reject_reasons FROM trade_decisions "
        "WHERE request_id LIKE 'recovered_%'").fetchall()
    assert row and row[0][0] == "RECOVERED"
    assert "truncated" in " ".join(_json.loads(row[0][1]))


def test_truncated_trade_answer_degrades_to_wait(agent, provider, monkeypatch):
    """A BUY cut off before its levels must never reach the risk engine."""
    full = _wait_decision(decision="BUY", entry={"type": "MARKET", "price": 1.0850},
                          stop_loss=1.0810, take_profit=[1.0900], risk_reward=2.0,
                          position_size={"requested_lots": 0.01})
    body = json.dumps(full)
    cut = body[: body.index('"stop_loss"')] + '"stop_loss": 1.08'
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, **kw: _resp(_completion(cut, "length", reasoning=1500)))
    agent.ai = provider
    decision = agent._analyze_symbol("EURUSD", "NORMAL")
    assert decision["decision"] == "WAIT"
    assert decision["entry"] is None and decision["stop_loss"] is None
    assert decision["take_profit"] == [] and decision["position_size"] is None
    assert not agent.repo.open_trades()


def test_truncated_answer_with_full_levels_is_still_never_traded(agent, provider, monkeypatch):
    """A cut-off response that happens to contain entry/SL/TP must still not trade."""
    full = _wait_decision(decision="BUY", entry={"type": "MARKET", "price": 1.0850},
                          stop_loss=1.0810, take_profit=[1.0900], risk_reward=2.0,
                          position_size={"requested_lots": 0.01},
                          thesis="momentum continuation", invalidation="break below 1.0810")
    body = json.dumps(full)
    cut = body[: body.index('"risks"')] + '"risks": ["news"'
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, **kw: _resp(_completion(cut, "length", reasoning=1500)))
    agent.ai = provider
    decision = agent._analyze_symbol("EURUSD", "NORMAL")
    assert decision["decision"] == "WAIT"
    assert decision.get("_recovered_from_truncation") is True
    assert not agent.repo.open_positions()
    assert not agent.repo.open_trades()


def test_no_partial_text_available_returns_none(agent, provider, monkeypatch):
    """An error without partial content must not fabricate a decision."""
    def boom(req, **kwargs):
        raise AssertionError("no urlopen expected")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    agent.ai = provider
    from app.ai.client import AIError
    decision = agent._salvage_truncated(AIError("boom"), "s", "u", "m", "EURUSD")
    assert decision is None
