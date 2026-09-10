"""Repository layer — all SQL lives here. Components never touch SQL directly."""
import json
from datetime import datetime, timezone

from app.database.models import get_connection, init_db


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Repository:
    def __init__(self, db_path: str = None):
        init_db(db_path)
        self.conn = get_connection(db_path)

    def close(self):
        self.conn.close()

    # ---- market snapshots / analysis / thesis ----
    def save_snapshot(self, symbol: str, timeframe: str, payload: dict):
        self.conn.execute(
            "INSERT INTO market_snapshots(symbol, ts, timeframe, payload) VALUES (?,?,?,?)",
            (symbol, utcnow(), timeframe, _j(payload)))
        self.conn.commit()

    def save_analysis(self, symbol: str, level: str, summary: str, payload: dict):
        self.conn.execute(
            "INSERT INTO market_analysis(symbol, ts, level, summary, payload) VALUES (?,?,?,?,?)",
            (symbol, utcnow(), level, summary, _j(payload)))
        self.conn.commit()

    def save_thesis(self, symbol: str, bias: str, regime: str, thesis: str,
                    invalidation: str, payload: dict):
        self.conn.execute(
            "INSERT INTO market_thesis(symbol, ts, bias, regime, thesis, invalidation, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, utcnow(), bias, regime, thesis, invalidation, _j(payload)))
        self.conn.commit()

    def latest_thesis(self, symbol: str):
        row = self.conn.execute(
            "SELECT * FROM market_thesis WHERE symbol=? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()
        return dict(row) if row else None

    def thesis_history(self, symbol: str, limit: int = 24):
        rows = self.conn.execute(
            "SELECT ts, bias, regime, thesis FROM market_thesis WHERE symbol=? "
            "ORDER BY id DESC LIMIT ?", (symbol, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---- news / calendar ----
    def save_news(self, item: dict) -> bool:
        """Returns True if inserted (not duplicate)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO news(source, title, url, published_ts, currencies, importance, "
            "category, summary) VALUES (?,?,?,?,?,?,?,?)",
            (item.get("source"), item.get("title"), item.get("url"),
             item.get("published_ts"), _j(item.get("currencies", [])),
             item.get("importance"), item.get("category"), item.get("summary")))
        self.conn.commit()
        return cur.rowcount > 0

    def recent_news(self, currencies=None, limit: int = 20):
        rows = self.conn.execute(
            "SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["currencies"] = json.loads(d.get("currencies") or "[]")
            out.append(d)
        return out

    def save_event(self, ev: dict) -> bool:
        cur = self.conn.execute(
            "INSERT INTO economic_events(event, currency, scheduled_time, importance, previous, "
            "forecast, actual) VALUES (?,?,?,?,?,?,?)",
            (ev.get("event"), ev.get("currency"), ev.get("scheduled_time"),
             ev.get("importance"), ev.get("previous"), ev.get("forecast"), ev.get("actual")))
        self.conn.commit()
        return cur.rowcount > 0

    def upcoming_events(self, currencies=None, within_hours: int = 48):
        rows = self.conn.execute(
            "SELECT * FROM economic_events ORDER BY scheduled_time ASC").fetchall()
        out = []
        now = datetime.now(timezone.utc)
        for r in rows:
            d = dict(r)
            try:
                st = datetime.fromisoformat(str(d["scheduled_time"]).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            delta_hours = (st - now).total_seconds() / 3600
            if -1 <= delta_hours <= within_hours:
                if currencies is None or d.get("currency") in currencies:
                    d["hours_away"] = round(delta_hours, 2)
                    out.append(d)
        return out

    # ---- decisions / trades ----
    def save_decision(self, request_id: str, symbol: str, decision: str, approved,
                      reject_reasons, payload: dict, versions: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO trade_decisions(request_id, symbol, ts, decision, approved, "
            "reject_reasons, payload, agent_version, prompt_version, risk_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request_id, symbol, utcnow(), decision,
             int(bool(approved)) if approved is not None else None,
             _j(reject_reasons or []), _j(payload),
             versions.get("agent_version"), versions.get("prompt_version"),
             versions.get("risk_version")))
        self.conn.commit()

    def save_trade(self, trade: dict):
        cols = ["trade_id", "opened_ts", "symbol", "direction", "entry", "stop_loss",
                "take_profit", "position_size", "risk_percent", "rr", "market_regime",
                "setup", "ai_confidence", "thesis", "invalidation", "status", "request_id"]
        vals = []
        for c in cols:
            v = trade.get(c)
            vals.append(_j(v) if c == "take_profit" else v)
        self.conn.execute(
            f"INSERT OR REPLACE INTO trades({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)
        self.conn.commit()

    def open_trades(self):
        rows = self.conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]

    def close_trade(self, trade_id: str, exit_price: float, profit_loss: float,
                    r_multiple: float, exit_reason: str):
        """Close a trade by its trade_id, request_id, or symbol+status fallback."""
        cur = self.conn.execute(
            "UPDATE trades SET status='closed', exit_ts=?, exit_price=?, profit_loss=?, "
            "r_multiple=?, exit_reason=? WHERE trade_id=?",
            (utcnow(), exit_price, profit_loss, r_multiple, exit_reason, trade_id))
        if cur.rowcount == 0:
            # Fallback 1: try by request_id
            cur = self.conn.execute(
                "UPDATE trades SET status='closed', exit_ts=?, exit_price=?, profit_loss=?, "
                "r_multiple=?, exit_reason=? WHERE request_id=? AND status='open'",
                (utcnow(), exit_price, profit_loss, r_multiple, exit_reason, trade_id))
        if cur.rowcount == 0:
            # Fallback 2: close the most recent open trade (from position close)
            # SQLite doesn't support ORDER BY in UPDATE, so use a subquery
            most_recent = self.conn.execute(
                "SELECT trade_id FROM trades WHERE status='open' "
                "ORDER BY opened_ts DESC LIMIT 1").fetchone()
            if most_recent:
                self.conn.execute(
                    "UPDATE trades SET status='closed', exit_ts=?, exit_price=?, profit_loss=?, "
                    "r_multiple=?, exit_reason=? WHERE trade_id=?",
                    (utcnow(), exit_price, profit_loss, r_multiple, exit_reason,
                     most_recent["trade_id"]))
        self.conn.commit()

    def get_trade(self, trade_id: str):
        row = self.conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def save_review(self, trade_id: str, classification: str, review: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO trade_reviews(trade_id, ts, classification, review) "
            "VALUES (?,?,?,?)", (trade_id, utcnow(), classification, review))
        self.conn.commit()

    # ---- positions (simulation bookkeeping) ----
    def save_position(self, pos: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO positions(ticket, symbol, direction, volume, entry_price, "
            "stop_loss, take_profit, opened_ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(pos["ticket"]), pos["symbol"], pos["direction"], pos["volume"],
             pos["entry_price"], pos.get("stop_loss"), pos.get("take_profit"),
             utcnow(), pos.get("status", "open")))
        self.conn.commit()

    def open_positions(self, symbol: str = None):
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open' AND symbol=?", (symbol,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return [dict(r) for r in rows]

    def close_position(self, ticket: str):
        self.conn.execute(
            "UPDATE positions SET status='closed' WHERE ticket=?", (str(ticket),))
        self.conn.commit()

    def update_position(self, ticket: str, stop_loss=None, take_profit=None,
                        volume=None):
        if stop_loss is not None:
            self.conn.execute("UPDATE positions SET stop_loss=? WHERE ticket=?",
                              (stop_loss, str(ticket)))
        if take_profit is not None:
            self.conn.execute("UPDATE positions SET take_profit=? WHERE ticket=?",
                              (take_profit, str(ticket)))
        if volume is not None:
            self.conn.execute("UPDATE positions SET volume=? WHERE ticket=?",
                              (volume, str(ticket)))
        self.conn.commit()

    # ---- ai / tokens ----
    def log_ai_request(self, task: str, model: str, input_tokens: int, output_tokens: int,
                       estimated_cost: float, symbol: str = None, status: str = "ok"):
        self.conn.execute(
            "INSERT INTO ai_requests(ts, task, model, input_tokens, output_tokens, total_tokens, "
            "estimated_cost, symbol, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (utcnow(), task, model, input_tokens, output_tokens,
             input_tokens + output_tokens, estimated_cost, symbol, status))
        self.conn.commit()

    def token_usage_today(self) -> dict:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT * FROM token_usage WHERE day=?", (day,)).fetchone()
        if row:
            return dict(row)
        return {"day": day, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "request_count": 0}

    def add_token_usage(self, input_tokens: int, output_tokens: int):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO token_usage(day, input_tokens, output_tokens, total_tokens, "
            "request_count) VALUES (?,?,?,? ,1) "
            "ON CONFLICT(day) DO UPDATE SET input_tokens=input_tokens+?, "
            "output_tokens=output_tokens+?, total_tokens=total_tokens+?, "
            "request_count=request_count+1",
            (day, input_tokens, output_tokens, input_tokens + output_tokens,
             input_tokens, output_tokens, input_tokens + output_tokens))
        self.conn.commit()

    # ---- system events ----
    def log_event(self, event: str, payload: dict = None):
        self.conn.execute(
            "INSERT INTO system_events(ts, event, payload) VALUES (?,?,?)",
            (utcnow(), event, _j(payload or {})))
        self.conn.commit()
