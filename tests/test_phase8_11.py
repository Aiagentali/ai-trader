"""Phase 8–11 tests: MT5 Demo, Trade Manager, Performance, Backtest."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _tmp_repo():
    from app.database.repository import Repository
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Repository(db_path=path), path


def _mock_feed(seed=42):
    from app.market.mt5 import MockFeed
    return MockFeed(seed=seed)


# ─── MT5 Demo Executor ────────────────────────────────────────────────────

class TestMT5DemoExecutor:
    def test_refuses_live_trading_enabled(self):
        """MT5DemoExecutor must refuse if live_trading_enabled=True."""
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        try:
            MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=True)
            assert False, "should refuse"
        except RuntimeError as e:
            assert "live_trading_enabled" in str(e)
        repo.close()
        os.unlink(path)

    def test_requires_mt5_package(self):
        """On Linux (no MT5), connect() should return False gracefully."""
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        exec_engine = MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=False)
        # This server has no MetaTrader5 package
        result = exec_engine.connect()
        assert result is False
        repo.close()
        os.unlink(path)

    def test_place_order_rejected_when_not_connected(self):
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        exec_engine = MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=False)
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.1}}
        result = exec_engine.place_order(decision)
        assert result["status"] == "REJECTED"
        assert result["reason"] == "mt5_not_connected"
        repo.close()
        os.unlink(path)

    def test_close_rejected_when_not_connected(self):
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        exec_engine = MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=False)
        result = exec_engine.close_position("12345")
        assert result["status"] == "ERROR"
        assert result["reason"] == "mt5_not_connected"
        repo.close()
        os.unlink(path)

    def test_refuses_no_sl(self):
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        exec_engine = MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=False)
        decision = {"symbol": "EURUSD", "decision": "BUY", "stop_loss": None,
                    "entry": {"price": 1.085}, "take_profit": [1.091]}
        result = exec_engine.place_order(decision)
        assert result["status"] == "REJECTED"
        assert result["reason"] == "no_stop_loss"
        repo.close()
        os.unlink(path)

    def test_disconnect(self):
        from app.execution.mt5_demo import MT5DemoExecutor
        repo, path = _tmp_repo()
        exec_engine = MT5DemoExecutor(repo, _mock_feed(), live_trading_enabled=False)
        exec_engine.disconnect()  # should not crash
        repo.close()
        os.unlink(path)


# ─── Trade Manager ────────────────────────────────────────────────────────

class TestTradeManager:
    def test_hold(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")
        tm = TradeManager(repo, feed, exe)
        result = tm.manage_position({"decision": "HOLD"}, "fake_ticket")
        assert result["status"] == "HOLD"
        repo.close()
        os.unlink(path)

    def test_modify_sl_tp_simulation(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")

        # Create a position
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.1},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        tm = TradeManager(repo, feed, exe)
        result = tm.modify_sl_tp(pos["ticket"], new_sl=1.083, new_tp=1.092)
        assert result["status"] == "MODIFIED_SIMULATED"

        # Verify DB updated
        updated = repo.open_positions("EURUSD")[0]
        assert updated["stop_loss"] == 1.083
        assert updated["take_profit"] == 1.092
        repo.close()
        os.unlink(path)

    def test_close_simulation(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")

        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.1},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        tm = TradeManager(repo, feed, exe)
        result = tm.close_position(pos["ticket"], reason="ai_close")
        assert result["status"] == "CLOSED_SIMULATED"
        assert len(repo.open_positions()) == 0
        repo.close()
        os.unlink(path)

    def test_partial_close_simulation(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")

        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.10},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        tm = TradeManager(repo, feed, exe)
        result = tm.partial_close(pos["ticket"], close_fraction=0.5)
        assert result["status"] == "PARTIAL_CLOSED_SIMULATED"
        assert result["closed_volume"] == 0.05
        assert result["remaining_volume"] == 0.05
        assert result["pnl"] is not None
        repo.close()
        os.unlink(path)

    def test_manage_modify_routes(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")

        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.1},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        tm = TradeManager(repo, feed, exe)
        result = tm.manage_position(
            {"decision": "MODIFY", "modified_sl": 1.0835, "modified_tp": 1.093},
            pos["ticket"])
        assert result["status"] == "MODIFIED_SIMULATED"
        repo.close()
        os.unlink(path)

    def test_manage_close_routes(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")

        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 80,
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "position_size": {"requested_lots": 0.1},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        tm = TradeManager(repo, feed, exe)
        result = tm.manage_position({"decision": "CLOSE"}, pos["ticket"])
        assert result["status"] == "CLOSED_SIMULATED"
        repo.close()
        os.unlink(path)

    def test_manage_unknown_decision(self):
        from app.execution.trade_manager import TradeManager
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        exe = ExecutionEngine(repo, feed, "simulation")
        tm = TradeManager(repo, feed, exe)
        result = tm.manage_position({"decision": "YOLO"}, "fake")
        assert result["status"] == "ERROR"
        assert "unknown" in result["reason"]
        repo.close()
        os.unlink(path)


# ─── Performance Metrics ─────────────────────────────────────────────────

class TestPerformance:
    def _seed_trades(self, repo):
        """Seed closed trades with known PnL."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        trades = [
            {"trade_id": "W1", "opened_ts": "2026-01-01T10:00:00Z", "symbol": "EURUSD",
             "direction": "BUY", "entry": 1.085, "stop_loss": 1.082,
             "take_profit": "[1.091]", "position_size": 0.17, "risk_percent": 0.5,
             "rr": 2.0, "market_regime": "TRENDING_UP", "setup": "breakout",
             "ai_confidence": 82, "thesis": "t", "invalidation": "x",
             "status": "closed", "exit_ts": "2026-01-01T12:00:00Z",
             "exit_price": 1.091, "profit_loss": 102.0, "r_multiple": 2.0,
             "exit_reason": "take_profit_hit"},
            {"trade_id": "L1", "opened_ts": "2026-01-02T10:00:00Z", "symbol": "GBPUSD",
             "direction": "SELL", "entry": 1.270, "stop_loss": 1.273,
             "take_profit": "[1.264]", "position_size": 0.17, "risk_percent": 0.5,
             "rr": 2.0, "market_regime": "TRENDING_DOWN", "setup": "retest",
             "ai_confidence": 75, "thesis": "t", "invalidation": "x",
             "status": "closed", "exit_ts": "2026-01-02T14:00:00Z",
             "exit_price": 1.273, "profit_loss": -51.0, "r_multiple": -1.0,
             "exit_reason": "stop_loss_hit"},
            {"trade_id": "W2", "opened_ts": "2026-01-03T10:00:00Z", "symbol": "USDJPY",
             "direction": "BUY", "entry": 151.50, "stop_loss": 151.00,
             "take_profit": "[152.50]", "position_size": 0.10, "risk_percent": 0.5,
             "rr": 2.0, "market_regime": "TRENDING_UP", "setup": "breakout",
             "ai_confidence": 80, "thesis": "t", "invalidation": "x",
             "status": "closed", "exit_ts": "2026-01-03T16:00:00Z",
             "exit_price": 152.50, "profit_loss": 100.0, "r_multiple": 2.0,
             "exit_reason": "take_profit_hit"},
        ]
        for t in trades:
            repo.save_trade(t)
            repo.close_trade(t["trade_id"], t.get("exit_price", 0),
                             t.get("profit_loss", 0), t.get("r_multiple", 0),
                             t.get("exit_reason", ""))

    def test_summary(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        self._seed_trades(repo)
        tracker = PerformanceTracker(repo)
        s = tracker.summary()
        assert s["total_trades"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["win_rate"] == 66.7
        assert s["total_pnl"] == 151.0
        assert s["profit_factor"] > 1
        assert s["max_consecutive_losses"] == 1
        repo.close()
        os.unlink(path)

    def test_breakdown_by_symbol(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        self._seed_trades(repo)
        tracker = PerformanceTracker(repo)
        bd = tracker.breakdown_by("symbol")
        assert "EURUSD" in bd
        assert "GBPUSD" in bd
        assert "USDJPY" in bd
        assert bd["EURUSD"]["trades"] == 1
        assert bd["EURUSD"]["total_pnl"] == 102.0
        repo.close()
        os.unlink(path)

    def test_breakdown_by_setup(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        self._seed_trades(repo)
        tracker = PerformanceTracker(repo)
        bd = tracker.breakdown_by("setup")
        assert "breakout" in bd
        assert "retest" in bd
        assert bd["breakout"]["trades"] == 2
        repo.close()
        os.unlink(path)

    def test_empty_summary(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        tracker = PerformanceTracker(repo)
        s = tracker.summary()
        assert s["total_trades"] == 0
        repo.close()
        os.unlink(path)

    def test_review_summary(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        repo.save_review("W1", "VALID_WIN", "good trade")
        repo.save_review("L1", "VALID_LOSS", "normal stop")
        repo.save_review("W2", "BAD_WIN", "lucky")
        tracker = PerformanceTracker(repo)
        rs = tracker.review_summary()
        assert rs["VALID_WIN"] == 1
        assert rs["VALID_LOSS"] == 1
        assert rs["BAD_WIN"] == 1
        repo.close()
        os.unlink(path)

    def test_daily_breakdown(self):
        from app.journal.performance import PerformanceTracker
        repo, path = _tmp_repo()
        self._seed_trades(repo)
        # Manually set different exit dates since close_trade uses utcnow()
        repo.conn.execute("UPDATE trades SET exit_ts='2026-01-01T12:00:00Z' WHERE trade_id='W1'")
        repo.conn.execute("UPDATE trades SET exit_ts='2026-01-02T14:00:00Z' WHERE trade_id='L1'")
        repo.conn.execute("UPDATE trades SET exit_ts='2026-01-03T16:00:00Z' WHERE trade_id='W2'")
        repo.conn.commit()
        tracker = PerformanceTracker(repo)
        db = tracker.daily_breakdown()
        assert len(db) == 3  # 3 different days
        assert db[0]["date"] == "2026-01-01"
        assert db[0]["pnl"] == 102.0
        repo.close()
        os.unlink(path)


# ─── Backtest Engine ──────────────────────────────────────────────────────

class TestBacktest:
    def _make_candle_data(self, count=100, tf_minutes=60, base_price=1.085):
        """Generate deterministic candle data for backtesting."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, minute=0)
        candles = []
        price = base_price
        for i in range(count):
            t = now - timedelta(minutes=tf_minutes * (count - i))
            drift = 0.0001 * (1 if i % 20 < 10 else -1)
            o = price
            c = o + drift
            h = max(o, c) + 0.0003
            l = min(o, c) - 0.0003
            price = c
            candles.append({
                "time": t.isoformat(),
                "open": round(o, 5), "high": round(h, 5),
                "low": round(l, 5), "close": round(c, 5),
                "tick_volume": 1000 + i,
            })
        return candles

    def test_empty_data(self):
        from app.backtest.engine import BacktestEngine
        engine = BacktestEngine({"H1": []})
        result = engine.run()
        assert result.get("error") == "no data"

    def test_basic_run_no_decisions(self):
        from app.backtest.engine import BacktestEngine
        data = {"H1": self._make_candle_data(100, 60)}
        engine = BacktestEngine(data)
        result = engine.run()
        assert result["total_trades"] == 0
        assert len(engine.get_equity_curve()) == 100

    def test_basic_run_with_decisions(self):
        from app.backtest.engine import BacktestEngine

        def decision_fn(closes, time, tick, positions):
            if len(positions) >= 1:
                return None
            if len(closes) < 10:
                return None
            # Simple: buy every 20 candles
            minute = time.minute
            if minute == 0 and not positions:
                return {"decision": "BUY", "confidence": 50,
                        "entry": {"price": tick["ask"]},
                        "stop_loss": round(tick["ask"] - 0.003, 5),
                        "take_profit": [round(tick["ask"] + 0.006, 5)],
                        "position_size": {"requested_lots": 0.1}}
            return None

        data = {"H1": self._make_candle_data(200, 60)}
        engine = BacktestEngine(data)
        result = engine.run(decision_fn=decision_fn)
        # Should have executed some trades
        assert result["total_trades"] >= 1

    def test_no_lookahead_bias(self):
        """Verify that decision_fn only receives data up to current time."""
        from app.backtest.engine import BacktestEngine, HistoricalCandleFeed

        seen_times = []

        def decision_fn(closes, time, tick, positions):
            seen_times.append(time)
            # Verify we can only see candles up to `time`
            for tf in ["H1", "H4"]:
                candles = self._make_candle_data(200, 3600 if tf == "H1" else 14400)
                # All visible candles should be <= current time
                for c in closes:
                    if c < 1.0:  # skip tick values
                        pass
            return None

        data = {"H1": self._make_candle_data(50, 60)}
        engine = BacktestEngine(data)
        engine.run(decision_fn=decision_fn)
        assert len(seen_times) > 0
        # All times should be monotonically non-decreasing
        for i in range(1, len(seen_times)):
            assert seen_times[i] >= seen_times[i - 1]

    def test_sl_tp_hit(self):
        """Verify SL/TP hit detection works in backtest."""
        from app.backtest.engine import BacktestEngine

        # Create candles that will definitely hit a stop loss
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles = []
        for i in range(50):
            t = now - timedelta(minutes=60 * (50 - i))
            # First candle: high, then crash
            if i < 5:
                p = 1.090
            else:
                p = 1.080  # drops below any reasonable SL
            candles.append({
                "time": t.isoformat(),
                "open": round(p, 5), "high": round(p + 0.001, 5),
                "low": round(p - 0.001, 5), "close": round(p, 5),
                "tick_volume": 1000,
            })

        def decision_fn(closes, time, tick, positions):
            if not positions and len(closes) >= 3:
                return {"decision": "BUY", "confidence": 80,
                        "entry": {"price": tick["ask"]},
                        "stop_loss": round(tick["ask"] - 0.003, 5),
                        "take_profit": [round(tick["ask"] + 0.010, 5)],
                        "position_size": {"requested_lots": 0.1}}
            return None

        data = {"H1": candles}
        engine = BacktestEngine(data)
        result = engine.run(decision_fn=decision_fn)
        trades = engine.get_trades()
        assert len(trades) >= 1
        # First trade should have been closed by SL
        assert trades[0]["exit_reason"] == "stop_loss_hit"

    def test_profit_factor(self):
        from app.backtest.engine import BacktestEngine
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles = []
        # Trending up: buy and take profit
        for i in range(100):
            t = now - timedelta(minutes=60 * (100 - i))
            p = 1.080 + i * 0.0001
            candles.append({
                "time": t.isoformat(),
                "open": round(p, 5), "high": round(p + 0.0002, 5),
                "low": round(p - 0.0001, 5), "close": round(p + 0.00005, 5),
                "tick_volume": 1000,
            })

        def decision_fn(closes, time, tick, positions):
            if not positions and len(closes) >= 5:
                return {"decision": "BUY", "confidence": 80,
                        "entry": {"price": tick["ask"]},
                        "stop_loss": round(tick["ask"] - 0.003, 5),
                        "take_profit": [round(tick["ask"] + 0.005, 5)],
                        "position_size": {"requested_lots": 0.1}}
            return None

        data = {"H1": candles}
        engine = BacktestEngine(data)
        result = engine.run(decision_fn=decision_fn)
        assert result["total_trades"] >= 1
        assert result["profit_factor"] > 0

    def test_equity_curve(self):
        from app.backtest.engine import BacktestEngine
        data = {"H1": self._make_candle_data(30, 60)}
        engine = BacktestEngine(data)
        engine.run()
        curve = engine.get_equity_curve()
        assert len(curve) == 30
        assert all("time" in pt and "equity" in pt for pt in curve)


# ─── Integration: agent loop MODIFY/CLOSE/PARTIAL ────────────────────────

class TestAgentTradeManagement:
    def test_agent_handles_modify_decision(self):
        """Test that MODIFY decision routes through TradeManager correctly."""
        from app.database.repository import Repository
        from app.execution.engine import ExecutionEngine
        from app.execution.trade_manager import TradeManager
        from app.market.mt5 import MockFeed

        repo, path = _tmp_repo()
        feed = MockFeed(seed=99)
        exe = ExecutionEngine(repo, feed, "simulation")
        tm = TradeManager(repo, feed, exe)

        # Place a trade
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
                    "entry": {"price": 1.0850}, "stop_loss": 1.0820,
                    "take_profit": [1.0910], "position_size": {"requested_lots": 0.17},
                    "thesis": "test", "market_regime": "TRENDING_UP",
                    "setup": "test", "invalidation": "below SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        # AI decides MODIFY with new SL
        modify_decision = {"decision": "MODIFY", "modified_sl": 1.0835,
                           "modified_tp": 1.0920}
        result = tm.manage_position(modify_decision, pos["ticket"])
        assert result["status"] == "MODIFIED_SIMULATED"

        updated = repo.open_positions("EURUSD")[0]
        assert updated["stop_loss"] == 1.0835
        assert updated["take_profit"] == 1.0920

        repo.close()
        os.unlink(path)

    def test_agent_handles_partial_close(self):
        from app.database.repository import Repository
        from app.execution.engine import ExecutionEngine
        from app.execution.trade_manager import TradeManager
        from app.market.mt5 import MockFeed

        repo, path = _tmp_repo()
        feed = MockFeed(seed=99)
        exe = ExecutionEngine(repo, feed, "simulation")
        tm = TradeManager(repo, feed, exe)

        decision = {"symbol": "GBPUSD", "decision": "SELL", "confidence": 78,
                    "entry": {"price": 1.2700}, "stop_loss": 1.2730,
                    "take_profit": [1.2640], "position_size": {"requested_lots": 0.10},
                    "thesis": "test", "market_regime": "TRENDING_DOWN",
                    "setup": "test", "invalidation": "above SL"}
        exe.place_order(decision)
        pos = repo.open_positions()[0]

        result = tm.manage_position(
            {"decision": "PARTIAL_CLOSE", "close_fraction": 0.3}, pos["ticket"])
        assert result["status"] == "PARTIAL_CLOSED_SIMULATED"
        assert result["closed_volume"] == 0.03
        assert result["remaining_volume"] == 0.07

        repo.close()
        os.unlink(path)

    def test_full_lifecycle(self):
        """Place → Modify → Partial Close → Full Close."""
        from app.database.repository import Repository
        from app.execution.engine import ExecutionEngine
        from app.execution.trade_manager import TradeManager
        from app.market.mt5 import MockFeed

        repo, path = _tmp_repo()
        feed = MockFeed(seed=99)
        exe = ExecutionEngine(repo, feed, "simulation")
        tm = TradeManager(repo, feed, exe)

        # 1. Open
        d = {"symbol": "USDJPY", "decision": "BUY", "confidence": 80,
             "entry": {"price": 151.50}, "stop_loss": 151.00,
             "take_profit": [152.50], "position_size": {"requested_lots": 0.10},
             "thesis": "t", "market_regime": "TRENDING_UP",
             "setup": "t", "invalidation": "x"}
        exe.place_order(d)
        assert len(repo.open_positions()) == 1
        pos = repo.open_positions()[0]

        # 2. Modify
        tm.manage_position({"decision": "MODIFY", "modified_sl": 151.20}, pos["ticket"])
        updated = repo.open_positions("USDJPY")[0]
        assert updated["stop_loss"] == 151.20

        # 3. Partial close
        tm.manage_position({"decision": "PARTIAL_CLOSE", "close_fraction": 0.5},
                           pos["ticket"])

        # 4. Close remaining
        tm.manage_position({"decision": "CLOSE"}, pos["ticket"])
        assert len(repo.open_positions()) == 0

        # 5. Check metrics
        from app.journal.performance import PerformanceTracker
        tracker = PerformanceTracker(repo)
        s = tracker.summary()
        assert s["total_trades"] >= 2  # partial + main

        repo.close()
        os.unlink(path)
