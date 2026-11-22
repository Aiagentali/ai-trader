"""Local-only control API; importing this module never starts trading."""
import json
import os
from pathlib import Path
from dotenv import dotenv_values
import ipaddress
import secrets
import re
import tempfile
import threading
import subprocess
import sys
import atexit
from collections import deque
from urllib.parse import urlsplit
from flask import jsonify, request


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_url(value):
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid base_url")
    parts = urlsplit(value)
    _ = parts.port
    allowed = parts.scheme == "https" or (parts.scheme == "http" and is_loopback(parts.hostname or ""))
    if not allowed or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("Use HTTPS (or loopback HTTP), without credentials, query or fragment")


def merged_settings(app, data):
    if not isinstance(data, dict) or set(data) - FIELDS.keys():
        raise ValueError("Invalid settings fields")
    values = settings(app)
    for key, value in data.items():
        if key == "daily_token_limit":
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000000000:
                raise ValueError("daily_token_limit must be a positive integer <= 1000000000")
        else:
            if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096:
                raise ValueError("Invalid " + key)
            value = value.strip()
            if key == "api_key" and not value:
                continue
            if key != "api_key" and not value:
                raise ValueError("Empty " + key)
            if key == "base_url":
                _validate_url(value)
            if key == "mode" and value not in {"simulation", "mt5"}:
                raise ValueError("mode must be simulation or mt5")
        values[key] = value
    return values


