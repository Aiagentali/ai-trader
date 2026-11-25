"""Risk engine — deterministic, validates every trade decision. AI cannot override."""
import math
from dataclasses import dataclass, field

CONTRACT_SIZE = 100_000  # standard lot

CRYPTO_TOKENS = ("BTC", "ETH", "SOL", "BNB", "LTC", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK", "TON")


def is_crypto_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return any(tok in s for tok in CRYPTO_TOKENS) or "CRYPTO" in s


def is_market_open(symbol: str, now_utc=None) -> bool:
    """Crypto trades 24/7. TradFi (Forex/Metals/Oil/Indices) closes Fri 21:55 UTC to Sun 22:00 UTC."""
    if is_crypto_symbol(symbol):
        return True
    from datetime import datetime, timezone
    dt = now_utc or datetime.now(timezone.utc)
    wd = dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    hour = dt.hour + dt.minute / 60.0
    if wd == 5:
        return False
    if wd == 4 and hour >= 21.92:
        return False
    if wd == 6 and hour < 22.0:
        return False
    return True


def contract_size(symbol: str) -> float:
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s:
        return 100.0
    if is_crypto_symbol(s):
        return 1.0
    if "BRENT" in s or "OIL" in s or "WTI" in s:
        return 100.0
    if "US30" in s or "WS30" in s or s.startswith("DJ") or "DOW" in s:
        return 1.0
    return CONTRACT_SIZE


# Rough currency-pair correlation groups (shared legs)
CURRENCY_LEG = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"), "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"),
    "NZDUSD": ("NZD", "USD"), "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
}


def _get_currency_leg(symbol: str) -> set:
    if symbol in CURRENCY_LEG:
        return set(CURRENCY_LEG[symbol])
    clean = symbol.split(".")[0]
    if clean in CURRENCY_LEG:
        return set(CURRENCY_LEG[clean])
    if len(clean) == 6 and clean.isalpha():
        return {clean[:3], clean[3:6]}
    return {symbol}


@dataclass
class RiskResult:
    approved: bool
    reasons_reject: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    calculated_lots: float = 0.0
    risk_percent: float = 0.0


def pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s:
        return 0.1
    if "BTC" in s:
        return 1.0
    if "ETH" in s:
        return 0.1
    if "SOL" in s:
        return 0.01
    if "BRENT" in s or "OIL" in s or "WTI" in s:
        return 0.01
    if "US30" in s or "WS30" in s or s.startswith("DJ") or "DOW" in s:
        return 1.0
    if s.endswith("JPY") or "JPY" in s:
        return 0.01
    return 0.0001


def calc_position_size(balance: float, risk_pct: float, entry: float,
                       stop_loss: float, symbol: str,
                       allow_micro: bool = False, max_micro_risk_pct: float = 0.08) -> float:
    """Lots = risk_amount / (SL distance in price * contract size).
    Supports small accounts by allowing 0.01 micro-lot if dollar loss is bounded.
    """
    risk_amount = balance * risk_pct
    dist = abs(entry - stop_loss)
    if dist <= 0:
        return 0.0
    per_lot_loss = dist * contract_size(symbol)
    lots = risk_amount / per_lot_loss
    floored = math.floor(lots * 100) / 100  # 2-decimal floor
    if floored < 0.01 and allow_micro and balance >= 10.0:
        micro_loss = 0.01 * per_lot_loss
        if micro_loss <= balance * max_micro_risk_pct:
            return 0.01
    return floored


def calc_rr(direction: str, entry: float, sl: float, tps: list) -> float:
    if not tps:
        return 0.0
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0
    reward = abs(tps[0] - entry)
    return round(reward / risk, 2)


def _correlated_count(symbol: str, open_symbols: list) -> int:
    leg = _get_currency_leg(symbol)
    count = 0
    for s in open_symbols:
        if s == symbol:
            count += 1
            continue
        if _get_currency_leg(s) & leg:
            count += 1
    return count


