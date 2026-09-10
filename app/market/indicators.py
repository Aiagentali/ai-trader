"""Technical indicator calculations (pure python, no pandas — memory-limited host)."""
import math


def sma(values, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values, period: int):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period: int = 14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def atr(highs, lows, closes, period: int = 14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


def adx(highs, lows, closes, period: int = 14):
    if len(closes) < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [0.0], [0.0], []
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    def smooth(vals, p):
        s = sum(vals[:p])
        out = [s]
        for v in vals[p:]:
            s = s - s / p + v
            out.append(s)
        return out
    n = len(trs)
    tr_s, pdm_s, mdm_s = smooth(trs, period), smooth(plus_dm, period), smooth(minus_dm, period)
    dxs = []
    for i in range(len(tr_s)):
        trv, pv, mv = tr_s[i], pdm_s[i], mdm_s[i]
        if trv == 0:
            dxs.append(0.0)
            continue
        pdi, mdi = 100 * pv / trv, 100 * mv / trv
        denom = pdi + mdi
        dxs.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return None
    return sum(dxs[-period:]) / period


def swing_levels(candles, lookback: int = 100):
    """Rough S/R: recent swing highs/lows from fractal pivots."""
    data = candles[-lookback:]
    res, sup = [], []
    for i in range(2, len(data) - 2):
        if data[i]["high"] == max(data[i - 2]["high"], data[i - 1]["high"],
                                  data[i]["high"], data[i + 1]["high"],
                                  data[i + 2]["high"]):
            res.append(data[i]["high"])
        if data[i]["low"] == min(data[i - 2]["low"], data[i - 1]["low"],
                                 data[i]["low"], data[i + 1]["low"],
                                 data[i + 2]["low"]):
            sup.append(data[i]["low"])
    last = candles[-1]["close"]
    res_above = sorted([x for x in res if x > last])
    sup_below = sorted([x for x in sup if x < last], reverse=True)
    return {"resistance": res_above[:3], "support": sup_below[:3]}
