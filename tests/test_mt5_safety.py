"""MT5 order construction: sl/tp must be absolute price levels, not point counts.

Regression for a real defect: place_order sent sl/tp as point distances, so a
BUY at 1.0851 with SL 1.0820 shipped sl=310 instead of sl=1.0820 — the demo
terminal would place a stop far outside the chart. Also covers the wrong-side
refusals and the MODIFY path.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

POINT = 0.00001
DIGITS = 5


class FakeMT5:
    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1

    def __init__(self, bid=1.0850, ask=1.0851):
        self.bid, self.ask = bid, ask
        self.sent = []

    def symbol_info_tick(self, _symbol):
        return types.SimpleNamespace(bid=self.bid, ask=self.ask)

    def symbol_info(self, _symbol):
        return types.SimpleNamespace(point=POINT, digits=DIGITS)

    def order_send(self, req):
        self.sent.append(req)
        return types.SimpleNamespace(order=555, price=req.get("price"), retcode=10009,
                                     comment="ok")


def _executor(tmpdir):
    from app.database.repository import Repository
    from app.execution.mt5_demo import MT5DemoExecutor
    fd, path = tempfile.mkstemp(suffix=".db", dir=tmpdir)
    os.close(fd)
    repo = Repository(db_path=path)
    exe = MT5DemoExecutor(repo, None, live_trading_enabled=False)
    fake = FakeMT5()
    exe._mt5 = fake
    exe._connected = True
    return repo, exe, fake, path


def _buy(**over):
    decision = {"symbol": "EURUSD", "decision": "BUY",
                "entry": {"type": "MARKET", "price": 1.0851},
                "stop_loss": 1.0820, "take_profit": [1.0910],
                "position_size": {"requested_lots": 0.01}}
    decision.update(over)
    return decision


def _sell(**over):
    decision = {"symbol": "EURUSD", "decision": "SELL",
                "entry": {"type": "MARKET", "price": 1.0850},
                "stop_loss": 1.0880, "take_profit": [1.0790],
                "position_size": {"requested_lots": 0.01}}
    decision.update(over)
    return decision


def test_buy_sends_absolute_price_levels_not_point_distances(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        assert exe.place_order(_buy())["status"] == "EXECUTED_MT5_DEMO"
        sent = fake.sent[0]
        assert sent["sl"] == 1.0820, f"sl must be a price level, got {sent['sl']}"
        assert sent["tp"] == 1.0910, f"tp must be a price level, got {sent['tp']}"
        assert sent["price"] == 1.0851
        # the point-distance value is 310 and must never reappear
        assert sent["sl"] != round((1.0851 - 1.0820) / POINT)
    finally:
        repo.close()
        os.unlink(path)


def test_sell_sends_absolute_price_levels(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        assert exe.place_order(_sell())["status"] == "EXECUTED_MT5_DEMO"
        sent = fake.sent[0]
        assert sent["sl"] == 1.0880
        assert sent["tp"] == 1.0790
    finally:
        repo.close()
        os.unlink(path)


def test_levels_snap_to_the_symbol_tick_grid(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        exe.place_order(_buy(stop_loss=1.082000043))
        sl = fake.sent[0]["sl"]
        assert sl == 1.082, f"un-snapped level {sl!r}"
    finally:
        repo.close()
        os.unlink(path)


def test_buy_with_stop_above_price_is_refused(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        result = exe.place_order(_buy(stop_loss=1.0900))
        assert result["status"] == "REJECTED"
        assert result["reason"] == "sl_wrong_side"
        assert not fake.sent, "no order may reach MT5 with a wrong-side stop"
        assert not repo.open_trades()
    finally:
        repo.close()
        os.unlink(path)


def test_sell_with_stop_below_price_is_refused(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        result = exe.place_order(_sell(stop_loss=1.0800))
        assert result["status"] == "REJECTED"
        assert result["reason"] == "sl_wrong_side"
        assert not fake.sent
    finally:
        repo.close()
        os.unlink(path)


def test_take_profit_on_the_wrong_side_is_refused(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        assert exe.place_order(_buy(take_profit=[1.0800]))["reason"] == "tp_wrong_side"
        assert exe.place_order(_sell(take_profit=[1.0900]))["reason"] == "tp_wrong_side"
        assert not fake.sent
    finally:
        repo.close()
        os.unlink(path)


def test_missing_symbol_info_refuses_the_order(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        fake.symbol_info = lambda _s: None
        assert exe.place_order(_buy())["reason"] == "no_symbol_info"
        assert not fake.sent
    finally:
        repo.close()
        os.unlink(path)


def test_modify_sends_price_levels_not_divisions(tmp_path):
    repo, exe, fake, path = _executor(str(tmp_path))
    try:
        exe.place_order(_buy())
        ticket = repo.open_positions()[0]["ticket"]
        assert exe.modify_position(ticket, new_sl=1.0830, new_tp=1.0930)["status"] \
            == "MODIFIED_MT5_DEMO"
        modify = fake.sent[-1]
        assert modify["action"] == FakeMT5.TRADE_ACTION_SLTP
        assert modify["sl"] == 1.0830
        assert modify["tp"] == 1.0930
    finally:
        repo.close()
        os.unlink(path)
