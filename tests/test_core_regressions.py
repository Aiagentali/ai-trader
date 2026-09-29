"""Offline regressions: production provider, mock market, temporary SQLite only."""
import json
import pytest
from app.ai.schemas import validate_decision, SchemaError


def buy(**overrides):
    return dict(symbol="EURUSD", decision="BUY", confidence=80,
                entry={"price": 1.085}, stop_loss=1.082, take_profit=[1.091],
                position_size=None, thesis="Test-only thesis", invalidation="below SL",
                **overrides)


@pytest.mark.parametrize("field,value", [
    ("stop_loss", None), ("stop_loss", True), ("stop_loss", float("nan")),
    ("stop_loss", float("inf")), ("take_profit", [{"price": 1.091}]),
    ("take_profit", [True]), ("take_profit", [float("nan")]),
    ("entry", {"price": True}), ("entry", {"price": float("inf")}),
    ("risk_reward", True), ("risk_reward", float("nan")),
    ("position_size", {"requested_lots": float("inf")}),
])
def test_invalid_trade_numbers_are_schema_errors(field, value):
    decision = buy()
    decision[field] = value
    with pytest.raises(SchemaError):
        validate_decision(decision)


@pytest.fixture
def agent(tmp_path):
    from app.agent.loop import Agent
    from app.database.repository import Repository
    from app.market.mt5 import MockFeed
    from app.ai.token_budget import TokenBudgetController
    from app.risk.engine import RiskEngine
    from app.execution.engine import ExecutionEngine
    from app.config.config_loader import load_risk
    repo = Repository(str(tmp_path / "offline.db"))
    feed = MockFeed(seed=77)
    class MockAI:
        """Explicit offline AI fixture; never contacts a provider."""
        decision = {"symbol": "EURUSD", "decision": "WAIT", "confidence": 40,
                    "thesis": "Test-only thesis", "invalidation": "Test-only"}
        calls = 0
        def chat(self, system, user, model, **kwargs):
            self.calls += 1
            self.last_model = model
            return {"content": json.dumps(self.decision), "model": model,
                    "input_tokens": 10, "output_tokens": 10}
    obj = Agent(feed, repo, MockAI(), TokenBudgetController(repo),
                RiskEngine(load_risk(), repo, feed), ExecutionEngine(repo, feed, "simulation"),
                ["EURUSD"], {"analysis": {"normal_interval_minutes": 60}})
    yield obj
    repo.close()


