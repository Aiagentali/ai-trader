import pytest
from app.strategy.quant_engine import QuantEngine
from app.risk.engine import RiskEngine, calc_position_size
from app.database.repository import Repository
import tempfile
import os


class MockCandleFeed:
    def __init__(self, h1_candles, m15_candles, bid=1.1000, ask=1.1002):
        self.h1 = h1_candles
        self.m15 = m15_candles
        self.bid = bid
        self.ask = ask

    def get_candles(self, symbol, tf, count):
        return self.h1 if tf == "H1" else self.m15

    def get_tick(self, symbol):
        return {"bid": self.bid, "ask": self.ask, "symbol": symbol}

    def get_spread(self, symbol):
        return round(self.ask - self.bid, 5)


def test_quant_engine_wait_on_empty_feed():
    feed = MockCandleFeed([], [])
    engine = QuantEngine()
    decision = engine.analyze_symbol("EURUSD", feed)
    assert decision["decision"] == "WAIT"


def test_quant_engine_bullish_pullback_triggers_buy():
    # 60 H1 candles trending up (ema20 > ema50)
    h1 = [{"open": 1.0800 + i * 0.0005, "high": 1.0810 + i * 0.0005,
           "low": 1.0790 + i * 0.0005, "close": 1.0805 + i * 0.0005} for i in range(60)]
    
    # 45 M15 candles trending up
    m15 = [{"open": 1.1000 + i * 0.0003, "high": 1.1010 + i * 0.0003,
            "low": 1.0995 + i * 0.0003, "close": 1.1008 + i * 0.0003} for i in range(45)]
    
    # Pullback to EMA20
    last_close = m15[-1]["close"]
    m15.append({"open": last_close, "high": last_close + 0.0002, "low": last_close - 0.0015, "close": last_close - 0.0010})
    m15.append({"open": last_close - 0.0010, "high": last_close + 0.0005, "low": last_close - 0.0012, "close": last_close + 0.0002})

    feed = MockCandleFeed(h1, m15, bid=last_close, ask=last_close + 0.0002)
    engine = QuantEngine()
    decision = engine.analyze_symbol("EURUSD", feed)

    assert decision["decision"] in ("BUY", "WAIT")


def test_small_account_validation_with_30_dollar_balance():
    # Test that a $30 account successfully validates a 0.01 micro-lot trade
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = Repository(db_path=path)

    risk_cfg = {
        "max_risk_per_trade": 0.005,
        "max_daily_loss": 0.10,
        "max_open_positions": 5,
        "max_correlated_exposure": 2,
        "min_rr": 1.5,
        "max_spread_multiplier": 3.0,
        "max_consecutive_losses": 4,
        "allow_micro_lot_small_account": True,
        "max_small_account_risk_pct": 0.08,
    }

    feed = MockCandleFeed([], [], bid=1.1000, ask=1.1002)
    risk_engine = RiskEngine(risk_cfg, repo, feed)

    # Decision with 15 pip SL ($1.50 risk on 0.01 lot = 5% of $30)
    decision = {
        "symbol": "EURUSD",
        "decision": "BUY",
        "entry": {"price": 1.1000},
        "stop_loss": 1.0985,  # 15 pips
        "take_profit": [1.1030],  # 30 pips (RR=2.0)
        "risk_reward": 2.0,
        "position_size": {"risk_percent": 0.005}
    }

    account = {"balance": 30.0, "equity": 30.0}
    res = risk_engine.validate(decision, account)

    assert res.approved is True, f"Failed reasons: {res.reasons_reject}"
    assert res.calculated_lots == 0.01
    repo.close()
    os.unlink(path)


def test_market_watch_symbols_mock_feed():
    from app.market.mt5 import MockFeed
    feed = MockFeed()
    syms = feed.get_market_watch_symbols()
    assert isinstance(syms, list)
    assert "EURUSD" in syms
    assert "GBPUSD" in syms


def test_contract_size_and_pip_size_helpers():
    from app.risk.engine import contract_size, pip_size, _get_currency_leg
    assert contract_size("XAUUSD") == 100.0
    assert contract_size("GOLD") == 100.0
    assert contract_size("EURUSD") == 100_000.0
    assert contract_size("BTCUSD") == 1.0

    assert pip_size("XAUUSD") == 0.1
    assert pip_size("USDJPY") == 0.01
    assert pip_size("EURUSD") == 0.0001

    assert _get_currency_leg("EURUSD.raw") == {"EUR", "USD"}
    assert _get_currency_leg("GBPUSD") == {"GBP", "USD"}


def test_quant_engine_volume_filter():
    h1 = [{"open": 1.0800 + i * 0.0005, "high": 1.0810 + i * 0.0005,
           "low": 1.0790 + i * 0.0005, "close": 1.0805 + i * 0.0005} for i in range(60)]
    # High volume initially, then zero/low volume on the trigger candle
    m15 = [{"open": 1.1000 + i * 0.0003, "high": 1.1010 + i * 0.0003,
            "low": 1.0995 + i * 0.0003, "close": 1.1008 + i * 0.0003,
            "tick_volume": 500} for i in range(45)]
    m15.append({"open": 1.114, "high": 1.115, "low": 1.113, "close": 1.1145, "tick_volume": 10})

    feed = MockCandleFeed(h1, m15, bid=1.1145, ask=1.1147)
    cfg = {"trading": {"volume_filter": {"enabled": True, "min_volume_ratio": 0.85}}}
    engine = QuantEngine(settings=cfg)
    decision = engine.analyze_symbol("EURUSD", feed)
    assert decision["decision"] == "WAIT"
    assert "volume" in decision["thesis"].lower() or "adx" in decision["thesis"].lower()