class _CalendarListAdapter:
    """Adapts a plain list of event dicts to the upcoming_events() interface."""

    def __init__(self, events):
        self._events = events or []

    def upcoming_events(self, currencies=None, within_hours: int = 48):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        out = []
        for ev in self._events:
            try:
                st = datetime.fromisoformat(
                    str(ev.get("scheduled_time", "")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            delta_h = (st - now).total_seconds() / 3600
            if not (-1 <= delta_h <= within_hours):
                continue
            if currencies is not None and ev.get("currency") not in currencies:
                continue
            d = dict(ev)
            d["hours_away"] = round(delta_h, 2)
            out.append(d)
        return out


class RiskEngine:
    def __init__(self, config: dict, repo, feed, calendar=None):
        self.cfg = config.get("risk", config)
        self.repo = repo
        self.feed = feed
        # calendar may be a plain list of event dicts or an object with
        # upcoming_events() — normalize to the adapter interface.
        if calendar is None:
            self.calendar = None
        elif isinstance(calendar, list):
            self.calendar = _CalendarListAdapter(calendar)
        else:
            self.calendar = calendar

    def _normal_spread(self, symbol: str) -> float:
        from app.market.mt5 import MockFeed
        if isinstance(self.feed, MockFeed):
            return MockFeed.NORMAL_SPREAD.get(symbol, 0.0002)
        s = (symbol or "").upper()
        if is_crypto_symbol(s):
            tick = self.feed.get_tick(symbol) if hasattr(self.feed, "get_tick") else None
            price = (tick or {}).get("bid") or 0.0
            if price > 0:
                # Crypto spreads on retail/demo CFD brokers are typically 0.04% to 0.12% of price
                return max(pip_size(symbol) * 2.0, price * 0.0008)
            if "BTC" in s:
                return 60.0
            if "ETH" in s:
                return 2.5
            return pip_size(symbol) * 15.0
        if "XAU" in s or "GOLD" in s:
            return max(pip_size(symbol) * 2.5, 0.35)
        if "BRENT" in s or "OIL" in s or "WTI" in s:
            return max(pip_size(symbol) * 2.5, 0.05)
        if "US30" in s or "WS30" in s or s.startswith("DJ") or "DOW" in s:
            return max(pip_size(symbol) * 3.0, 3.0)
        return pip_size(symbol) * 1.8

    def validate(self, decision: dict, account: dict) -> RiskResult:
        res = RiskResult(approved=False)
        cfg = self.cfg
        symbol = decision["symbol"]
        d = decision["decision"]

        # --- basic sanity ---
        if d not in ("BUY", "SELL"):
            res.approved = True  # non-trade decisions pass through (logged elsewhere)
            return res

        entry = decision.get("entry", {}).get("price")
        sl = decision.get("stop_loss")
        tps = decision.get("take_profit") or []
        if entry is None or sl is None or not tps:
            res.reasons_reject.append("missing entry/SL/TP")
            return res

        # --- SL/TP geometry (already schema-checked; re-verify deterministic) ---
        side = 1 if d == "BUY" else -1
        if side * (entry - sl) <= 0 or any(side * (tp - entry) <= 0 for tp in tps):
            res.reasons_reject.append("invalid SL/TP geometry")
            return res

        # --- risk per trade ---
        balance = account.get("balance", 0.0)
        requested_risk = decision.get("position_size", {}).get("risk_percent")
        risk_pct = min(requested_risk, cfg["max_risk_per_trade"]) if requested_risk \
            else cfg["max_risk_per_trade"]
        allow_micro = cfg.get("allow_micro_lot_small_account", False) and balance < 500
        max_micro_risk = cfg.get("max_small_account_risk_pct", 0.08)
        lots = calc_position_size(balance, risk_pct, entry, sl, symbol,
                                  allow_micro=allow_micro, max_micro_risk_pct=max_micro_risk)
        if lots <= 0:
            res.reasons_reject.append("position size rounds to zero")
            return res
        res.calculated_lots = lots
        res.risk_percent = round((lots * abs(entry - sl) * contract_size(symbol) / balance) * 100, 3) if balance > 0 else 0.0

        # --- RR ---
        rr = calc_rr(d, entry, sl, tps)
        if rr < cfg["min_rr"]:
            res.reasons_reject.append(f"RR {rr} < min {cfg['min_rr']}")
        res.warnings.append(f"RR={rr}")

        # --- daily loss ---
        u = self._daily_pnl()
        if u <= -cfg["max_daily_loss"] * balance:
            res.reasons_reject.append(
                f"daily loss limit hit ({u:.2f} <= {-cfg['max_daily_loss'] * balance:.2f})")

        # --- open positions cap ---
        open_pos = self.repo.open_positions()
        if len(open_pos) >= cfg["max_open_positions"]:
            res.reasons_reject.append(f"max open positions ({cfg['max_open_positions']})")

        # --- correlation ---
        corr = _correlated_count(symbol, [p["symbol"] for p in open_pos])
        if corr >= cfg["max_correlated_exposure"]:
            res.reasons_reject.append(
                f"correlated exposure: {corr} positions share currencies with {symbol}")

        # --- consecutive losses ---
        cons = self._consecutive_losses()
        max_cons = cfg.get("max_consecutive_losses", 4)
        if cons >= max_cons:
            res.reasons_reject.append(f"{cons} consecutive losses — trading halted for today")

        # --- spread ---
        spread = self.feed.get_spread(symbol)
        normal = self._normal_spread(symbol)
        if spread is None:
            res.reasons_reject.append("spread unavailable")
        elif normal and spread > normal * cfg["max_spread_multiplier"]:
            res.reasons_reject.append(
                f"spread {spread:.5f} > {cfg['max_spread_multiplier']}x normal {normal:.5f}")

        # --- news window ---
        if self.calendar is not None:
            events = self.calendar.upcoming_events(currencies=set(
                CURRENCY_LEG.get(symbol, ())), within_hours=1)
            high = [e for e in events if e.get("importance") == "HIGH"]
            if high:
                soonest = min(e["hours_away"] for e in high)
                if soonest < 0.5 or -1 <= soonest < 0:
                    res.reasons_reject.append(
                        f"high-impact event within {soonest}h: "
                        + "; ".join(e["event"] for e in high))
                elif soonest < 1:
                    res.warnings.append(f"high-impact event in {soonest:.1f}h")

        res.approved = not res.reasons_reject
        return res

    def _daily_pnl(self) -> float:
        rows = self.repo.open_trades()
        # closed today
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed = self.repo.conn.execute(
            "SELECT profit_loss FROM trades WHERE status='closed' "
            "AND substr(exit_ts,1,10)=?", (today,)).fetchall()
        return sum((r["profit_loss"] or 0.0) for r in closed)

    def _consecutive_losses(self) -> int:
        rows = self.repo.conn.execute(
            "SELECT profit_loss FROM trades WHERE status='closed' "
            "ORDER BY exit_ts DESC LIMIT 20").fetchall()
        n = 0
        for r in rows:
            if (r["profit_loss"] or 0.0) < 0:
                n += 1
            else:
                break
        return n