@pytest.mark.parametrize("action", ["WAIT", "HOLD", "WATCH"])
def test_non_trade_decision_and_thesis_persist(agent, action):
    agent.ai.decision = dict(agent.ai.decision, decision=action)
    agent._tick()
    rows = agent.repo.conn.execute("SELECT * FROM trade_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["decision"] == action
    assert rows[0]["approved"] is None  # diagnosis, not a risk-approved order
    assert agent.repo.latest_thesis("EURUSD")["thesis"] == "Test-only thesis"
    assert not agent.repo.open_positions()


def test_buy_null_size_uses_risk_lots_and_single_decision(agent):
    agent.ai.decision = buy()
    agent._tick()
    positions = agent.repo.open_positions()
    assert len(positions) == 1
    assert positions[0]["volume"] == 0.16
    rows = agent.repo.conn.execute("SELECT * FROM trade_decisions").fetchall()
    assert len(rows) == 1 and rows[0]["approved"] == 1


@pytest.mark.parametrize("symbol", ["GBPUSD", None])
def test_analysis_rejects_wrong_or_missing_symbol(agent, symbol):
    agent.ai.decision = buy()
    agent.ai.decision["symbol"] = symbol
    agent._tick()
    assert not agent.repo.open_positions()
    rows = agent.repo.conn.execute("SELECT * FROM trade_decisions").fetchall()
    assert len(rows) == 1 and rows[0]["approved"] == 0


def test_normal_interval_keeps_sl_tp_checks_running(agent, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr("app.agent.loop.time.time", lambda: clock[0])
    checks = []
    monkeypatch.setattr(agent.execution, "check_sl_tp", lambda: checks.append(True))
    agent._tick()
    clock[0] += 30
    agent._tick()
    assert agent.ai.calls == 1
    assert len(checks) == 2
    clock[0] += 3570
    agent._tick()
    assert agent.ai.calls == 2


def test_watch_queue_never_blocks_open_position_analysis(agent, monkeypatch):
    monkeypatch.setenv("AI_MODEL_CRITICAL", "test-critical")
    agent.repo.save_position({"ticket": "TEST-ONLY", "symbol": "EURUSD", "direction": "BUY",
                              "volume": .01, "entry_price": 1.085,
                              "stop_loss": 1.0, "take_profit": 1.2})
    monkeypatch.setattr("app.agent.loop.time.time", lambda: 10000)
    agent._watch_queue["EURUSD"] = 20000
    agent._tick()
    assert agent.ai.calls == 1
    assert agent.ai.last_model == "test-critical"


def test_watch_uses_configured_watch_model(agent, monkeypatch):
    monkeypatch.setenv("AI_MODEL_WATCH", "test-watch")
    agent._analyze_symbol("EURUSD", "WATCH")
    assert agent.ai.last_model == "test-watch"


@pytest.mark.parametrize("body", ["offline-test-secret", "[]", '{"choices": []}',
                                  '{"data": null}', '{"choices": [{}]}'])
def test_invalid_provider_response_is_sanitized(provider, monkeypatch, body):
    from app.ai.client import AIError
    mock_http(monkeypatch, body)
    with pytest.raises(AIError) as error:
        provider.chat("offline", "offline", "test-model")
    assert "offline-test-secret" not in str(error.value)


def test_provider_rejects_oversized_response(provider, monkeypatch):
    from app.ai.client import AIError
    mock_http(monkeypatch, json.dumps(completion()) + " " * 1_000_001)
    with pytest.raises(AIError, match="limit"):
        provider.chat("offline", "offline", "test-model")


def test_non_transient_failure_is_not_retried(agent, monkeypatch):
    from app.ai.client import AIError
    calls = []
    def invalid(*args, **kwargs):
        calls.append(True)
        raise AIError("Invalid provider response")
    monkeypatch.setattr(agent.ai, "chat", invalid)
    monkeypatch.setattr("time.sleep", lambda _: None)
    agent._tick()
    assert len(calls) == 1


def completion(reason="stop"):
    return {"choices": [{"message": {"content": json.dumps(buy())}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}}


@pytest.fixture
def provider(monkeypatch):
    from app.ai.client import get_provider
    monkeypatch.setenv("AI_PROVIDER", "openai-compat")
    monkeypatch.setenv("AI_API_KEY", "offline-test-secret")
    monkeypatch.setenv("AI_BASE_URL", "http://127.0.0.1:1/v1")
    return get_provider()


def mock_http(monkeypatch, body):
    """Fake HTTP boundary only; production get_provider/chat executes."""
    import io
    requests = []
    def urlopen(req, **kwargs):
        requests.append(json.loads(req.data))
        return io.BytesIO(body.encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


@pytest.mark.parametrize("envelope", [False, True])
def test_production_provider_json_contract(provider, monkeypatch, envelope):
    data = completion()
    requests = mock_http(monkeypatch, json.dumps({"data": data} if envelope else data))
    result = provider.chat("offline", "offline", "test-model")
    assert json.loads(result["content"])["decision"] == "BUY"
    assert result["output_tokens"] == 20
    assert requests[0].get("stream") is False


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls"])
def test_incomplete_completion_never_returns_trade(provider, monkeypatch, reason):
    from app.ai.client import AIError
    mock_http(monkeypatch, json.dumps(completion(reason)))
    with pytest.raises(AIError):
        provider.chat("offline", "offline", "test-model")


def test_provider_error_is_sanitized_actionable(provider, monkeypatch):
    import email.message
    import io
    import urllib.error
    from app.ai.client import AIError

    def http_error(status, body):
        def _raise(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, status, "err",
                                         hdrs=email.message.Message(),
                                         fp=io.BytesIO(body.encode()))
        return _raise

    monkeypatch.setattr("urllib.request.urlopen", http_error(401, '{"error":"bad key 1234"}'))
    with pytest.raises(AIError) as ei:
        provider.chat("offline", "offline", "test-model")
    msg = str(ei.value)
    assert "401" in msg and "auth" in msg.lower() and "AI_API_KEY" in msg
    assert "offline-test-secret" not in msg
    assert "Bearer" not in msg

    monkeypatch.setattr("urllib.request.urlopen", http_error(429, '{"error":"quota"}'))
    with pytest.raises(AIError) as ei:
        provider.chat("offline", "offline", "test-model")
    assert "429" in str(ei.value) and "retry" in str(ei.value).lower()

    monkeypatch.setattr("urllib.request.urlopen", http_error(502, '{"error":"upstream"}'))
    with pytest.raises(AIError) as ei:
        provider.chat("offline", "offline", "test-model")
    assert "502" in str(ei.value) and "transient" in str(ei.value).lower()


def test_retry_is_bounded_and_transient_only(agent, monkeypatch):
    class FlakyAI:
        """Transient failure then success — exercises loop retry path."""
        def __init__(self):
            self.calls = 0
        def chat(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                from app.ai.client import AIError
                raise AIError("HTTP 502 transient upstream error", retryable=True)
            return {"content": json.dumps({"symbol": "EURUSD", "decision": "WAIT",
                                           "confidence": 40, "thesis": "Test-only",
                                           "invalidation": "Test-only"}),
                    "model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr("time.sleep", lambda s: None)
    agent.ai = FlakyAI()
    agent._tick()
    assert agent.ai.calls == 2  # bounded: exactly one retry

    class AuthFail:
        calls = 0
        def chat(self, *a, **k):
            AuthFail.calls += 1
            raise RuntimeError("HTTP 401 — API key rejected; check AI_API_KEY")

    monkeypatch.setattr("time.sleep", lambda s: None)
    agent._normal_queue.clear()  # move past the normal-analysis interval gate
    agent.ai = AuthFail()
    agent._tick()
    assert AuthFail.calls == 1  # auth failures are not retried


def test_unmatched_close_never_closes_unrelated_trade(tmp_path):
    """Regression: close_trade matches ONLY its trade_id — an unknown
    identifier (e.g. a position ticket) must never close any other trade."""
    from app.database.repository import Repository
    repo = Repository(str(tmp_path / "close.db"))
    for tid, opened, sym in (("T_A", "2026-01-01T10:00:00Z", "EURUSD"),
                             ("T_B", "2026-01-02T10:00:00Z", "GBPUSD")):
        repo.save_trade({"trade_id": tid, "opened_ts": opened, "symbol": sym,
                         "direction": "BUY", "entry": 1.0, "stop_loss": 0.9,
                         "take_profit": "[]", "position_size": 0.1,
                         "risk_percent": 0.5, "rr": 2.0, "market_regime": "",
                         "setup": "", "ai_confidence": 50, "thesis": "Test-only",
                         "invalidation": "Test-only", "status": "open",
                         "request_id": f"req_{tid}"})
    # Execution-style close with an unknown identifier: strictly no-op
    assert repo.close_trade("SIM-999999", 1.0, 5.0, 1.0, "stop_loss_hit") is None
    assert repo.get_trade("T_A")["status"] == "open"   # unrelated — untouched
    assert repo.get_trade("T_B")["status"] == "open"   # unrelated — untouched
    # Exact-id closes still work one row at a time
    assert repo.close_trade("T_B", 1.0, 5.0, 1.0, "stop_loss_hit") == "T_B"
    assert repo.get_trade("T_B")["status"] == "closed"
    assert repo.get_trade("T_A")["status"] == "open"


def test_close_trade_returns_matched_id(tmp_path):
    """close_trade reports which trade row it actually closed (or None)."""
    from app.database.repository import Repository
    repo = Repository(str(tmp_path / "close2.db"))
    repo.save_trade({"trade_id": "req-1", "opened_ts": "2026-01-01T10:00:00Z",
                     "symbol": "EURUSD", "direction": "BUY", "entry": 1.0,
                     "stop_loss": 0.9, "take_profit": "[]", "position_size": 0.1,
                     "risk_percent": 0.5, "rr": 2.0, "market_regime": "",
                     "setup": "", "ai_confidence": 50, "thesis": "Test-only",
                     "invalidation": "Test-only", "status": "open",
                     "request_id": "req-1"})
    assert repo.close_trade("SIM-404", 1.0, 0.0, 0.0, "stop_loss_hit") is None
    assert repo.close_trade("req-1", 1.0, 0.0, 0.0, "stop_loss_hit") == "req-1"


def test_simulation_restart_never_overwrites_position(agent):
    from app.execution.engine import ExecutionEngine
    decision = buy()
    decision["position_size"] = {"requested_lots": .01}
    first = agent.execution.place_order(decision)
    restarted = ExecutionEngine(agent.repo, agent.feed, "simulation")
    second = restarted.place_order(decision)
    assert first["ticket"] != second["ticket"]
    assert len(agent.repo.open_positions()) == 2


def test_demo_executor_id_linkage_with_fake_mt5(agent):
    """Local fake MT5 only: no SDK import, connection, or external trade."""
    from types import SimpleNamespace
    from app.execution.mt5_demo import MT5DemoExecutor
    fake = SimpleNamespace(
        symbol_info_tick=lambda _: SimpleNamespace(bid=1.085, ask=1.0851),
        symbol_info=lambda _: SimpleNamespace(point=.00001),
        order_send=lambda _: SimpleNamespace(order=12345, price=1.0851, retcode=10009),
        TRADE_RETCODE_DONE=10009, TRADE_ACTION_DEAL=1,
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
        ORDER_TIME_GTC=0, ORDER_FILLING_IOC=1)
    exe = MT5DemoExecutor(agent.repo, agent.feed)
    exe._mt5 = fake
    exe._connected = True  # explicitly injected fake, never connect()
    decision = buy()
    decision["position_size"] = {"requested_lots": .01}
    result = exe.place_order(decision)
    ticket = result["ticket"]
    assert agent.repo.get_trade(ticket)["request_id"] == result["request_id"]
    assert exe.close_position(ticket)["status"] == "CLOSED_MT5_DEMO"
    assert agent.repo.get_trade(ticket)["status"] == "closed"


def test_truncated_production_response_cannot_trade(agent, provider, monkeypatch):
    """Truncation must never trade — not even after the doubled-budget retry."""
    requests = mock_http(monkeypatch, json.dumps(completion("length")))
    agent.ai = provider
    agent._tick()
    assert len(requests) == 2  # initial budget + one doubled retry
    assert [r["max_tokens"] for r in requests] == [requests[0]["max_tokens"],
                                                    requests[0]["max_tokens"] * 2]
    assert not agent.repo.open_positions()
    assert not agent.repo.open_trades()


def test_repeated_transient_failure_stops_after_two_calls(agent, monkeypatch):
    from app.ai.client import AIError
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise AIError("HTTP 502 transient", retryable=True)
    monkeypatch.setattr(agent.ai, "chat", fail)
    monkeypatch.setattr("time.sleep", lambda _: None)
    agent._tick()
    assert len(calls) == 2
    assert not agent.repo.open_positions()


def test_execution_close_reports_trade_id(tmp_path):
    """Execution closes the exact trade of the position and returns its id."""
    from app.database.repository import Repository
    from app.execution.engine import ExecutionEngine
    from app.market.mt5 import MockFeed
    repo = Repository(str(tmp_path / "close3.db"))
    exe = ExecutionEngine(repo, MockFeed(seed=7), "simulation")
    decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                "entry": {"price": 1.085}, "stop_loss": 1.082,
                "take_profit": [1.091], "position_size": {"requested_lots": 0.16},
                "thesis": "Test-only thesis", "invalidation": "Test-only"}
    exe.place_order(decision)
    ticket = repo.open_positions()[0]["ticket"]
    result = exe.close_position(ticket, reason="manual")
    assert result["status"] == "CLOSED_SIMULATED"
    # trade_id must match the position ticket exactly — no fuzzy reconciliation
    assert result["trade_id"] == ticket
    trade = repo.get_trade(result["trade_id"])
    assert trade is not None and trade["status"] == "closed"


def test_wait_with_null_entry_price_and_zero_lots_passes():
    obj = {
        "symbol": "USDJPY", "decision": "WAIT", "confidence": 97,
        "market_regime": "UNCLEAR", "setup": "none", "time_horizon": "intraday",
        "entry": {"type": "MARKET", "price": None}, "stop_loss": None,
        "take_profit": [], "position_size": {"requested_lots": 0},
        "risk_reward": 0, "thesis": "wait", "invalidation": "n/a",
    }
    d = validate_decision(obj)
    assert d["decision"] == "WAIT"


def test_buy_still_requires_positive_lots_and_priced_entry():
    base = {
        "symbol": "USDJPY", "decision": "BUY", "confidence": 80,
        "market_regime": "TRENDING_UP", "setup": "breakout",
        "time_horizon": "intraday", "entry": {"type": "MARKET", "price": None},
        "stop_loss": None, "take_profit": [], "position_size": None,
        "risk_reward": None, "thesis": "x", "invalidation": "y",
    }
    with pytest.raises(SchemaError):
        validate_decision(base)
