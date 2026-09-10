"""Phase 1–7: comprehensive test suite for ai-trader core modules."""
import json
import os
import sys
import tempfile

# Ensure app is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─── helpers ──────────────────────────────────────────────────────────────

def _tmp_repo():
    """Create a fresh Repository backed by a temp DB."""
    from app.database.repository import Repository
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = Repository(db_path=path)
    return r, path


def _mock_feed(seed=42, stale=False):
    from app.market.mt5 import MockFeed
    return MockFeed(seed=seed, stale=stale)


def _risk_config():
    return {
        "risk": {
            "max_risk_per_trade": 0.005,
            "max_daily_loss": 0.02,
            "max_open_positions": 5,
            "max_correlated_exposure": 2,
            "min_rr": 1.5,
            "max_spread_multiplier": 3.0,
            "max_drawdown": 0.10,
            "max_consecutive_losses": 4,
        }
    }


# ─── DB / Repository ─────────────────────────────────────────────────────

class TestDatabase:
    def test_init_creates_tables(self):
        repo, path = _tmp_repo()
        tables = [r[0] for r in repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        repo.close()
        os.unlink(path)
        expected = {"market_snapshots", "market_analysis", "market_thesis",
                    "news", "economic_events", "trade_decisions", "trades",
                    "trade_reviews", "positions", "ai_requests",
                    "token_usage", "system_events"}
        assert expected.issubset(set(tables)), f"missing: {expected - set(tables)}"

    def test_save_and_get_thesis(self):
        repo, path = _tmp_repo()
        repo.save_thesis("EURUSD", "bullish", "TRENDING_UP", "test thesis", "below 1.08", {})
        t = repo.latest_thesis("EURUSD")
        assert t is not None
        assert t["bias"] == "bullish"
        assert t["regime"] == "TRENDING_UP"
        hist = repo.thesis_history("EURUSD")
        assert len(hist) >= 1
        repo.close()
        os.unlink(path)

    def test_save_news_dedup(self):
        repo, path = _tmp_repo()
        item = {"source": "test", "title": "USD rallies on Fed", "url": "https://x.com/1",
                "published_ts": "2026-01-01T00:00:00Z", "currencies": ["USD"],
                "importance": "HIGH", "summary": "..."}
        assert repo.save_news(item) is True
        assert repo.save_news(item) is False  # duplicate
        news = repo.recent_news()
        assert len(news) == 1
        repo.close()
        os.unlink(path)

    def test_trade_lifecycle(self):
        repo, path = _tmp_repo()
        trade = {"trade_id": "T001", "opened_ts": "2026-01-01T00:00:00Z",
                 "symbol": "EURUSD", "direction": "BUY", "entry": 1.085,
                 "stop_loss": 1.082, "take_profit": "[1.091, 1.095]",
                 "position_size": 0.17, "risk_percent": 0.5,
                 "rr": 2.0, "market_regime": "TRENDING_UP",
                 "setup": "breakout_retest", "ai_confidence": 82,
                 "thesis": "bullish", "invalidation": "below 1.082",
                 "status": "open", "request_id": "R001"}
        repo.save_trade(trade)
        assert len(repo.open_trades()) == 1
        repo.close_trade("T001", 1.091, 102.0, 2.0, "take_profit_hit")
        assert len(repo.open_trades()) == 0
        t = repo.get_trade("T001")
        assert t["status"] == "closed"
        assert t["exit_reason"] == "take_profit_hit"
        repo.close()
        os.unlink(path)

    def test_positions(self):
        repo, path = _tmp_repo()
        pos = {"ticket": "SIM-1", "symbol": "EURUSD", "direction": "BUY",
               "volume": 0.17, "entry_price": 1.085, "stop_loss": 1.082,
               "take_profit": 1.091}
        repo.save_position(pos)
        assert len(repo.open_positions("EURUSD")) == 1
        repo.update_position("SIM-1", stop_loss=1.083)
        p = repo.open_positions("EURUSD")[0]
        assert p["stop_loss"] == 1.083
        repo.close_position("SIM-1")
        assert len(repo.open_positions()) == 0
        repo.close()
        os.unlink(path)

    def test_token_usage(self):
        repo, path = _tmp_repo()
        u = repo.token_usage_today()
        assert u["total_tokens"] == 0
        repo.add_token_usage(500, 200)
        u = repo.token_usage_today()
        assert u["total_tokens"] == 700
        assert u["request_count"] == 1
        repo.add_token_usage(300, 100)
        u = repo.token_usage_today()
        assert u["total_tokens"] == 1100
        assert u["request_count"] == 2
        repo.close()
        os.unlink(path)


# ─── MockFeed ─────────────────────────────────────────────────────────────

class TestMockFeed:
    def test_get_candles(self):
        feed = _mock_feed()
        candles = feed.get_candles("EURUSD", "H1", 50)
        assert len(candles) == 50
        for c in candles:
            assert all(k in c for k in ("time", "open", "high", "low", "close", "tick_volume"))
            assert c["high"] >= c["low"]
            assert c["high"] >= c["open"] and c["high"] >= c["close"]
            assert c["low"] <= c["open"] and c["low"] <= c["close"]

    def test_get_candles_stale(self):
        feed = _mock_feed(stale=True)
        assert feed.get_candles("EURUSD", "H1", 50) is None
        assert feed.get_tick("EURUSD") is None
        assert feed.get_spread("EURUSD") is None
        assert feed.connected() is False

    def test_get_tick(self):
        feed = _mock_feed()
        tick = feed.get_tick("EURUSD")
        assert tick["ask"] >= tick["bid"]
        assert tick["spread"] if "spread" in tick else True
        assert tick["symbol"] == "EURUSD"

    def test_get_account_info(self):
        feed = _mock_feed()
        acc = feed.get_account_info()
        assert acc["balance"] == 10000.0
        assert acc["currency"] == "USD"

    def test_all_major_pairs(self):
        feed = _mock_feed()
        for sym in ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD"]:
            tick = feed.get_tick(sym)
            assert tick is not None, f"{sym} tick failed"
            assert tick["bid"] > 0, f"{sym} bid <= 0"
            candles = feed.get_candles(sym, "H1", 20)
            assert len(candles) == 20, f"{sym} candle count wrong"

    def test_candle_consistency_all_tfs(self):
        feed = _mock_feed()
        for tf in ["M15", "H1", "H4", "D1"]:
            candles = feed.get_candles("EURUSD", tf, 30)
            assert len(candles) == 30, f"{tf} candle count"
            for c in candles:
                assert c["high"] >= c["low"]

    def test_invalid_timeframe(self):
        feed = _mock_feed()
        try:
            feed.get_candles("EURUSD", "X9", 10)
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_jpy_pair_decimals(self):
        feed = _mock_feed()
        for tf in ["H1", "H4"]:
            candles = feed.get_candles("USDJPY", tf, 5)
            for c in candles:
                # JPY pairs should have 2-3 decimals
                assert len(str(c["close"]).split(".")[-1]) <= 3


# ─── Indicators ───────────────────────────────────────────────────────────

class TestIndicators:
    def test_sma(self):
        from app.market.indicators import sma
        assert sma([1, 2, 3, 4, 5], 3) == 4.0
        assert sma([1, 2], 3) is None

    def test_ema(self):
        from app.market.indicators import ema
        vals = [float(i) for i in range(20)]
        result = ema(vals, 10)
        assert result is not None
        assert 8 < result < 20  # should track upward trend

    def test_rsi_bullish(self):
        from app.market.indicators import rsi
        vals = [float(i) for i in range(30)]  # strong uptrend
        result = rsi(vals)
        assert result is not None
        assert result > 50  # bullish

    def test_rsi_bearish(self):
        from app.market.indicators import rsi
        vals = [float(30 - i) for i in range(30)]  # strong downtrend
        result = rsi(vals)
        assert result is not None
        assert result < 50

    def test_atr(self):
        from app.market.indicators import atr
        h = [102.0 + i * 0.1 for i in range(30)]
        l = [101.0 + i * 0.1 for i in range(30)]
        c = [101.5 + i * 0.1 for i in range(30)]
        result = atr(h, l, c)
        assert result is not None
        assert result > 0

    def test_adx(self):
        from app.market.indicators import adx
        h = [100.0 + i * 0.5 for i in range(50)]
        l = [99.0 + i * 0.5 for i in range(50)]
        c = [99.5 + i * 0.5 for i in range(50)]
        result = adx(h, l, c)
        assert result is not None
        assert result > 0

    def test_swing_levels(self):
        from app.market.indicators import swing_levels
        # Create candles with a peak in the middle
        base = 1.0850
        candles = []
        for i in range(40):
            if i == 20:
                h = base + 0.003
            elif i == 15:
                l = base - 0.003
            else:
                h = base + 0.001
                l = base - 0.001
            candles.append({"high": h, "low": l, "close": base})
        levels = swing_levels(candles, lookback=40)
        assert "resistance" in levels
        assert "support" in levels


# ─── Market Snapshot ──────────────────────────────────────────────────────

class TestMarketSnapshot:
    def test_snapshot_structure(self):
        from app.market.snapshot import build_snapshot
        feed = _mock_feed()
        snap = build_snapshot(feed, "EURUSD")
        assert "symbol" in snap
        assert "tick" in snap
        assert "spread" in snap
        assert "timeframes" in snap
        assert "regime" in snap
        for tf in ["D1", "H4", "H1", "M15"]:
            assert tf in snap["timeframes"], f"missing {tf}"

    def test_snapshot_regime_classification(self):
        from app.market.snapshot import build_snapshot
        feed = _mock_feed()
        snap = build_snapshot(feed, "EURUSD")
        regime = snap.get("regime", "")
        assert regime in ("TRENDING_UP", "TRENDING_DOWN", "RANGING",
                          "HIGH_VOLATILITY", "LOW_VOLATILITY",
                          "BREAKOUT", "UNCLEAR", "TRANSITION")

    def test_snapshot_stale_feed(self):
        from app.market.snapshot import build_snapshot
        feed = _mock_feed(stale=True)
        snap = build_snapshot(feed, "EURUSD")
        assert snap.get("error") == "DATA_UNAVAILABLE"

    def test_snapshot_tf_summary_fields(self):
        from app.market.snapshot import build_snapshot
        feed = _mock_feed()
        snap = build_snapshot(feed, "GBPUSD")
        h1 = snap["timeframes"].get("H1", {})
        assert "last_close" in h1
        assert "rsi" in h1
        assert "adx" in h1
        assert "structure" in h1
        assert "levels" in h1


# ─── AI Schemas ───────────────────────────────────────────────────────────

class TestSchemas:
    def test_valid_wait(self):
        from app.ai.schemas import validate_decision, parse_json
        obj = {"symbol": "EURUSD", "decision": "WAIT", "confidence": 40,
               "market_regime": "RANGING", "setup": "none", "time_horizon": "intraday",
               "entry": {}, "stop_loss": 0, "take_profit": [], "position_size": {},
               "risk_reward": 0, "thesis": "unclear", "technical_analysis": "b",
               "price_action_analysis": "c", "fundamental_analysis": "d",
               "news_analysis": "e", "invalidation": "", "conditions": [], "risks": []}
        result = validate_decision(obj)
        assert result["decision"] == "WAIT"

    def test_valid_buy(self):
        from app.ai.schemas import validate_decision
        obj = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
               "market_regime": "TRENDING_UP", "setup": "breakout_retest",
               "time_horizon": "intraday",
               "entry": {"type": "MARKET", "price": 1.0850},
               "stop_loss": 1.0820,
               "take_profit": [1.0900, 1.0930],
               "position_size": {"requested_lots": 0.17},
               "risk_reward": 1.67,
               "thesis": "bullish", "technical_analysis": "HH HL",
               "price_action_analysis": "engulfing", "fundamental_analysis": "neutral",
               "news_analysis": "none", "invalidation": "below 1.0820",
               "conditions": [], "risks": []}
        result = validate_decision(obj)
        assert result["decision"] == "BUY"
        assert result["entry"]["price"] == 1.0850

    def test_buy_wrong_sl_side(self):
        from app.ai.schemas import validate_decision, SchemaError
        obj = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
               "market_regime": "TRENDING_UP", "setup": "test",
               "time_horizon": "intraday",
               "entry": {"type": "MARKET", "price": 1.0850},
               "stop_loss": 1.0860,  # WRONG: above entry for BUY
               "take_profit": [1.0900],
               "position_size": {"requested_lots": 0.17},
               "risk_reward": 1.0, "thesis": "t", "technical_analysis": "t",
               "price_action_analysis": "t", "fundamental_analysis": "t",
               "news_analysis": "t", "invalidation": "x",
               "conditions": [], "risks": []}
        try:
            validate_decision(obj)
            assert False, "should reject BUY with SL above entry"
        except SchemaError as e:
            assert "wrong side" in str(e)

    def test_sell_wrong_tp_side(self):
        from app.ai.schemas import validate_decision, SchemaError
        obj = {"symbol": "EURUSD", "decision": "SELL", "confidence": 75,
               "market_regime": "TRENDING_DOWN", "setup": "test",
               "time_horizon": "intraday",
               "entry": {"type": "MARKET", "price": 1.0850},
               "stop_loss": 1.0870,
               "take_profit": [1.0860],  # WRONG: above entry for SELL
               "position_size": {"requested_lots": 0.17},
               "risk_reward": 0.5, "thesis": "t", "technical_analysis": "t",
               "price_action_analysis": "t", "fundamental_analysis": "t",
               "news_analysis": "t", "invalidation": "x",
               "conditions": [], "risks": []}
        try:
            validate_decision(obj)
            assert False, "should reject SELL with TP above entry"
        except SchemaError as e:
            assert "wrong side" in str(e)

    def test_invalid_decision_value(self):
        from app.ai.schemas import validate_decision, SchemaError
        obj = {"symbol": "EURUSD", "decision": "YOLO", "confidence": 50}
        try:
            validate_decision(obj)
            assert False, "should reject invalid decision"
        except SchemaError as e:
            assert "invalid decision" in str(e)

    def test_parse_json_with_fences(self):
        from app.ai.schemas import parse_json
        raw = '```json\n{"a": 1}\n```'
        assert parse_json(raw) == {"a": 1}

    def test_parse_json_invalid(self):
        from app.ai.schemas import parse_json, SchemaError
        try:
            parse_json("this is not json at all {{{")
            assert False
        except SchemaError:
            pass


