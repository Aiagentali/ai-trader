"""AI provider abstraction — OpenRouter/OpenAI/Anthropic-compatible (OpenAI chat API shape).

Model/task routing: cheap model for routine analysis, stronger model for trade decisions.
Same JSON schema enforced for all.
"""
import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

DEFAULT_MODELS = {
    "normal": "openai/gpt-4o-mini",       # routine hourly analysis
    "watch": "openai/gpt-4o-mini",
    "critical": "openai/gpt-4o",          # trade decisions / reviews
}

# Rough per-1M token pricing for cost estimates (USD)
PRICING = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
}


class AIError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class AIProvider(ABC):
    @abstractmethod
    def chat(self, system: str, user: str, model: str, max_tokens: int,
             temperature: float) -> dict:
        """Returns {'content': str, 'input_tokens': int, 'output_tokens': int, 'model': str}"""


class OpenAICompatProvider(AIProvider):
    """Works with OpenAI, OpenRouter, or any OpenAI-compatible endpoint."""

    def __init__(self, api_key: str = None, base_url: str = None, timeout: int = 60):
        self.api_key = api_key or os.environ.get("AI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AI_BASE_URL",
                         "https://api.orcarouter.ai/v1")).rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise AIError("AI_API_KEY not set")

    def list_models(self) -> list[str]:
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise AIError("Model list exceeds response limit")
            data = json.loads(raw)
            rows = data.get("data") if isinstance(data, dict) else data
            if isinstance(rows, dict):
                rows = rows.get("data")
            if not isinstance(rows, list):
                raise AIError("Invalid model list response")
            return list(dict.fromkeys(row["id"] for row in rows[:1000]
                        if isinstance(row, dict) and isinstance(row.get("id"), str)
                        and len(row["id"]) <= 256))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise AIError(f"Model discovery failed: HTTP {status}") from None
        except AIError:
            raise
        except Exception:
            raise AIError("Model discovery failed; check URL, credentials and response format") from None

    def chat(self, system: str, user: str, model: str, max_tokens: int = 1000,
             temperature: float = 0.2) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(1_000_001)
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()  # discard untrusted error body; never expose it in logs
            if status in (401, 403):
                raise AIError(f"AI request failed: HTTP {status} — authentication rejected; "
                              "check AI_API_KEY and provider permissions") from None
            if status == 429:
                raise AIError("AI request failed: HTTP 429 — rate limit/quota hit; "
                              "retry later with backoff", retryable=True) from None
            if status in (408, 500, 502, 503, 504):
                raise AIError(f"AI request failed: HTTP {status} — transient upstream "
                              "error; retry later", retryable=True) from None
            raise AIError(f"AI request failed: HTTP {status}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise AIError("AI transport failed; check endpoint/connectivity and retry",
                          retryable=True) from None
        except Exception:
            raise AIError("AI request failed; check provider configuration") from None

        if len(raw) > 1_000_000:
            raise AIError("AI response exceeds 1000000-byte limit")
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            choice = data["choices"][0]
            if choice.get("finish_reason", "stop") != "stop":
                raise AIError("AI completion incomplete or filtered; no decision accepted")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("missing content")
            usage = data.get("usage") or {}
            counts = [usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)]
            if any(type(n) is not int or n < 0 for n in counts):
                raise ValueError("invalid usage")
            return {"content": content, "input_tokens": counts[0],
                    "output_tokens": counts[1], "model": data.get("model") or model}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise AIError("Invalid AI response; expected a complete chat-completion JSON "
                          "object (optionally wrapped in data); check router format") from None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, (1.0, 2.0))
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)


def get_provider() -> AIProvider:
    kind = os.environ.get("AI_PROVIDER", "openai-compat")
    if kind == "openai-compat":
        return OpenAICompatProvider()
    raise AIError(f"unknown provider kind {kind}")
