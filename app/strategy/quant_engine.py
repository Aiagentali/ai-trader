"""Pure Quantitative Decision Engine — 100% mathematical, zero AI dependency.

Implements an institutional Trend-Pullback system:
1. Multi-Timeframe Trend Confirmation (H1 EMA 20 & EMA 50 alignment).
2. Trend Strength Filter (ADX > 20 to avoid choppy/sideways markets).
3. Dynamic Pullback Entry (M15 pullback to dynamic EMA support/resistance).
4. Structural Swing-based Stop Loss + 1.8R Take Profit.
5. Optional Session Filter (London & New York Sessions, Rollover block).
6. Optional Volume Filter (Tick volume confirmation).
"""
import math
from datetime import datetime, timezone
from app.market.indicators import sma, ema, rsi, atr, adx
from app.risk.engine import pip_size


class QuantEngine:
    """Mathematical trading engine that generates validated trade decisions without AI."""

    def __init__(self, settings: dict = None, min_rr: float = 1.8, learned_profiles: dict = None):
        self.settings = settings or {}
        self.min_rr = min_rr
        self.learned_profiles = learned_profiles if learned_profiles is not None else self._load_learned_profiles()

    def _load_learned_profiles(self) -> dict:
        try:
            from pathlib import Path
            import json
            p = Path("data/learned_profiles.json")
            if not p.exists():
                p = Path(__file__).resolve().parents[2] / "data" / "learned_profiles.json"
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def analyze_symbol(self, symbol: str, feed) -> dict:
        """Analyze a symbol using multi-timeframe candle data from feed."""
        profile = (self.learned_profiles or {}).get(symbol, {}).get("params", {})
        adx_min = profile.get("adx_min", 18.0)
        min_rr = profile.get("min_rr", self.min_rr)

        tf_mode = self.settings.get("trading", {}).get("timeframe_mode", "standard_m15")
        if tf_mode == "scalp_m5":
            trend_tf = "M15"
            entry_tf = "M5"
            max_sl_pips = 16.0
            atr_mult = 1.0
        else:
            trend_tf = "H1"
            entry_tf = "M15"
            max_sl_pips = 35.0
            atr_mult = 1.2

        candles_trend = feed.get_candles(symbol, trend_tf, 100) or []
        candles_entry = feed.get_candles(symbol, entry_tf, 80) or []
        tick = feed.get_tick(symbol)
        spread = feed.get_spread(symbol) or 0.00015

        # Fallback to H1/M15 if feed has insufficient candles for M5/M15 scalp
        if tf_mode == "scalp_m5" and (len(candles_trend) < 40 or len(candles_entry) < 35):
            trend_tf = "H1"
            entry_tf = "M15"
            max_sl_pips = 35.0
            atr_mult = 1.2
            candles_trend = feed.get_candles(symbol, trend_tf, 100) or []
            candles_entry = feed.get_candles(symbol, entry_tf, 80) or []

        if len(candles_trend) < 40 or len(candles_entry) < 35 or not tick:
            return {
                "symbol": symbol,
                "decision": "WAIT",
                "confidence": 0,
                "thesis": "Insufficient candle history or market tick unavailable",
                "market_regime": "UNCLEAR",
                "setup": "NONE",
            }

        # Session Filter (optional)
        session_cfg = self.settings.get("trading", {}).get("session_filter", {})
        if session_cfg.get("enabled", False):
            now_utc = datetime.now(timezone.utc)
            hour_utc = now_utc.hour + now_utc.minute / 60.0

            # Rollover block: 21:45 to 23:15 UTC (huge spreads, swap charges)
            if session_cfg.get("block_rollover", True) and (21.75 <= hour_utc <= 23.25):
                return {
                    "symbol": symbol,
                    "decision": "WAIT",
                    "confidence": 0,
                    "thesis": "Rollover / Swap window active (21:45-23:15 UTC). Spreads widen abnormally. Blocked.",
                    "market_regime": "ROLLOVER",
                    "setup": "NONE",
                }

            allowed = session_cfg.get("allowed_sessions", ["london", "new_york"])
            in_session = False
            if "london" in allowed and (7.0 <= hour_utc <= 16.0):
                in_session = True
            if "new_york" in allowed and (12.0 <= hour_utc <= 21.0):
                in_session = True
            if "asian" in allowed and (0.0 <= hour_utc <= 9.0):
                in_session = True
            if "all" in allowed:
                in_session = True

            if not in_session:
                return {
                    "symbol": symbol,
                    "decision": "WAIT",
                    "confidence": 0,
                    "thesis": f"Outside allowed trading sessions ({', '.join(allowed)}). Current UTC hour: {hour_utc:.2f}.",
                    "market_regime": "OFF_SESSION",
                    "setup": "NONE",
                }

        # 1. Higher Timeframe Trend Structure
        closes_trend = [c["close"] for c in candles_trend]
        trend_ema20 = ema(closes_trend, 20)
        trend_ema50 = ema(closes_trend, 50)
        last_trend = closes_trend[-1]

        trend = "NEUTRAL"
        if trend_ema20 and trend_ema50:
            if last_trend > trend_ema20 > trend_ema50:
                trend = "BULLISH"
            elif last_trend < trend_ema20 < trend_ema50:
                trend = "BEARISH"

        if trend == "NEUTRAL":
            return {
                "symbol": symbol,
                "decision": "WAIT",
                "confidence": 40,
                "market_regime": "RANGING_NO_CLEAR_TREND",
                "setup": "NONE",
                "thesis": f"{trend_tf} Trend is mixed or flat. Waiting for clear EMA alignment.",
                "invalidation": "N/A"
            }

        # 2. Execution Timeframe Indicators
        closes_entry = [c["close"] for c in candles_entry]
        highs_entry = [c["high"] for c in candles_entry]
        lows_entry = [c["low"] for c in candles_entry]
        opens_entry = [c["open"] for c in candles_entry]

        entry_ema20 = ema(closes_entry, 20)
        entry_ema50 = ema(closes_entry, 50)
        entry_adx = adx(highs_entry, lows_entry, closes_entry, 14) or 15.0
        entry_rsi = rsi(closes_entry, 14) or 50.0
        entry_atr = atr(highs_entry, lows_entry, closes_entry, 14) or (pip_size(symbol) * 15)

        # Skip low momentum / choppy drift
        if entry_adx < adx_min:
            return {
                "symbol": symbol,
                "decision": "WAIT",
                "confidence": 45,
                "market_regime": f"WEAK_TREND_{trend}",
                "setup": "NONE",
                "thesis": f"ADX is {entry_adx:.1f} (< {adx_min:.1f}). Market is lacking directional momentum.",
                "invalidation": "N/A"
            }

        # Volume Confirmation Filter (optional)
        vol_cfg = self.settings.get("trading", {}).get("volume_filter", {})
        if vol_cfg.get("enabled", False):
            volumes = [c.get("tick_volume", 0) for c in candles_entry]
            if any(v > 0 for v in volumes):
                avg_vol = sma(volumes, 20) or 1.0
                curr_vol = volumes[-1]
                min_ratio = profile.get("volume_ratio", vol_cfg.get("min_volume_ratio", 0.85))
                if curr_vol < avg_vol * min_ratio:
                    return {
                        "symbol": symbol,
                        "decision": "WAIT",
                        "confidence": 40,
                        "market_regime": f"LOW_VOL_{trend}",
                        "setup": "WAIT_VOLUME",
                        "thesis": f"Tick volume ({curr_vol}) < {min_ratio*100:.0f}% of 20-bar avg ({avg_vol:.0f}). Lacks institutional volume.",
                        "invalidation": "N/A"
                    }

        curr_close = closes_entry[-1]
        curr_open = opens_entry[-1]
        curr_low = lows_entry[-1]
        curr_high = highs_entry[-1]

        prev_close = closes_entry[-2]
        prev_open = opens_entry[-2]

        digits = 2 if ("XAU" in symbol or "GOLD" in symbol) else (3 if symbol.endswith("JPY") else 5)
        pip = pip_size(symbol)

        # Check Order Book (DOM) imbalance if supported by broker (inspired by jev-trader)
        book_info = feed.get_order_book(symbol) if hasattr(feed, "get_order_book") else None
        imbalance = book_info.get("imbalance", 0.5) if book_info else 0.5
        check_book = self.settings.get("trading", {}).get("check_order_book", False)

        decision = "WAIT"
        confidence = 0
        thesis = ""
        setup = "NONE"
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = []

        exec_type = self.settings.get("trading", {}).get("execution_type", "market").upper()

        # --- Rule 1: Bullish Trend Pullback ---
        if trend == "BULLISH" and entry_ema20 and entry_ema50:
            if check_book and book_info and imbalance < 0.35:
                # Sellers heavily dominate order book
                pass
            elif entry_ema20 >= entry_ema50 * 0.9995 and 40 <= entry_rsi <= 65:
                if (curr_low <= entry_ema20 or lows_entry[-2] <= entry_ema20) and curr_close > entry_ema20:
                    if curr_close > curr_open:  # Bullish candle confirmation
                        # Recent swing low over last 10 candles
                        recent_swing_low = min(lows_entry[-10:])
                        sl_dist = max(curr_close - recent_swing_low + 3 * pip, atr_mult * entry_atr)
                        # Cap max SL for account safety
                        sl_dist = min(sl_dist, max_sl_pips * pip)

                        decision = "BUY"
                        entry_price = round(entry_ema20, digits) if exec_type == "LIMIT" else tick["ask"]
                        stop_loss = round(entry_price - sl_dist, digits)
                        tp_val = round(entry_price + sl_dist * min_rr, digits)
                        take_profit = [tp_val]
                        confidence = min(88, int(65 + (entry_adx - 18) * 1.2))
                        setup = "QUANT_BULLISH_EMA_PULLBACK"
                        thesis = (f"{trend_tf} & {entry_tf} Uptrend confirmed. Pullback to {entry_tf} EMA20/50 rejected with bullish close. "
                                  f"ADX={entry_adx:.1f}, RSI={entry_rsi:.1f}, DOM={imbalance:.2f}.")

        # --- Rule 2: Bearish Trend Pullback ---
        elif trend == "BEARISH" and entry_ema20 and entry_ema50:
            if check_book and book_info and imbalance > 0.65:
                # Buyers heavily dominate order book
                pass
            elif entry_ema20 <= entry_ema50 * 1.0005 and 35 <= entry_rsi <= 60:
                if (curr_high >= entry_ema20 or highs_entry[-2] >= entry_ema20) and curr_close < entry_ema20:
                    if curr_close < curr_open:  # Bearish candle confirmation
                        recent_swing_high = max(highs_entry[-10:])
                        sl_dist = max(recent_swing_high - curr_close + 3 * pip, atr_mult * entry_atr)
                        sl_dist = min(sl_dist, max_sl_pips * pip)

                        decision = "SELL"
                        entry_price = round(entry_ema20, digits) if exec_type == "LIMIT" else tick["bid"]
                        stop_loss = round(entry_price + sl_dist, digits)
                        tp_val = round(entry_price - sl_dist * min_rr, digits)
                        take_profit = [tp_val]
                        confidence = min(88, int(65 + (entry_adx - 18) * 1.2))
                        setup = "QUANT_BEARISH_EMA_PULLBACK"
                        thesis = (f"{trend_tf} & {entry_tf} Downtrend confirmed. Rally to {entry_tf} EMA20/50 rejected with bearish close. "
                                  f"ADX={entry_adx:.1f}, RSI={entry_rsi:.1f}, DOM={imbalance:.2f}.")

        if decision == "WAIT":
            return {
                "symbol": symbol,
                "decision": "WAIT",
                "confidence": 50,
                "market_regime": f"TREND_{trend}",
                "setup": "WATCHING_PULLBACK",
                "thesis": f"Monitoring {symbol} in {trend} trend ({trend_tf}). Waiting for high-probability {entry_tf} EMA pullback trigger.",
                "invalidation": "N/A",
                "market_context": {"trend": trend, "adx": round(entry_adx, 1), "rsi": round(entry_rsi, 1), "imbalance": imbalance, "timeframe": entry_tf}
            }

        return {
            "symbol": symbol,
            "decision": decision,
            "confidence": confidence,
            "entry": {"price": entry_price, "type": exec_type},
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": round(min_rr, 2),
            "market_regime": f"TREND_{trend}",
            "setup": setup,
            "thesis": thesis,
            "invalidation": f"Price violates swing level at {stop_loss}",
            "position_size": {"risk_percent": 0.005},
            "market_context": {"trend": trend, "adx": round(entry_adx, 1), "rsi": round(entry_rsi, 1), "imbalance": imbalance, "timeframe": entry_tf}
        }