# ─── Token Budget ─────────────────────────────────────────────────────────

class TestTokenBudget:
    def test_usage_tracking(self):
        from app.ai.token_budget import TokenBudgetController
        repo, path = _tmp_repo()
        budget = TokenBudgetController(repo, daily_limit=10000)
        u = budget.usage()
        assert u["total_tokens"] == 0
        assert u["remaining"] == 10000
        budget.record("test", 500, 200)
        u = budget.usage()
        assert u["total_tokens"] == 700
        assert u["remaining"] == 9300
        repo.close()
        os.unlink(path)

    def test_budget_exhausted(self):
        from app.ai.token_budget import TokenBudgetController
        repo, path = _tmp_repo()
        budget = TokenBudgetController(repo, daily_limit=1000)
        budget.record("big_call", 950, 50)
        assert budget.check_and_allow("normal", "NORMAL") is False
        assert budget.check_and_allow("critical", "CRITICAL") is True  # uses reserve
        repo.close()
        os.unlink(path)

    def test_critical_bypasses_throttle(self):
        from app.ai.token_budget import TokenBudgetController
        repo, path = _tmp_repo()
        budget = TokenBudgetController(repo, daily_limit=1000)
        budget.record("fill", 890, 50)  # 94% used
        assert budget.check_and_allow("test", "NORMAL") is False  # >90% throttled
        assert budget.check_and_allow("test", "CRITICAL") is True
        repo.close()
        os.unlink(path)