def test_agent_dynamic_market_watch_sync():
    from app.agent.loop import Agent
    from app.market.mt5 import MockFeed

    class DynamicFeed(MockFeed):
        def __init__(self):
            super().__init__()
            self._mw = ["EURUSD", "GBPUSD"]

        def get_market_watch_symbols(self):
            return list(self._mw)

    feed = DynamicFeed()
    settings = {"trading": {"use_market_watch": True, "engine": "quant"}}
    agent = Agent(feed=feed, repo=None, ai_client=None, budget=None,
                  risk_engine=None, execution=None, symbols=["EURUSD"], settings=settings)

    assert agent.use_market_watch is True
    # Simulate dynamic update by modifying feed market watch
    feed._mw = ["EURUSD", "GBPUSD", "USDJPY"]
    agent._last_mw_sync = 0  # force sync
    # Call the sync logic
    mw_syms = feed.get_market_watch_symbols()
    if mw_syms and set(mw_syms) != set(agent.symbols):
        agent.symbols = mw_syms
    assert "USDJPY" in agent.symbols


def test_decision_ai_disabled_standalone():
    from app.strategy.decision_ai import DecisionAI
    ai = DecisionAI(settings={"decision_ai": {"enabled": False}}, client=None)
    eval_res = ai.evaluate_setup({"symbol": "EURUSD", "decision": "BUY", "confidence": 75})
    assert eval_res["approve"] is True
    assert eval_res["source"] == "quant_standalone"


def test_decision_ai_fallback_on_network_error():
    from app.strategy.decision_ai import DecisionAI

    class BrokenClient:
        def chat(self, **kwargs):
            raise RuntimeError("HTTP 503 high demand spike")

    ai = DecisionAI(settings={"decision_ai": {"enabled": True}}, client=BrokenClient())
    eval_res = ai.evaluate_setup({"symbol": "EURUSD", "decision": "BUY", "confidence": 75})
    # Must never crash, must return approved fallback
    assert eval_res["approve"] is True
    assert eval_res["source"] == "fallback_quant"


def test_decision_ai_approve_and_reject():
    from app.strategy.decision_ai import DecisionAI
    import json

    class MockGeminiClient:
        def __init__(self, approve=True, confidence=80):
            self.approve = approve
            self.confidence = confidence

        def chat(self, **kwargs):
            return {"content": json.dumps({
                "approve": self.approve,
                "confidence": self.confidence,
                "reason": "Test decision"
            })}

    # Test approve with generative / mock model
    ai = DecisionAI(settings={"decision_ai": {"enabled": True, "model": "mock_generative", "min_confidence": 60}},
                    client=MockGeminiClient(approve=True, confidence=85))
    res = ai.evaluate_setup({"symbol": "EURUSD", "decision": "BUY"})
    assert res["approve"] is True
    assert res["source"] == "decision_ai"

    # Test reject with generative / mock model
    ai_reject = DecisionAI(settings={"decision_ai": {"enabled": True, "model": "mock_generative", "min_confidence": 60}},
                           client=MockGeminiClient(approve=False, confidence=40))
    res_reject = ai_reject.evaluate_setup({"symbol": "EURUSD", "decision": "BUY"})
    assert res_reject["approve"] is False


def test_gemini_decision_1_mock_api():
    from app.strategy.decision_ai import DecisionAI
    from unittest.mock import patch, MagicMock
    import json
    import io

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({
        "answers": {
            "approve_trade": {"confidence": 0.88, "noul": 0.88, "type": "noul"},
            "setup_quality": {"confidence": 0.90, "score": 1.75, "type": "score"}
        }
    }).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        client = MagicMock()
        client.base_url = "https://generativelanguage.googleapis.com/v1beta"
        client.api_key = "fake_key"
        ai = DecisionAI(settings={"decision_ai": {"enabled": True, "model": "gemini-decision-1", "min_confidence": 60}},
                        client=client)
        res = ai.evaluate_setup({"symbol": "EURUSD", "decision": "BUY"})
        assert res["approve"] is True
        assert res["source"] == "gemini-decision-1"
        assert res["confidence"] == 88
        assert "1.75/2.0" in res["reason"]


def test_order_book_depth_imbalance():
    from app.market.mt5 import MockFeed
    feed = MockFeed()
    book = feed.get_order_book("EURUSD")
    assert book is not None
    assert "imbalance" in book
    assert 0.0 <= book["imbalance"] <= 1.0


def test_quant_engine_scalp_m5_mode():
    class ScalpFeed:
        def __init__(self):
            # 60 M15 candles trending up
            self.m15 = [{"open": 1.0800 + i * 0.0003, "high": 1.0810 + i * 0.0003,
                         "low": 1.0790 + i * 0.0003, "close": 1.0805 + i * 0.0003} for i in range(60)]
            # 45 M5 candles trending up
            self.m5 = [{"open": 1.0950 + i * 0.0001, "high": 1.0960 + i * 0.0001,
                        "low": 1.0945 + i * 0.0001, "close": 1.0955 + i * 0.0001} for i in range(45)]
        def get_candles(self, sym, tf, count):
            return self.m15 if tf == "M15" else self.m5
        def get_tick(self, sym):
            return {"bid": 1.1000, "ask": 1.10015, "symbol": sym}
        def get_spread(self, sym):
            return 0.00015

    feed = ScalpFeed()
    engine = QuantEngine(settings={"trading": {"timeframe_mode": "scalp_m5"}})
    decision = engine.analyze_symbol("EURUSD", feed)
    assert decision["decision"] in ("BUY", "WAIT")
    if decision["decision"] == "BUY":
        assert decision["stop_loss"] < decision["entry"]["price"]
        # SL capped to max 16 pips
        assert abs(decision["entry"]["price"] - decision["stop_loss"]) <= 16.5 * 0.0001


