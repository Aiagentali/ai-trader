"""Risk engine — deterministic, validates every trade decision. AI cannot override."""
import math
from dataclasses import dataclass, field

CONTRACT_SIZE = 100_000  # standard lot

# Rough currency-pair correlation groups (shared legs)
CURRENCY_LEG = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"), "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"),
    "NZDUSD": ("NZD", "USD"), "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
}


@dataclass
class RiskResult:
    approved: bool
    reasons_reject: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    calculated_lots: float = 0.0
    risk_percent: float = 0.0


def pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


def calc_position_size(balance: float, risk_pct: float, entry: float,
                       stop_loss: float, symbol: str) -> float:
    """Lots = risk_amount / (SL distance in price * contract size)."""
    risk_amount = balance * risk_pct
    dist = abs(entry - stop_loss)
    if dist <= 0:
        return 0.0
    per_lot_loss = dist * CONTRACT_SIZE
    lots = risk_amount / per_lot_loss
    return math.floor(lots * 100) / 100  # 2-decimal floor


def calc_rr(direction: str, entry: float, sl: float, tps: list) -> float:
    if not tps:
        return 0.0
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0
    reward = abs(tps[0] - entry)
    return round(reward / risk, 2)


def _correlated_count(symbol: str, open_symbols: list) -> int:
    leg = set(CURRENCY_LEG.get(symbol, ()))
    count = 0
    for s in open_symbols:
        if s == symbol:
            count += 1
            continue
        if set(CURRENCY_LEG.get(s, ())) & leg:
            count += 1
    return count


class RiskEngine:
    def __init__(self, config: dict, repo, feed, calendar=None):
        self.cfg = config.get("risk", config)
        self.repo = repo
        self.feed = feed
        self.calendar = calendar

    def _normal_spread(self, symbol: str) -> float:
        from app.market.mt5 import MockFeed
        if isinstance(self.feed, MockFeed):
            return MockFeed.NORMAL_SPREAD.get(symbol, 0.0002)
        return pip_size(symbol) * 1.5

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
        lots = calc_position_size(balance, risk_pct, entry, sl, symbol)
        if lots <= 0:
            res.reasons_reject.append("position size rounds to zero")
            return res
        res.calculated_lots = lots
        res.risk_percent = round(risk_pct * 100, 3)

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
        if cons >= cfg["max_consecutive_losses"]:
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
        if self.calendar:
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
