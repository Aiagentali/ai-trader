"""Regression tests for crypto spread baseline, weekend filter, pipeline audit, and loss post-mortem."""
from datetime import datetime, timezone
from app.risk.engine import RiskEngine, is_crypto_symbol, is_market_open
from app.strategy.quant_engine import QuantEngine
from app.database.repository import Repository


class _DummyCryptoFeed:
    def get_tick(self, symbol):
        return {"bid": 90000.0, "ask": 90035.0, "symbol": symbol}

    def get_spread(self, symbol):
        return 35.0


def test_crypto_spread_not_falsely_rejected(tmp_path):
    cfg = {
        "max_risk_per_trade": 0.01,
        "max_daily_loss": 0.05,
        "max_open_positions": 5,
        "max_correlated_exposure": 2,
        "max_spread_multiplier": 3.0,
        "min_rr": 1.5,
        "allow_micro_lot_small_account": True,
        "max_small_account_risk_pct": 0.15,
    }
    repo = Repository(str(tmp_path / "test.db"))
    risk = RiskEngine(cfg, repo, _DummyCryptoFeed())
    decision = {
        "symbol": "BTCUSD",
        "decision": "BUY",
        "entry": {"price": 90035.0},
        "stop_loss": 89700.0,
        "take_profit": [90650.0],
        "position_size": {"risk_percent": 0.01},
    }
    res = risk.validate(decision, {"balance": 100.0})
    assert res.approved is True, f"BTCUSD with $35 spread should pass, got: {res.reasons_reject}"


def test_weekend_market_open_filter():
    saturday = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)
    sunday_early = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    wednesday = datetime(2026, 11, 25, 14, 0, tzinfo=timezone.utc)

    assert is_crypto_symbol("BTCUSD") is True
    assert is_crypto_symbol("ETHUSD") is True
    assert is_crypto_symbol("EURUSD") is False

    assert is_market_open("BTCUSD", saturday) is True
    assert is_market_open("ETHUSD", sunday_early) is True
    assert is_market_open("EURUSD", saturday) is False
    assert is_market_open("XAUUSD", sunday_early) is False
    assert is_market_open("EURUSD", wednesday) is True


def test_quant_pipeline_stages_and_timestamps():
    from app.market.mt5 import MockFeed
    feed = MockFeed()
    qe = QuantEngine(settings={"trading": {"timeframe_mode": "standard_m15", "volume_filter": {"enabled": True}}})
    res = qe.analyze_symbol("EURUSD", feed)
    assert res["candle_time"]
    assert res["analyzed_at"]
    assert res["persian_reason"]
    assert isinstance(res["pipeline_stages"], list)
    assert len(res["pipeline_stages"]) >= 3
    assert res["pipeline_stages"][0]["key"] == "market"
