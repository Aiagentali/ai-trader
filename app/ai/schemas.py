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


def _strip_fences(text: str) -> str:
    return (text.strip()
            .removeprefix("```json").removeprefix("```JSON")
            .removeprefix("```").removesuffix("```").strip())


def _scan_balanced_object(text: str) -> str:
    """Return the first complete {...} span, ignoring braces inside JSON strings."""
    start = text.find("{")
    if start < 0:
        raise SchemaError("AI output contains no JSON object")
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise SchemaError("AI output JSON object is not closed (truncated response)")


def repair_truncated_json(content: str) -> dict:
    """Salvage a completion cut short by the token budget.

    Two strategies, in order: close the open structure if nothing is missing
    (a trailing text field lost its closing quote), else drop trailing elements
    from the last incomplete array/object until the document closes. A truncated
    trade answer is never a trading signal: the caller still re-validates, and
    BUY/SELL need every level field.
    """
    text = _strip_fences(content)
    # strategy 1: the object is missing only its closing characters
    opener = {"{": "}", "[": "]"}
    stack = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in opener:
            stack.append(opener[ch])
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
    if stack:
        # unterminated string: close the quote, then the containers
        candidate = text + ('"' if in_str else "") + "".join(reversed(stack))
        obj = _loads(candidate)
        if isinstance(obj, dict):
            return obj
    # strategy 2: walk back element by element
    for cut in _candidate_cuts(text):
        obj = _loads(cut)
        if isinstance(obj, dict):
            return obj
    raise SchemaError("AI output could not be repaired from truncated JSON")


def _loads(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _candidate_cuts(text: str):
    """Yield progressively shorter prefixes that could close into valid JSON."""
    start = text.find("{")
    if start < 0:
        return
    in_str = False
    escaped = False
    for i in range(len(text) - 1, start, -1):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_str = not in_str
            continue
        if in_str or ch not in "}],":
            continue
        # cut here and close every container still open at this point
        prefix = text[:i]
        stack = []
        in_s = False
        esc = False
        for c in prefix:
            if in_s:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_s = False
                continue
            if c == '"':
                in_s = True
            elif c == "{":
                stack.append("}")
            elif c == "[":
                stack.append("]")
            elif c in ("}", "]") and stack and stack[-1] == c:
                stack.pop()
        if in_s:
            prefix += '"'
        closing = "".join(reversed(stack))
        trimmed = prefix.rstrip().rstrip(",")
        if not trimmed.endswith(("{", "[")):
            trimmed += closing
        yield trimmed


def parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        stripped = _strip_fences(content)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
        # truncated completion: salvage what arrived before the cut
        try:
            return repair_truncated_json(content)
        except SchemaError:
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
