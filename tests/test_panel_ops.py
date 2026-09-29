"""Panel must be able to run the whole trader, and its debug tab must explain
failures rather than just dump rows.

Covers the new /api/mt5/status readiness probe, the MT5 start gate, the
diagnostics payload, and the log-window control.
"""
import json
import sys
import types

import pytest

from app.dashboard.server import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "PANEL_ROOT": str(tmp_path),
                       "PANEL_ENV": str(tmp_path / ".env"),
                       "CGROUP_ROOT": str(tmp_path / "missing-cgroup"),
                       "DATABASE": str(tmp_path / "panel.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    return {"X-CSRF-Token": client.get("/api/session").get_json()["csrf_token"]}


def _fake_mt5(trade_mode, initialize=True):
    mt5 = types.SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0)
    mt5.initialize = lambda *a, **k: initialize
    mt5.shutdown = lambda *a, **k: None
    mt5.account_info = lambda: types.SimpleNamespace(trade_mode=trade_mode)
    return mt5


def test_mt5_status_reports_missing_package(monkeypatch, client):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    data = client.get("/api/mt5/status").get_json()
    assert data["ready"] is False
    assert "MetaTrader5" in data["reason"]


def test_mt5_status_ready_on_demo_account(monkeypatch, client):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(0))
    data = client.get("/api/mt5/status").get_json()
    assert data["ready"] is True
    assert data["reason"] == ""


def test_mt5_status_refuses_a_live_account(monkeypatch, client):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(1))
    data = client.get("/api/mt5/status").get_json()
    assert data["ready"] is False
    assert "NOT a demo" in data["reason"]


def test_mt5_status_reports_unreachable_terminal(monkeypatch, client):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "MetaTrader5", _fake_mt5(0, initialize=False))
    data = client.get("/api/mt5/status").get_json()
    assert data["ready"] is False
    assert "not reachable" in data["reason"]


def test_start_in_mt5_mode_is_gated_not_silently_blocked(monkeypatch, client):
    """The gate must be the real precondition, not a permanent 409."""
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    response = client.post("/api/process/start", json={"mode": "mt5"}, headers=csrf(client))
    assert response.status_code == 409
    body = response.get_json()
    assert "MT5 package missing" in body["error"]
    # the old message must be gone
    assert "pending a safety audit" not in body["error"]


def test_simulation_start_is_unaffected(client, app):
    """A working simulation run must still start from the panel."""
    response = client.post("/api/process/start", json={"mode": "simulation"},
                           headers=csrf(client))
    assert response.status_code == 200
    assert response.get_json()["running"] is True
    app.extensions["panel_process"].stop()


def test_log_window_is_bounded_but_large_enough(client, app):
    """A 600-line window fits a debugging session; it must still be finite."""
    from app.dashboard.control import OwnedProcess
    process = OwnedProcess()
    assert process.logs.maxlen == 600
    app.extensions["panel_process"].stop()


def test_process_logs_can_be_skipped(client, app):
    app.extensions["panel_process"].stop()
    with_logs = client.get("/api/process").get_json()
    assert isinstance(with_logs["logs"], list)
    without = client.get("/api/process?logs=0").get_json()
    assert without["logs"] == []


def test_debug_explains_failures_instead_of_only_rows(client, app):
    data = client.get("/api/debug").get_json()
    diag = data["diagnostics"]
    for key in ("ai_by_model", "ai_by_status", "decisions_by_kind", "recovered", "trades"):
        assert key in diag
    assert data["ai_requests"] == []


def test_diagnostics_counts_real_failures(client, app):
    from app.database.repository import Repository
    db_path = str(app.config["DATABASE"])
    repo = Repository(db_path)
    repo.log_ai_request("analysis_EURUSD", "m/ok", 10, 10, 0.0, "EURUSD", "ok")
    repo.log_ai_request("analysis_EURUSD", "m/bad", 0, 0, 0.0, "EURUSD", "error")
    repo.save_decision(request_id="r1", symbol="EURUSD", decision="RECOVERED",
                       approved=False, reject_reasons=["truncated"],
                       payload={}, versions={})
    repo.close()

    diag = client.get("/api/debug").get_json()["diagnostics"]
    by_model = {m["model"]: m for m in diag["ai_by_model"]}
    assert by_model["m/ok"]["failed"] == 0
    assert by_model["m/bad"]["failed"] == 1
    assert diag["ai_by_status"] == {"ok": 1, "error": 1}
    assert diag["decisions_by_kind"]["RECOVERED"] == 1
    assert diag["recovered"][0]["symbol"] == "EURUSD"


def test_last_error_names_the_failing_model(client, app):
    """A dead model id must be named in one endpoint, not buried in log lines."""
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("analysis_USDJPY", "cl/dead/model:free", 0, 0, 0.0, "USDJPY", "error")
    repo.close()
    data = client.get("/api/last_error").get_json()
    assert data["failing"] is True
    assert data["model"] == "cl/dead/model:free"
    assert data["task"] == "analysis_USDJPY"
    assert data["recent_failures"] >= 1


def test_last_error_reports_the_real_reason_not_a_bare_status(client, app):
    """An operator needs the provider's wording; "error" is not actionable."""
    import json as _json
    manager = app.extensions["panel_process"]
    manager.logs.append(_json.dumps({
        "ts": "2026-09-29T00:00:00+00:00", "level": "ERROR",
        "component": "ai-trader.agent",
        "msg": "AI call failed: AI request failed: HTTP 404 model not found"}))
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("analysis_USDJPY", "cl/dead/model:free", 0, 0, 0.0, "USDJPY", "error")
    repo.close()
    data = client.get("/api/last_error").get_json()
    assert data["reason"] == "HTTP 404 model not found"


def test_reason_survives_a_retry_failure(client, app):
    import json as _json
    manager = app.extensions["panel_process"]
    manager.logs.append(_json.dumps({
        "level": "ERROR", "component": "ai-trader.agent",
        "msg": "AI retry failed: AI completion incomplete or filtered; no decision accepted"}))
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("t", "m", 0, 0, 0.0, "EURUSD", "error")
    repo.close()
    assert "incomplete or filtered" in client.get("/api/last_error").get_json()["reason"]


def test_last_error_is_quiet_when_all_calls_succeeded(client, app):
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("analysis_EURUSD", "good/model", 10, 20, 0.0, "EURUSD", "ok")
    repo.close()
    data = client.get("/api/last_error").get_json()
    assert data["failing"] is False
    assert "model" not in data


def test_last_error_redacts_the_api_key(client, app, tmp_path):
    (tmp_path / ".env").write_text("AI_API_KEY=leakysecret\n")
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("t", "leakysecret", 0, 0, 0.0, "EURUSD", "error")
    repo.close()
    assert "leakysecret" not in client.get("/api/last_error").get_data(as_text=True)


def test_diagnostics_never_leak_the_api_key(client, app, tmp_path):
    env = tmp_path / ".env"
    env.write_text("AI_API_KEY=supersecretkey\nAI_BASE_URL=https://router.example/v1\n")
    from app.database.repository import Repository
    repo = Repository(str(app.config["DATABASE"]))
    repo.log_ai_request("t", "supersecretkey", 1, 1, 0.0, "EURUSD", "ok")
    repo.close()
    body = client.get("/api/debug").get_data(as_text=True)
    assert "supersecretkey" not in body
