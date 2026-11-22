import json
from pathlib import Path
import pytest
from app.strategy.trainer import run_backtest_simulation, self_train_and_optimize
from app.dashboard.server import create_app


def test_run_backtest_simulation_synthetic():
    res = run_backtest_simulation(symbol="EURUSD", count=300, initial_balance=30.0)
    assert res["symbol"] == "EURUSD"
    assert res["initial_balance"] == 30.0
    assert "final_balance" in res
    assert "net_profit" in res
    assert "total_trades" in res
    assert "equity_curve" in res
    assert isinstance(res["equity_curve"], list)
    assert len(res["equity_curve"]) >= 1


def test_self_train_and_optimize():
    profile = self_train_and_optimize(symbol="EURUSD", count=300, initial_balance=30.0)
    assert profile["symbol"] == "EURUSD"
    assert "best_params" in profile
    assert "adx_min" in profile["best_params"]
    assert "volume_ratio" in profile["best_params"]
    assert "lessons" in profile
    assert isinstance(profile["lessons"], list)
    assert len(profile["lessons"]) > 0


def test_dashboard_backtest_and_train_api(tmp_path):
    app = create_app({
        "TESTING": True,
        "PANEL_ROOT": str(tmp_path),
        "PANEL_ENV": str(tmp_path / ".env"),
        "CGROUP_ROOT": str(tmp_path / "missing-cgroup"),
        "DATABASE": str(tmp_path / "panel.db")
    })
    client = app.test_client()

    token = client.get("/api/session").get_json()["csrf_token"]
    headers = {"X-CSRF-Token": token}

    # Test /api/backtest/run
    res_bt = client.post("/api/backtest/run", json={"symbol": "EURUSD", "count": 200, "balance": 30.0}, headers=headers)
    assert res_bt.status_code == 200
    bt_data = res_bt.get_json()
    assert bt_data["symbol"] == "EURUSD"
    assert "equity_curve" in bt_data

    # Test /api/train/run
    res_tr = client.post("/api/train/run", json={"symbol": "EURUSD", "count": 200, "balance": 30.0}, headers=headers)
    assert res_tr.status_code == 200
    tr_data = res_tr.get_json()
    assert tr_data["symbol"] == "EURUSD"
    assert "lessons" in tr_data
