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

    def _raise_http(self, status: int) -> None:
        """Translate an HTTP status into a safe, actionable, credential-free AIError."""
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
        """One request; on finish_reason=length, retry ONCE with a doubled budget.

        Reasoning models (e.g. nex-n2.5) spend most of max_tokens on hidden
        reasoning tokens, so a 2200-token budget can cut the JSON in half.
        A single doubled retry fixes that without inflating every request.
        """
        budget = int(max_tokens)
        for attempt in (0, 1):
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "max_tokens": budget,
                "temperature": temperature,
                "stream": False,
            }
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(
                            f"{self.base_url}/chat/completions",
                            data=json.dumps(payload).encode(),
                            headers={"Authorization": f"Bearer {self.api_key}",
                                     "Content-Type": "application/json"}),
                        timeout=self.timeout) as resp:
                    raw = resp.read(1_000_001)
            except urllib.error.HTTPError as e:
                status = e.code
                e.close()  # discard untrusted body; never surface it
                if attempt == 1:
                    self._raise_http(status)  # never returns
                continue
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
                finish = choice.get("finish_reason", "stop")
                content = choice["message"].get("content")
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                raise AIError("Invalid AI response; expected a complete chat-completion JSON "
                              "object (optionally wrapped in data); check router format") from None

            if finish == "length" and attempt == 0:
                budget = min(budget * 2, 8_000)
                continue
            if finish != "stop":
                raise AIError("AI completion incomplete or filtered; no decision accepted")
            if not isinstance(content, str) or not content.strip():
                raise AIError("AI returned an empty completion; retry the analysis")
            usage = data.get("usage") or {}
            counts = [usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)]
            if any(type(n) is not int or n < 0 for n in counts):
                raise AIError("Invalid AI response; token usage malformed")
            return {"content": content, "input_tokens": counts[0],
                    "output_tokens": counts[1], "model": data.get("model") or model}
        raise AIError("AI completion incomplete after retry; no decision accepted")


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, (1.0, 2.0))
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)


def get_provider() -> AIProvider:
    kind = os.environ.get("AI_PROVIDER", "openai-compat")
    if kind == "openai-compat":
        return OpenAICompatProvider()
    raise AIError(f"unknown provider kind {kind}")
