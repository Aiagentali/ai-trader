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


# Truncated completions are retried with a larger budget before giving up.
MAX_BUDGET_ESCALATIONS = 2
MAX_TOKEN_BUDGET = 8_000


class AIError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable
        # partial text of a truncated completion, when the provider returned one —
        # lets the caller attempt a JSON repair instead of discarding the call
        self.partial = None
        self.reasoning_tokens = 0


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
        """Send a chat request, escalating the token budget while the model truncates.

        Reasoning models (nex-n2.5, nemotron) spend 40-70% of max_tokens on hidden
        reasoning tokens, so a 2200-token budget routinely cuts the JSON in half
        (finish_reason="length"). The budget is doubled up to MAX_BUDGET_ESCALATIONS
        times; a still-truncated final attempt raises AIError carrying the partial
        text so the caller can attempt a JSON repair instead of losing the call.
        """
        budget = int(max_tokens)
        partial = None
        for attempt in range(MAX_BUDGET_ESCALATIONS + 1):
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
                if attempt < MAX_BUDGET_ESCALATIONS:
                    continue  # transient upstream blip: same budget, one more try
                self._raise_http(status)  # never returns
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
                usage = data.get("usage") or {}
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                raise AIError("Invalid AI response; expected a complete chat-completion JSON "
                              "object (optionally wrapped in data); check router format") from None

            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            if finish == "length":
                partial = content if isinstance(content, str) else None
                if attempt < MAX_BUDGET_ESCALATIONS:
                    # reasoning tokens are invisible overhead — budget must clear them
                    headroom = int(reasoning * 1.5) + 512
                    budget = min(max(budget * 2, budget + headroom), MAX_TOKEN_BUDGET)
                    continue
                err = AIError("AI completion incomplete or filtered; no decision accepted")
                err.partial = partial
                err.reasoning_tokens = reasoning
                raise err
            if finish != "stop":
                err = AIError("AI completion incomplete or filtered; no decision accepted")
                err.partial = partial
                raise err
            if not isinstance(content, str) or not content.strip():
                raise AIError("AI returned an empty completion; retry the analysis")
            counts = [usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)]
            if any(type(n) is not int or n < 0 for n in counts):
                raise AIError("Invalid AI response; token usage malformed")
            return {"content": content, "input_tokens": counts[0],
                    "output_tokens": counts[1], "model": data.get("model") or model,
                    "reasoning_tokens": reasoning}
        err = AIError("AI completion incomplete after retries; no decision accepted")
        err.partial = partial
        raise err


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, (1.0, 2.0))
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)


def get_provider() -> AIProvider:
    kind = os.environ.get("AI_PROVIDER", "openai-compat")
    if kind == "openai-compat":
        return OpenAICompatProvider()
    raise AIError(f"unknown provider kind {kind}")
