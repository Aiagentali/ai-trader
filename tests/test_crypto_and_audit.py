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


def test_two_strike_daily_loss_breaker_and_survival_floor(tmp_path):
    cfg = {
        "max_risk_per_trade": 0.005,
        "max_daily_loss": 0.02,
        "max_small_account_daily_loss": 0.12,
        "min_trading_balance": 12.0,
        "max_open_positions": 5,
        "max_correlated_exposure": 2,
        "max_spread_multiplier": 3.0,
        "min_rr": 1.5,
        "max_consecutive_losses": 2,
        "allow_micro_lot_small_account": True,
        "max_small_account_risk_pct": 0.08,
    }
    repo = Repository(str(tmp_path / "risk_test.db"))
    risk = RiskEngine(cfg, repo, _DummyCryptoFeed())

    # 1. Survival floor: $10 balance < $12 floor -> rejected
    dec = {
        "symbol": "BTCUSD",
        "decision": "BUY",
        "entry": {"price": 90035.0},
        "stop_loss": 89900.0,
        "take_profit": [90300.0],
        "position_size": {"risk_percent": 0.005},
    }
    res_low = risk.validate(dec, {"balance": 10.0})
    assert res_low.approved is False
    assert any("survival floor" in r.lower() for r in res_low.reasons_reject)

    # 2. Crypto does not collide with 2 open Forex USD positions
    repo.save_position({"ticket": "F1", "symbol": "EURUSD", "direction": "BUY", "volume": 0.01, "entry_price": 1.08, "stop_loss": 1.07, "take_profit": 1.10})
    repo.save_position({"ticket": "F2", "symbol": "GBPUSD", "direction": "BUY", "volume": 0.01, "entry_price": 1.25, "stop_loss": 1.24, "take_profit": 1.27})
    res_crypto = risk.validate(dec, {"balance": 30.0})
    assert res_crypto.approved is True, f"BTCUSD should not be blocked by Forex USD correlation: {res_crypto.reasons_reject}"

    # 3. Yesterday's losses do NOT halt today
    repo.conn.execute(
        "INSERT INTO trades(trade_id, opened_ts, symbol, direction, entry, stop_loss, position_size, status, exit_ts, exit_price, profit_loss, r_multiple, exit_reason) "
        "VALUES ('Y1', '2020-01-01T10:00:00Z', 'EURUSD', 'BUY', 1.08, 1.07, 0.01, 'closed', '2020-01-01T11:00:00Z', 1.07, -1.0, -1.0, 'sl'), "
        "('Y2', '2020-01-01T12:00:00Z', 'EURUSD', 'BUY', 1.08, 1.07, 0.01, 'closed', '2020-01-01T13:00:00Z', 1.07, -1.0, -1.0, 'sl')"
    )
    repo.conn.commit()
    assert risk.validate(dec, {"balance": 30.0}).approved is True

    # 4. 2 consecutive losses TODAY halt trading for today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo.conn.execute(
        "INSERT INTO trades(trade_id, opened_ts, symbol, direction, entry, stop_loss, position_size, status, exit_ts, exit_price, profit_loss, r_multiple, exit_reason) "
        "VALUES (?, ?, 'EURUSD', 'BUY', 1.08, 1.07, 0.01, 'closed', ?, 1.07, -0.9, -1.0, 'sl'), "
        "(?, ?, 'GBPUSD', 'BUY', 1.25, 1.24, 0.01, 'closed', ?, 1.24, -0.9, -1.0, 'sl')",
        ("T1", f"{today}T08:00:00Z", f"{today}T09:00:00Z", "T2", f"{today}T10:00:00Z", f"{today}T11:00:00Z")
    )
    repo.conn.commit()
    res_halt = risk.validate(dec, {"balance": 30.0})
    assert res_halt.approved is False
    assert any("consecutive" in r.lower() for r in res_halt.reasons_reject)
