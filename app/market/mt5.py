"""Market data access — MT5 real adapter (Windows) + Mock (dev/test).

The real adapter requires the MetaTrader5 python package (Windows only).
The mock generates deterministic synthetic OHLC data so the whole pipeline
can run and be tested anywhere.
"""
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}

BASE_PRICES = {
    "EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 151.50,
    "USDCHF": 0.8850, "AUDUSD": 0.6550, "USDCAD": 1.3650,
    "XAUUSD": 2350.0,
}

JPIP = {"USDJPY": 0.01}  # 3-decimal pairs


def decimals_for(symbol: str) -> int:
    return 3 if symbol in JPIP else 5


class Candle:
    __slots__ = ("time", "open", "high", "low", "close", "tick_volume")

    def __init__(self, time, o, h, l, c, v):
        self.time, self.open, self.high, self.low, self.close, self.tick_volume = time, o, h, l, c, v

    def to_dict(self):
        return {"time": self.time.isoformat(), "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "tick_volume": self.tick_volume}


class MarketDataFeed:
    """Common interface for real MT5 and mock feed."""

    def get_candles(self, symbol: str, timeframe: str, count: int):
        raise NotImplementedError

    def get_tick(self, symbol: str) -> dict:
        raise NotImplementedError

    def get_spread(self, symbol: str) -> float:
        raise NotImplementedError

    def get_account_info(self) -> dict:
        raise NotImplementedError

    def connected(self) -> bool:
        raise NotImplementedError


class MockFeed(MarketDataFeed):
    """Deterministic synthetic feed: seed per symbol so tests are reproducible."""

    NORMAL_SPREAD = {"EURUSD": 0.00012, "GBPUSD": 0.00018, "USDJPY": 0.015,
                     "USDCHF": 0.00015, "AUDUSD": 0.00014, "USDCAD": 0.00016}

    def __init__(self, seed: int = 42, stale: bool = False):
        self.seed = seed
        self.stale = stale
        self._cache = {}

    def _series(self, symbol: str, timeframe: str, count: int):
        tf_min = TIMEFRAME_MINUTES.get(timeframe)
        if tf_min is None:
            raise ValueError(f"unknown timeframe {timeframe}")
        key = (symbol, timeframe)
        rnd = random.Random(f"{self.seed}:{symbol}:{timeframe}")
        base = BASE_PRICES.get(symbol, 1.0)
        dec = decimals_for(symbol)
        vol = {"D1": 0.0060, "H4": 0.0030, "H1": 0.0012, "M15": 0.0005, "M5": 0.0003}[timeframe]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles = []
        price = base
        trend = 0.0
        for i in range(count, -1, -1):  # oldest -> newest, +1 live candle
            t = now - timedelta(minutes=tf_min * i)
            trend += rnd.uniform(-1, 1) * vol * 0.15
            trend = max(-vol * 2, min(vol * 2, trend))  # mean-ish drift
            o = price
            c = o + trend * o * rnd.uniform(0.2, 1.4)
            h = max(o, c) + abs(rnd.gauss(0, vol * 0.4)) * o
            l = min(o, c) - abs(rnd.gauss(0, vol * 0.4)) * o
            price = c
            candles.append(Candle(t, round(o, dec), round(h, dec), round(l, dec),
                                  round(c, dec), rnd.randint(50, 5000)))
        self._cache[key] = candles
        return candles

    def get_candles(self, symbol: str, timeframe: str, count: int):
        if self.stale:
            return None
        series = self._series(symbol, timeframe, max(count, 60))
        return [c.to_dict() for c in series[-count:]] if count < len(series) else \
            [c.to_dict() for c in series]

    def get_tick(self, symbol: str) -> dict:
        if self.stale:
            return None
        candles = self._series(symbol, "M5", 2)
        last = candles[-1]
        dec = decimals_for(symbol)
        spread = self.NORMAL_SPREAD.get(symbol, last.close * 0.00002)
        return {"symbol": symbol, "bid": last.close, "ask": round(last.close + spread, dec),
                "time": last.time.isoformat()}

    def get_spread(self, symbol: str) -> float:
        tick = self.get_tick(symbol)
        return round(tick["ask"] - tick["bid"], 8) if tick else None

    def get_account_info(self) -> dict:
        return {"login": 0, "balance": 10000.0, "equity": 10000.0,
                "currency": "USD", "leverage": 100, "margin_free": 10000.0}

    def connected(self) -> bool:
        return not self.stale


class MT5Feed(MarketDataFeed):
    """Real MetaTrader5 adapter (Windows). Import is deferred so this file is
    importable on Linux for tests."""

    def __init__(self):
        try:
            import MetaTrader5 as mt5  # noqa: F401
            self.mt5 = mt5
        except ImportError:
            raise RuntimeError(
                "MetaTrader5 package not available. Run on Windows: pip install MetaTrader5")
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    def get_candles(self, symbol: str, timeframe: str, count: int):
        tf = getattr(self.mt5, f"TIMEFRAME_{timeframe}", None)
        if tf is None:
            raise ValueError(f"unknown timeframe {timeframe}")
        rates = self.mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        return [{"time": datetime.fromtimestamp(r["time"], tz=timezone.utc).isoformat(),
                 "open": r["open"], "high": r["high"], "low": r["low"],
                 "close": r["close"], "tick_volume": r["tick_volume"]} for r in rates]

    def get_tick(self, symbol: str) -> dict:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return {"symbol": symbol, "bid": t.bid, "ask": t.ask,
                "time": datetime.fromtimestamp(t.time, tz=timezone.utc).isoformat()}

    def get_spread(self, symbol: str) -> float:
        info = self.mt5.symbol_info(symbol)
        tick = self.get_tick(symbol)
        if info is None or tick is None:
            return None
        return tick.ask - tick.bid

    def get_account_info(self) -> dict:
        acc = self.mt5.account_info()
        if acc is None:
            return None
        return {"login": acc.login, "balance": acc.balance, "equity": acc.equity,
                "currency": acc.currency, "leverage": acc.leverage,
                "margin_free": acc.margin_free}

    def connected(self) -> bool:
        return self.mt5.terminal_info() is not None


def create_feed(kind: str = "mock", **kw) -> MarketDataFeed:
    if kind == "mock":
        return MockFeed(**kw)
    if kind == "mt5":
        return MT5Feed()
    raise ValueError(f"unknown feed kind {kind}")
