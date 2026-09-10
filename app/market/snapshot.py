"""Market snapshot builder — normalized multi-timeframe state per symbol."""
from app.market.indicators import sma, ema, rsi, atr, adx, swing_levels


def _structure(candles):
    """Classify basic structure from swing sequence (HH/HL, LH/LL...)."""
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    # simple: compare halves
    n = len(candles)
    if n < 20:
        return "insufficient_data"
    h1, h2 = max(highs[:n // 2]), max(highs[n // 2:])
    l1, l2 = min(lows[:n // 2]), min(lows[n // 2:])
    if h2 > h1 and l2 > l1:
        return "higher_highs_higher_lows"
    if h2 < h1 and l2 < l1:
        return "lower_highs_lower_lows"
    if h2 > h1 and l2 < l1:
        return "expanding_range"
    return "contracting_or_mixed"


def _tf_summary(candles):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    last = closes[-1]
    s = sma(closes, 20)
    e = ema(closes, 50)
    r = rsi(closes)
    a = atr(highs, lows, closes)
    ad = adx(highs, lows, closes)
    a_pct = (a / last * 100) if (a and last) else None
    return {
        "last_close": last,
        "sma20": round(s, 6) if s else None,
        "ema50": round(e, 6) if e else None,
        "rsi": round(r, 1) if r is not None else None,
        "atr": round(a, 6) if a else None,
        "atr_pct": round(a_pct, 3) if a_pct else None,
        "adx": round(ad, 1) if ad is not None else None,
        "structure": _structure(candles),
        "levels": swing_levels(candles),
        "last_candles": [{"o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]}
                         for c in candles[-5:]],
    }


def classify_regime(tf_h4: dict, tf_h1: dict) -> str:
    adx4, adx1 = tf_h4.get("adx"), tf_h1.get("adx")
    atrp = tf_h1.get("atr_pct") or 0
    st4 = tf_h4.get("structure")
    if adx4 is None:
        return "UNCLEAR"
    if atrp > 0.9:
        return "HIGH_VOLATILITY"
    if adx4 >= 25 or adx1 is not None and adx1 >= 30:
        if st4 == "higher_highs_higher_lows":
            return "TRENDING_UP"
        if st4 == "lower_highs_lower_lows":
            return "TRENDING_DOWN"
        return "BREAKOUT"
    if adx4 <= 18:
        return "RANGING"
    if atrp and atrp < 0.15:
        return "LOW_VOLATILITY"
    return "UNCLEAR"


def build_snapshot(feed, symbol: str, timeframes=None, candles_count: int = 120) -> dict:
    tfs = timeframes or ["D1", "H4", "H1", "M15"]
    tick = feed.get_tick(symbol)
    spread = feed.get_spread(symbol)
    out = {"symbol": symbol, "tick": tick, "spread": spread, "timeframes": {}}
    if tick is None or spread is None:
        out["error"] = "DATA_UNAVAILABLE"
        return out
    for tf in tfs:
        candles = feed.get_candles(symbol, tf, candles_count if tf != "D1" else 60)
        if not candles or len(candles) < 25:
            out["timeframes"][tf] = {"error": "insufficient_data"}
            continue
        out["timeframes"][tf] = _tf_summary(candles)
    h4, h1 = out["timeframes"].get("H4", {}), out["timeframes"].get("H1", {})
    if "last_close" in h4 and "last_close" in h1:
        out["regime"] = classify_regime(h4, h1)
    else:
        out["regime"] = "UNCLEAR"
    return out