# ─── Risk Engine ──────────────────────────────────────────────────────────

class TestRiskEngine:
    def _make_engine(self, feed=None, extra_open=0):
        from app.risk.engine import RiskEngine
        repo, path = _tmp_repo()
        feed = feed or _mock_feed()
        engine = RiskEngine(_risk_config(), repo, feed)
        return engine, repo, path

    def test_wait_passes(self):
        engine, repo, path = self._make_engine()
        acc = {"balance": 10000}
        decision = {"symbol": "EURUSD", "decision": "WAIT"}
        result = engine.validate(decision, acc)
        assert result.approved is True
        repo.close()
        os.unlink(path)

    def test_buy_approved(self):
        engine, repo, path = self._make_engine()
        acc = {"balance": 10000}
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
            "market_regime": "TRENDING_UP", "setup": "test",
            "thesis": "bullish", "invalidation": "below SL"
        }
        result = engine.validate(decision, acc)
        assert result.approved is True
        assert result.calculated_lots > 0
        assert result.risk_percent > 0
        repo.close()
        os.unlink(path)

    def test_reject_no_sl(self):
        engine, repo, path = self._make_engine()
        acc = {"balance": 10000}
        # SL=entry → geometry check rejects (distance 0)
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0850, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("geometry" in r.lower() for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_reject_bad_rr(self):
        engine, repo, path = self._make_engine()
        acc = {"balance": 10000}
        # SL and TP on correct sides, but RR too low (20:30 < 1.5)
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0870],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 0.67,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("RR" in r for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_reject_max_positions(self):
        engine, repo, path = self._make_engine()
        # fill up 5 positions
        for i in range(5):
            repo.save_position({"ticket": f"P{i}", "symbol": f"USDJPY",
                                "direction": "BUY", "volume": 0.01,
                                "entry_price": 151.5, "stop_loss": 151.0})
        acc = {"balance": 10000}
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("positions" in r.lower() for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_reject_correlated_exposure(self):
        engine, repo, path = self._make_engine()
        # BUY EURUSD + GBPUSD already open (both share USD)
        repo.save_position({"ticket": "P1", "symbol": "EURUSD",
                            "direction": "BUY", "volume": 0.01,
                            "entry_price": 1.085, "stop_loss": 1.082})
        repo.save_position({"ticket": "P2", "symbol": "GBPUSD",
                            "direction": "BUY", "volume": 0.01,
                            "entry_price": 1.27, "stop_loss": 1.267})
        acc = {"balance": 10000}
        decision = {
            "symbol": "USDCAD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.3650},
            "stop_loss": 1.3620, "take_profit": [1.3710],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("correlated" in r.lower() for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_reject_no_tp(self):
        engine, repo, path = self._make_engine()
        acc = {"balance": 10000}
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("TP" in r for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_position_size_calculation(self):
        from app.risk.engine import calc_position_size
        lots = calc_position_size(10000, 0.005, 1.0850, 1.0820, "EURUSD")
        # risk = 50, SL dist = 0.0030, per_lot = 300 => 50/300 = 0.1667 => floor to 0.16
        assert lots == 0.16

    def test_position_size_jpy(self):
        from app.risk.engine import calc_position_size
        lots = calc_position_size(10000, 0.005, 151.50, 151.00, "USDJPY")
        # risk = 50, SL dist = 0.5, per_lot = 50000 => 50/50000 = 0.001 => floor to 0.00
        assert lots == 0.00  # very tight SL on JPY pair with small account

    def test_rr_calculation(self):
        from app.risk.engine import calc_rr
        assert calc_rr("BUY", 1.0850, 1.0820, [1.0910]) == 2.0
        assert calc_rr("SELL", 1.0850, 1.0880, [1.0790]) == 2.0
        assert calc_rr("BUY", 1.0850, 1.0820, []) == 0.0

    def test_daily_pnl_check(self):
        engine, repo, path = self._make_engine()
        # simulate a day with big loss
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        repo.conn.execute(
            "INSERT INTO trades(trade_id, opened_ts, symbol, direction, entry, "
            "stop_loss, position_size, status, exit_ts, exit_price, profit_loss, "
            "r_multiple, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', "
            "?, ?, ?, ?, ?)",
            ("T_LOSS", "2026-01-01T00:00:00Z", "EURUSD", "BUY", 1.085, 1.082,
             0.17, f"{today}T12:00:00Z", 1.082, -255.0, -1.0, "stop_loss_hit"))
        repo.conn.commit()
        acc = {"balance": 10000}
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("daily loss" in r.lower() for r in result.reasons_reject)
        repo.close()
        os.unlink(path)

    def test_consecutive_losses_halt(self):
        engine, repo, path = self._make_engine()
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for i in range(4):
            repo.conn.execute(
                "INSERT INTO trades(trade_id, opened_ts, symbol, direction, entry, "
                "stop_loss, position_size, status, exit_ts, exit_price, profit_loss, "
                "r_multiple, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', "
                "?, ?, ?, ?, ?)",
                (f"T_CL{i}", "2026-01-01T00:00:00Z", "EURUSD", "BUY", 1.085, 1.082,
                 0.01, f"{today}T1{i}:00:00Z", 1.082, -30.0, -1.0, "stop_loss_hit"))
        repo.conn.commit()
        acc = {"balance": 10000}
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 80,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17}, "risk_reward": 2.0,
        }
        result = engine.validate(decision, acc)
        assert result.approved is False
        assert any("consecutive" in r.lower() for r in result.reasons_reject)
        repo.close()
        os.unlink(path)


# ─── Execution Engine (Simulation) ───────────────────────────────────────

class TestExecution:
    def test_place_order_simulation(self):
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        engine = ExecutionEngine(repo, feed, execution_mode="simulation")
        decision = {
            "symbol": "EURUSD", "decision": "BUY", "confidence": 82,
            "entry": {"type": "MARKET", "price": 1.0850},
            "stop_loss": 1.0820, "take_profit": [1.0910],
            "position_size": {"requested_lots": 0.17},
            "thesis": "test", "market_regime": "TRENDING_UP",
            "setup": "test", "invalidation": "below SL",
        }
        result = engine.place_order(decision)
        assert result["status"] == "EXECUTED_SIMULATED"
        assert result["ticket"].startswith("SIM-")
        assert len(repo.open_positions()) == 1
        assert len(repo.open_trades()) == 1
        repo.close()
        os.unlink(path)

    def test_reject_no_sl_simulation(self):
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        engine = ExecutionEngine(repo, feed, execution_mode="simulation")
        decision = {"symbol": "EURUSD", "decision": "BUY", "stop_loss": None,
                    "entry": {"price": 1.085}, "take_profit": [1.091]}
        result = engine.place_order(decision)
        assert result["status"] == "REJECTED"
        repo.close()
        os.unlink(path)

    def test_reject_live_trading_disabled(self):
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        engine = ExecutionEngine(repo, feed, execution_mode="live",
                                 live_trading_enabled=False)
        decision = {"symbol": "EURUSD", "decision": "BUY",
                    "entry": {"price": 1.085}, "stop_loss": 1.082,
                    "take_profit": [1.091], "stop_loss": 1.082}
        result = engine.place_order(decision)
        assert result["status"] == "REJECTED"
        assert result["reason"] == "live_trading_disabled"
        repo.close()
        os.unlink(path)

    def test_close_position(self):
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        engine = ExecutionEngine(repo, feed, execution_mode="simulation")
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
                    "entry": {"type": "MARKET", "price": 1.0850},
                    "stop_loss": 1.0820, "take_profit": [1.0910],
                    "position_size": {"requested_lots": 0.17},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "below SL"}
        engine.place_order(decision)
        pos = repo.open_positions()[0]
        result = engine.close_position(pos["ticket"], reason="manual")
        assert result["status"] == "CLOSED_SIMULATED"
        assert result["exit_price"] is not None
        assert len(repo.open_positions()) == 0
        repo.close()
        os.unlink(path)

    def test_check_sl_tp(self):
        from app.execution.engine import ExecutionEngine
        repo, path = _tmp_repo()
        feed = _mock_feed()
        engine = ExecutionEngine(repo, feed, execution_mode="simulation")
        # place a BUY at a very high price so SL gets hit at mock tick
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
                    "entry": {"type": "MARKET", "price": 1.2000},
                    "stop_loss": 1.2100,  # above entry = wrong side, but force SL hit
                    "take_profit": [1.2500],
                    "position_size": {"requested_lots": 0.17},
                    "thesis": "t", "market_regime": "TRENDING_UP",
                    "setup": "t", "invalidation": "t"}
        engine.place_order(decision)
        engine.check_sl_tp()
        # tick is around 1.085, SL is 1.2100 — SELL with SL 1.2100 should trigger
        # Actually, mock BUY at 1.20 with SL 1.21 — current bid ~1.085 < 1.20
        # Let me fix: place SELL at 1.085 with SL at 1.090
        decision2 = {"symbol": "GBPUSD", "decision": "SELL", "confidence": 82,
                     "entry": {"type": "MARKET", "price": 1.2700},
                     "stop_loss": 1.2750, "take_profit": [1.2600],
                     "position_size": {"requested_lots": 0.17},
                     "thesis": "t", "market_regime": "TRENDING_DOWN",
                     "setup": "t", "invalidation": "t"}
        engine.place_order(decision2)
        initial_count = len(repo.open_positions("GBPUSD"))
        engine.check_sl_tp()
        # current mock GBPUSD tick ~1.2700, SL=1.2750 — should NOT be hit
        assert len(repo.open_positions("GBPUSD")) == initial_count
        repo.close()
        os.unlink(path)


# ─── News Engine ──────────────────────────────────────────────────────────

class TestNews:
    def test_collect_pipeline(self):
        from app.news.rss import collect, dedupe, classify
        items = [
            {"title": "Fed raises rates", "url": "https://x.com/1",
             "summary": "USD strong, FOMC decision impacts dollar", "published_raw": None},
            {"title": "Fed raises rates", "url": "https://x.com/2",
             "summary": "duplicate", "published_raw": None},
            {"title": "Local weather event", "url": "https://x.com/3",
             "summary": "no currencies", "published_raw": None},
        ]
        # dedupe
        deduped = dedupe(items)
        assert len(deduped) == 2  # duplicate removed

        # classify
        classified = classify(items[0])
        assert "USD" in classified["currencies"]
        assert classified["importance"] == "HIGH"  # FOMC

        # filter
        order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        filtered = [it for it in [classified]
                    if order.get(it["importance"], 0) >= order.get("MEDIUM", 1)]
        assert len(filtered) == 1

    def test_date_parsing(self):
        from app.news.rss import parse_date
        assert parse_date(None) is None
        assert parse_date("") is None
        assert parse_date("2026-09-10T12:00:00+00:00") is not None
        assert parse_date("Wed, 10 Sep 2026 12:00:00 +0000") is not None


# ─── Economic Calendar ───────────────────────────────────────────────────

class TestCalendar:
    def test_fallback_events(self):
        from app.fundamentals.calendar import fetch_calendar
        events = fetch_calendar()
        assert len(events) >= 1
        assert any("USD" in e["currency"] for e in events)
        assert any(e["importance"] == "HIGH" for e in events)


# ─── Config ───────────────────────────────────────────────────────────────

class TestConfig:
    def test_load_settings(self):
        from app.config.config_loader import load_settings
        s = load_settings()
        assert s["trading"]["mode"] == "demo"
        assert s["trading"]["live_trading_enabled"] is False

    def test_load_symbols(self):
        from app.config.config_loader import load_symbols
        s = load_symbols()
        assert "EURUSD" in s["symbols"]
        assert s["symbols"]["EURUSD"]["enabled"] is True
        assert "XAUUSD" in s["future_symbols"]

    def test_load_risk(self):
        from app.config.config_loader import load_risk
        r = load_risk()
        assert r["risk"]["max_risk_per_trade"] == 0.005
        assert r["risk"]["max_daily_loss"] == 0.02


# ─── Integration: full pipeline without AI ────────────────────────────────

class TestIntegration:
    def test_full_tick_cycle(self):
        """Simulate one full agent tick without calling AI."""
        from app.database.repository import Repository
        from app.market.mt5 import MockFeed
        from app.market.snapshot import build_snapshot
        from app.risk.engine import RiskEngine
        from app.execution.engine import ExecutionEngine
        from app.config.config_loader import load_risk

        repo, path = _tmp_repo()
        feed = MockFeed(seed=77)
        risk_cfg = load_risk()
        risk_engine = RiskEngine(risk_cfg, repo, feed)
        execution = ExecutionEngine(repo, feed, "simulation")

        # 1. Build snapshot for EURUSD
        snap = build_snapshot(feed, "EURUSD")
        assert snap.get("regime") in ("TRENDING_UP", "TRENDING_DOWN", "RANGING",
                                      "HIGH_VOLATILITY", "LOW_VOLATILITY",
                                      "BREAKOUT", "UNCLEAR")

        # 2. Check SL/TP
        execution.check_sl_tp()

        # 3. Save a thesis
        repo.save_thesis("EURUSD", "bullish", "TRENDING_UP",
                         "H1 structure bullish", "below 1.08", snap)
        t = repo.latest_thesis("EURUSD")
        assert t["bias"] == "bullish"

        # 4. Simulate a trade
        decision = {"symbol": "EURUSD", "decision": "BUY", "confidence": 82,
                    "entry": {"type": "MARKET", "price": 1.0850},
                    "stop_loss": 1.0820, "take_profit": [1.0910],
                    "position_size": {"requested_lots": 0.17},
                    "thesis": "test", "market_regime": "TRENDING_UP",
                    "setup": "test", "invalidation": "below SL"}
        result = execution.place_order(decision)
        assert result["status"] == "EXECUTED_SIMULATED"

        # 5. Risk check on account
        acc = feed.get_account_info()
        risk_result = risk_engine.validate(decision, acc)
        assert risk_result.approved is True

        repo.close()
        os.unlink(path)
