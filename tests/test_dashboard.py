"""Phase 12: Dashboard tests."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _seed_data():
    """Seed test data into a fresh DB, return its path."""
    from app.database.repository import Repository
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = Repository(db_path=path)

    trades = [
        {"trade_id": "W1", "opened_ts": "2026-01-01T10:00:00Z", "symbol": "EURUSD",
         "direction": "BUY", "entry": 1.085, "stop_loss": 1.082,
         "take_profit": "[1.091]", "position_size": 0.17, "risk_percent": 0.5,
         "rr": 2.0, "market_regime": "TRENDING_UP", "setup": "breakout",
         "ai_confidence": 82, "thesis": "bullish", "invalidation": "below SL",
         "status": "closed", "exit_ts": "2026-01-01T12:00:00Z",
         "exit_price": 1.091, "profit_loss": 102.0, "r_multiple": 2.0,
         "exit_reason": "take_profit_hit"},
        {"trade_id": "L1", "opened_ts": "2026-01-02T10:00:00Z", "symbol": "GBPUSD",
         "direction": "SELL", "entry": 1.270, "stop_loss": 1.273,
         "take_profit": "[1.264]", "position_size": 0.17, "risk_percent": 0.5,
         "rr": 2.0, "market_regime": "TRENDING_DOWN", "setup": "retest",
         "ai_confidence": 75, "thesis": "bearish", "invalidation": "above SL",
         "status": "closed", "exit_ts": "2026-01-02T14:00:00Z",
         "exit_price": 1.273, "profit_loss": -51.0, "r_multiple": -1.0,
         "exit_reason": "stop_loss_hit"},
    ]
    for t in trades:
        repo.save_trade(t)
        repo.close_trade(t["trade_id"], t.get("exit_price", 0),
                         t.get("profit_loss", 0), t.get("r_multiple", 0),
                         t.get("exit_reason", ""))

    repo.save_thesis("EURUSD", "bullish", "TRENDING_UP",
                     "H1 structure bullish", "below 1.08", {})
    repo.save_thesis("GBPUSD", "bearish", "TRENDING_DOWN",
                     "H4 breakdown", "above 1.28", {})

    repo.save_position({"ticket": "SIM-100", "symbol": "USDJPY",
                        "direction": "BUY", "volume": 0.10,
                        "entry_price": 151.50, "stop_loss": 151.00,
                        "take_profit": 152.50})

    repo.save_news({"source": "test", "title": "Fed raises rates",
                    "url": "https://x.com/1", "importance": "HIGH",
                    "currencies": ["USD"]})

    repo.log_event("agent_started")
    repo.close()
    return path


def _make_client(db_path=None):
    """Create test client. If db_path, override server's global repo."""
    import app.dashboard.server as srv

    app = srv.create_app({"DATABASE": db_path} if db_path else None)
    app.config["TESTING"] = True
    return app.test_client()


class TestDashboardAPI:
    def test_index_page(self):
        client = _make_client()
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"ai-trader" in resp.data

    def test_api_overview(self):
        client = _make_client()
        resp = client.get("/api/overview")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "account" in data
        assert "token_usage" in data

    def test_api_positions(self):
        client = _make_client()
        resp = client.get("/api/positions")
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_api_trades(self):
        client = _make_client()
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_api_performance(self):
        client = _make_client()
        resp = client.get("/api/performance")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "summary" in data
        assert "daily" in data

    def test_api_thesis(self):
        client = _make_client()
        resp = client.get("/api/thesis")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, dict)
        for sym in ["EURUSD", "GBPUSD", "USDJPY"]:
            assert sym in data

    def test_api_news(self):
        client = _make_client()
        resp = client.get("/api/news")
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_api_calendar(self):
        client = _make_client()
        resp = client.get("/api/calendar")
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_api_decisions(self):
        client = _make_client()
        resp = client.get("/api/decisions")
        assert resp.status_code == 200
        assert isinstance(json.loads(resp.data), list)

    def test_api_tokens(self):
        client = _make_client()
        resp = client.get("/api/tokens")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "usage" in data
        assert "by_task" in data


class TestDashboardWithData:
    def test_overview_shows_pnl(self):
        path = _seed_data()
        client = _make_client(db_path=path)
        resp = client.get("/api/overview")
        data = json.loads(resp.data)
        assert data["total_pnl"] == 51.0
        assert data["open_positions"] == 1
        import app.dashboard.server as srv
        srv._repo = None

    def test_performance_shows_trades(self):
        path = _seed_data()
        client = _make_client(db_path=path)
        resp = client.get("/api/performance")
        data = json.loads(resp.data)
        s = data["summary"]
        assert s["total_trades"] == 2
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["win_rate"] == 50.0
        import app.dashboard.server as srv
        srv._repo = None

    def test_thesis_shows_bias(self):
        path = _seed_data()
        client = _make_client(db_path=path)
        resp = client.get("/api/thesis")
        data = json.loads(resp.data)
        assert data["EURUSD"]["bias"] == "bullish"
        assert data["GBPUSD"]["bias"] == "bearish"
        import app.dashboard.server as srv
        srv._repo = None

    def test_thesis_no_data_symbols(self):
        path = _seed_data()
        client = _make_client(db_path=path)
        resp = client.get("/api/thesis")
        data = json.loads(resp.data)
        no_data = [s for s, t in data.items() if t["bias"] == "NO_DATA"]
        assert len(no_data) > 0
        import app.dashboard.server as srv
        srv._repo = None

    def test_trades_list(self):
        path = _seed_data()
        client = _make_client(db_path=path)
        resp = client.get("/api/trades")
        data = json.loads(resp.data)
        assert len(data) == 2
        import app.dashboard.server as srv
        srv._repo = None


class TestDashboardStatic:
    def test_css_served(self):
        client = _make_client()
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert b"--bg:" in resp.data
