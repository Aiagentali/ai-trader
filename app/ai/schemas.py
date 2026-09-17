"""Structured JSON schemas + strict validation. Malformed AI output is rejected."""
import json
import math


def _number(value, field, positive=False):
    if (type(value) not in (int, float) or
            (isinstance(value, float) and not math.isfinite(value))):
        raise SchemaError(f"field {field} must be a finite number")
    if positive and value <= 0:
        raise SchemaError(f"field {field} must be positive")

DECISION_SCHEMA = {
    "symbol": str,
    "decision": str,          # WAIT|WATCH|BUY|SELL|HOLD|CLOSE|MODIFY
    "confidence": int,
    "market_regime": str,
    "setup": str,
    "time_horizon": str,
    "entry": (dict, type(None)),
    "stop_loss": (int, float, type(None)),
    "take_profit": (list, type(None)),
    "position_size": (dict, type(None)),
    "risk_reward": (int, float, type(None)),
    "thesis": str,
    "technical_analysis": str,
    "price_action_analysis": str,
    "fundamental_analysis": str,
    "news_analysis": str,
    "invalidation": str,
    "conditions": list,
    "risks": list,
}

ALLOWED_DECISIONS = {"WAIT", "WATCH", "BUY", "SELL", "HOLD", "CLOSE", "MODIFY"}
REQUIRED_FOR_TRADE = {"entry", "stop_loss", "take_profit", "thesis", "invalidation"}


class SchemaError(Exception):
    pass


def parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        # tolerate markdown fences
        stripped = content.strip().removeprefix("```json").removeprefix("```") \
            .removesuffix("```").strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            raise SchemaError(f"AI output is not valid JSON: {e}") from e


def validate_decision(obj: dict, strict: bool = False) -> dict:
    if not isinstance(obj, dict):
        raise SchemaError("decision must be a JSON object")
    for key, typ in DECISION_SCHEMA.items():
        if key not in obj:
            if strict:
                raise SchemaError(f"missing required field: {key}")
            continue
        if typ is not None and not isinstance(obj[key], typ):
            # bool is subclass of int — reject for numeric fields
            if typ in ((int, float), int) and isinstance(obj[key], bool):
                raise SchemaError(f"field {key} must be numeric")
            raise SchemaError(f"field {key} has wrong type: {type(obj[key]).__name__}")
    d = obj.get("decision", "")
    if d not in ALLOWED_DECISIONS:
        raise SchemaError(f"invalid decision: {d!r}")
    c = obj.get("confidence", 0)
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0 <= c <= 100:
        raise SchemaError("confidence must be 0-100")
    for field in ("stop_loss", "risk_reward"):
        if obj.get(field) is not None:
            _number(obj[field], field)
    entry = obj.get("entry")
    if entry and "price" in entry and entry["price"] is not None:
        _number(entry["price"], "entry.price", positive=True)
    for tp in obj.get("take_profit") or []:
        _number(tp, "take_profit", positive=True)
    size = obj.get("position_size")
    if size and "requested_lots" in size:
        lots = size["requested_lots"]
        # WAIT/WATCH/management decisions legitimately carry 0/null sizing;
        # positivity is enforced for actual trade decisions below.
        if d in ("BUY", "SELL") or lots is not None:
            _number(lots, "position_size.requested_lots", positive=bool(d in ("BUY", "SELL") or lots))
    if d in ("BUY", "SELL"):
        missing = REQUIRED_FOR_TRADE - set(obj)
        if missing:
            raise SchemaError(f"trade decision missing fields: {sorted(missing)}")
        sl, entry = obj["stop_loss"], obj["entry"]
        _number(sl, "stop_loss", positive=True)
        if not isinstance(entry, dict) or "price" not in entry:
            raise SchemaError("entry must include price")
        if not isinstance(obj["take_profit"], list) or not obj["take_profit"]:
            raise SchemaError("take_profit must be a non-empty list")
        side = 1 if d == "BUY" else -1
        if side * (entry["price"] - sl) <= 0:
            raise SchemaError(f"stop_loss on wrong side for {d}")
        for tp in obj["take_profit"]:
            if side * (tp - entry["price"]) <= 0:
                raise SchemaError(f"take_profit {tp} on wrong side for {d}")
    return obj