def persist_settings(path, data, values):
    # Only replace submitted keys; preserve comments and all unrelated values.
    updates = {FIELDS[k]: str(values[k]) for k in data if k != "api_key" or data[k].strip()}
    if not updates:
        return
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not any(re.match(r"^\s*(?:export\s+)?" + key + r"\s*=", line) for key in updates)]
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Za-z0-9_./:@+,-]*", value):
            value = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        kept.append(key + "=" + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".panel-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(kept) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


FIELDS = {"base_url": "AI_BASE_URL", "api_key": "AI_API_KEY",
          "model_normal": "AI_MODEL_NORMAL", "model_watch": "AI_MODEL_WATCH",
          "model_critical": "AI_MODEL_CRITICAL", "mode": "EXECUTION_MODE",
          "daily_token_limit": "AI_DAILY_TOKEN_LIMIT"}
DEFAULTS = {"base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": "",
            "model_normal": "gemini-decision-1", "model_watch": "gemini-decision-1",
            "model_critical": "gemini-decision-1", "mode": "simulation", "daily_token_limit": 200000}


def settings(app):
    values = dotenv_values(app.config["PANEL_ENV"], interpolate=False)
    result = {k: values.get(v, os.environ.get(v, DEFAULTS[k])) for k, v in FIELDS.items()}
    try:
        result["daily_token_limit"] = int(result["daily_token_limit"])
        if not 1 <= result["daily_token_limit"] <= 1000000000:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Invalid AI_DAILY_TOKEN_LIMIT in environment; set a positive integer <= 1000000000") from None
    return result


def public_settings(values):
    return {**{k: v for k, v in values.items() if k != "api_key"},
            "api_key_set": bool(values.get("api_key"))}


def memory_health(app):
    root = Path(app.config.get("CGROUP_ROOT", "/sys/fs/cgroup"))
    def number(path):
        try:
            text = path.read_text().strip()
            return int(text) if text != "max" else None
        except (OSError, ValueError):
            return None
    current = number(root / "memory.current")
    limit = number(root / "memory.max")
    host = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                host[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return {"memory": {"current_bytes": current, "limit_bytes": limit,
                       "percent": round(current / limit * 100, 2) if current is not None and limit else None,
                       "swap_current_bytes": number(root / "memory.swap.current"),
                       "swap_limit_bytes": number(root / "memory.swap.max"),
                       "source": "cgroup-v2" if current is not None else "unavailable"},
            "host_memory": host, "note": "Null means unavailable or unlimited; host swap is not container swap."}


# Fixed bootstrap only: no user-supplied code, paths or shell commands.
# Override configuration before main.py imports its load_settings reference.
WORKER = """
import os, runpy
from pathlib import Path
from dotenv import dotenv_values
from app.config import config_loader
original = config_loader.load_settings
def safe_settings():
    config = original()
    config.setdefault('execution', {})['mode'] = os.environ['EXECUTION_MODE']
    config.setdefault('trading', {})['live_trading_enabled'] = False
    config.setdefault('ai', {})['daily_token_limit'] = int(os.environ['AI_DAILY_TOKEN_LIMIT'])
    return config
config_loader.load_settings = safe_settings
runpy.run_path('main.py', run_name='__main__')
"""


def _strip_control_noise(message: str) -> str:
    """Cut a log message at the internal error marker so the panel shows the
    provider's own wording rather than the wrapper's prefix."""
    for marker in ("AI request failed:", "AI call failed:", "AI retry failed:"):
        if marker in message:
            message = message[message.index(marker) + len(marker):]
    return message.strip()[:300]


def _diagnostics(repo, key: str = "") -> dict:
    """Turn the raw tables into the answers a debugger actually asks for.

    The recurring failures are visible here without reading log lines: which
    model is actually being used, how often calls error out, how much of the
    token budget reasoning models eat, and whether any answer was recovered
    from a truncated completion.
    """
    out = {}

    def scrub(value):
        """Same redaction the row dump applies; a model id is not a safe place
        for a credential to survive."""
        if not key or not isinstance(value, str):
            return value
        return value.replace(key, "[redacted]")

    try:
        out["ai_by_model"] = [dict(r) for r in repo.conn.execute(
            "SELECT model, COUNT(*) AS calls, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS failed, "
            "MAX(ts) AS last_seen "
            "FROM ai_requests WHERE model IS NOT NULL "
            "GROUP BY model ORDER BY last_seen DESC LIMIT 10").fetchall()]
        out["ai_by_model"] = [{k: scrub(v) for k, v in row.items()} for row in out["ai_by_model"]]
    except Exception as exc:
        out["ai_by_model_error"] = str(exc)[:200]
    try:
        out["ai_by_status"] = {r["status"]: r["n"] for r in repo.conn.execute(
            "SELECT COALESCE(status,'(none)') AS status, COUNT(*) AS n "
            "FROM ai_requests GROUP BY status").fetchall()}
    except Exception as exc:
        out["ai_by_status_error"] = str(exc)[:200]
    try:
        out["decisions_by_kind"] = {r["decision"]: r["n"] for r in repo.conn.execute(
            "SELECT decision, COUNT(*) AS n FROM trade_decisions "
            "GROUP BY decision ORDER BY n DESC LIMIT 15").fetchall()}
    except Exception as exc:
        out["decisions_by_kind_error"] = str(exc)[:200]
    try:
        out["recovered"] = [dict(r) for r in repo.conn.execute(
            "SELECT symbol, decision, reject_reasons, ts FROM trade_decisions "
            "WHERE decision IN ('RECOVERED','PARSE_ERROR') "
            "ORDER BY id DESC LIMIT 20").fetchall()]
    except Exception as exc:
        out["recovered_error"] = str(exc)[:200]
    try:
        out["trades"] = {r["status"]: r["n"] for r in repo.conn.execute(
            "SELECT status, COUNT(*) AS n FROM trades GROUP BY status").fetchall()}
    except Exception as exc:
        out["trades_error"] = str(exc)[:200]
    return out


def _mt5_status(values) -> dict:
    """Non-blocking MT5 readiness probe for the panel."""
    blocked, reason = _mt5_precondition(values)
    return {"ready": not blocked, "reason": reason}


def _mt5_precondition(values) -> tuple:
    """MT5 may start only when the terminal is present, on a DEMO account, and the
    package is importable. Returns (blocked, reason). Never enables live trading."""
    import importlib.util
    if importlib.util.find_spec("MetaTrader5") is None:
        return True, ("MT5 package missing. Run: .venv\\Scripts\\python -m pip install MetaTrader5 "
                      "(Windows only), keep the MT5 terminal open on a DEMO account.")
    try:
        import MetaTrader5 as mt5
    except Exception:
        return True, "MetaTrader5 package is installed but could not be imported"

    from app.strategy.trainer import is_mt5_running
    if not is_mt5_running():
        return True, ("MetaTrader 5 terminal is not reachable. Open the terminal, log into a "
                      "DEMO account, then press Test connection again.")

    term = getattr(mt5, "terminal_info", lambda: None)()
    if term is None and not mt5.initialize(timeout=3000):
        return True, ("MetaTrader 5 terminal is not reachable. Open the terminal, log into a "
                      "DEMO account, then press Test connection again.")

    try:
        acc = mt5.account_info()
        if acc is not None and acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return True, ("Refusing to start: the MT5 account is NOT a demo account. "
                          "ai-trader is demo-only by design.")
    except Exception:
        pass

    # Note: We do NOT call mt5.shutdown() here because calling shutdown
    # closes the IPC pipe and terminates the terminal while the user is logging in.
    return False, ""


class OwnedProcess:
    def __init__(self):
        self.child = None
        self.logs = deque(maxlen=600)
        self.lock = threading.RLock()
        atexit.register(self.stop)

    def status(self):
        with self.lock:
            code = self.child.poll() if self.child else None
            running = self.child is not None and code is None
            return {"running": running, "pid": self.child.pid if running else None,
                    "exit_code": code, "logs": list(self.logs)}

    def start(self, root, values, testing=False):
        with self.lock:
            if self.status()["running"]:
                raise ValueError("Process already running")
            env = os.environ.copy()
            # .env values override inherited settings only in this owned child.
            env.update({FIELDS[k]: str(v) for k, v in values.items()})
            env["AI_PROVIDER"] = "openai-compat"
            env["LIVE_TRADING_ENABLED"] = "false"
            python_bin = sys.executable
            venv_bin = Path(root) / ".venv" / "Scripts" / "python.exe"
            if venv_bin.exists():
                python_bin = str(venv_bin)
            args = [python_bin, "-u", "-c", WORKER]
            if testing:
                args = [sys.executable, "-u", str(Path(root) / "main.py")]
            self.logs.clear()
            self.child = subprocess.Popen(args, cwd=root, env=env, shell=False,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            child = self.child
            key = values["api_key"]
            def drain():
                try:
                    while True:
                        chunk = child.stdout.readline(4096)
                        if not chunk:
                            break
                        text = chunk.decode("utf-8", errors="replace").rstrip()
                        text = text.replace(key, "[redacted]") if key else text
                        # Suppress credential-looking log entries as an extra guard.
                        if re.search(r"(?i)api[_ -]?key|authorization|bearer\s", text):
                            text = "[credential-bearing log omitted]"
                        with self.lock:
                            self.logs.append(text[:2048])
                finally:
                    child.stdout.close()
            threading.Thread(target=drain, daemon=True).start()

    def stop(self):
        with self.lock:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
            return self.status()


def register_control(app, get_repo):
    token = secrets.token_urlsafe(32)
    lock = threading.RLock()
    process = OwnedProcess()
    app.extensions["panel_process"] = process

    @app.before_request
    def local_guard():
        try:
            host = urlsplit(request.host_url).hostname
            local = is_loopback(host) and is_loopback(request.remote_addr or "")
        except (ValueError, TypeError):
            local = False
        origin = request.headers.get("Origin")
        if not local or (origin and origin != request.host_url.rstrip("/")) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="Local same-origin access required"), 403
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), token):
            return jsonify(error="CSRF token required; reload panel"), 403

    @app.after_request
    def secure_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/session")
    def session():
        return jsonify(csrf_token=token)

    @app.post("/api/settings")
    def post_settings():
        data = request.get_json(silent=True)
        with lock:
            if process.status()["running"]:
                return jsonify(error="Stop the process before changing settings; restart to apply"), 409
            try:
                values = merged_settings(app, data)
                persist_settings(Path(app.config["PANEL_ENV"]), data, values)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            except OSError:
                return jsonify(error="Cannot save settings; check file permissions"), 500
        return jsonify(public_settings(values))

    @app.get("/api/process")
    def process_status():
        data = process.status()
        if request.args.get("logs") == "0":
            data["logs"] = []
        return jsonify(data)

    @app.get("/api/logs")
    def api_logs():
        data = process.status()
        tail = request.args.get("tail", type=int)
        logs = list(data.get("logs", []))
        if tail and tail > 0:
            logs = logs[-tail:]
        return jsonify({"logs": logs})

    @app.post("/api/process/start")
    @app.post("/api/start")
    def process_start():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or set(data) - {"mode"}:
            return jsonify(error="Only mode may be supplied"), 400
        with lock:
            if process.status()["running"]:
                return jsonify(error="Process already running"), 409
            try:
                values = merged_settings(app, data)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            if values["mode"] == "mt5":
                blocked, why = _mt5_precondition(values)
                if blocked:
                    return jsonify(error=why), 409
            pressure = memory_health(app)["memory"]["percent"]
            if pressure is not None and pressure >= 92:
                return jsonify(error="Memory pressure too high (>=92%); stop other "
                                     "workloads first"), 409
            try:
                process.start(app.config["PANEL_ROOT"], values, app.testing)
            except (OSError, ValueError):
                return jsonify(error="Could not start local process"), 500
        return jsonify(process.status())

    @app.post("/api/process/stop")
    @app.post("/api/stop")
    def process_stop():
        with lock:
            return jsonify(process.stop())

    @app.post("/api/ai/models")
    def ai_models():
        from app.ai.client import OpenAICompatProvider
        try:
            values = merged_settings(app, request.get_json(silent=True))
            _validate_url(values["base_url"])
        except ValueError as exc:
            return jsonify(error=str(exc), models=[]), 400
        try:
            provider = OpenAICompatProvider(api_key=values["api_key"], base_url=values["base_url"], timeout=10)
            models = provider.list_models()
            return jsonify(models=sorted({m for m in models[:1000] if isinstance(m, str) and len(m) <= 256 and (not values["api_key"] or values["api_key"] not in m)}))
        except Exception:
            return jsonify(models=[], error="Model discovery failed; enter a model ID manually"), 502

    @app.post("/api/ai/test")
    def ai_test():
        from time import monotonic
        from app.ai.client import OpenAICompatProvider
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error="Invalid JSON object"), 400
        model = data.pop("model", None)
        try:
            values = merged_settings(app, data)
            _validate_url(values["base_url"])
            model = model or values["model_normal"]
            if not isinstance(model, str) or len(model) > 256 or any(ord(c) < 32 for c in model):
                raise ValueError("Invalid model")
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        started = monotonic()
        try:
            provider = OpenAICompatProvider(api_key=values["api_key"], base_url=values["base_url"], timeout=10)
            result = provider.chat(system="You are a connectivity test. Reply only OK.", user="Reply OK.", model=model, max_tokens=32, temperature=0)
            content = str(result.get("content", ""))
            if values["api_key"]:
                content = content.replace(values["api_key"], "[redacted]")
            return jsonify(ok=True, model=model, latency_ms=round((monotonic()-started)*1000), content=content[:2048], error=None)
        except Exception:
            return jsonify(ok=False, model=model, latency_ms=round((monotonic()-started)*1000), content="", error="AI request failed; check URL, key, model and provider availability"), 502

    @app.get("/api/last_error")
    def last_error():
        """The newest AI failure, with the model that caused it.

        A run can look healthy in the header while every symbol is failing on a
        dead model id. This is the one question the panel should answer without
        the operator reading raw lines.
        """
        repo = get_repo()
        try:
            key = settings(app)["api_key"]
        except ValueError:
            key = ""
        row = repo.conn.execute(
            "SELECT ts, task, model, status FROM ai_requests "
            "WHERE status IS NOT NULL AND status != 'ok' ORDER BY id DESC LIMIT 1").fetchone()
        recent = repo.conn.execute(
            "SELECT COUNT(*) AS n FROM ai_requests "
            "WHERE status IS NOT NULL AND status != 'ok' "
            "AND ts >= datetime('now', '-15 minutes')").fetchone()
        if row is None:
            return jsonify(failing=False, recent_failures=int(recent["n"] or 0))
        model = row["model"]
        # The reason lives only in the log line. A bare "error" row is useless
        # to an operator: surface the message the agent actually logged.
        reason = ""
        for line in reversed(list(process.logs)):
            try:
                entry = json.loads(line)
            except (TypeError, ValueError):
                continue
            if entry.get("level") != "ERROR":
                continue
            msg = str(entry.get("msg", ""))
            if "AI" in msg:
                reason = _strip_control_noise(msg)
                break
        return jsonify(failing=True, recent_failures=int(recent["n"] or 0),
                       ts=row["ts"], task=row["task"],
                       model=(model.replace(key, "[redacted]") if key and model else model),
                       status=row["status"], reason=reason)

    @app.get("/api/debug")
    def debug():
        try:
            limit = max(1, min(100, int(request.args.get("limit", 30))))
        except ValueError:
            return jsonify(error="Invalid limit"), 400
        repo = get_repo()
        result = {}
        columns = {"ai_requests": ("ai_requests", "id,ts,task,model,input_tokens,output_tokens,total_tokens,estimated_cost,symbol,status"),
                   "decisions": ("trade_decisions", "id,ts,symbol,decision,approved"),
                   "events": ("system_events", "id,ts,event")}
        try:
            key = settings(app)["api_key"]
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        for name, (table, cols) in columns.items():
            rows = repo.conn.execute(f"SELECT {cols} FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            result[name] = [{k: (v.replace(key, "[redacted]")[:2048] if key and isinstance(v, str) else v[:2048] if isinstance(v, str) else v) for k, v in dict(row).items()} for row in rows]
        result["diagnostics"] = _diagnostics(repo, key)
        return jsonify(result)

    @app.get("/api/mt5/status")
    def mt5_status():
        try:
            values = settings(app)
        except ValueError as exc:
            return jsonify(ready=False, reason=str(exc)), 400
        return jsonify(_mt5_status(values))

    @app.get("/api/health")
    def health():
        return jsonify(memory_health(app))

    @app.get("/api/settings")
    def get_settings():
        try:
            return jsonify(public_settings(settings(app)))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
