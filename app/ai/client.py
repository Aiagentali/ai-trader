"""AI provider abstraction — OpenRouter/OpenAI/Anthropic-compatible (OpenAI chat API shape).

Model/task routing: cheap model for routine analysis, stronger model for trade decisions.
Same JSON schema enforced for all.
"""
import json
import os
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
    pass


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

    def chat(self, system: str, user: str, model: str, max_tokens: int = 1000,
             temperature: float = 0.2) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
        except Exception as e:
            raise AIError(f"AI request failed: {e}") from e

        # Try to parse as JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Response might have markdown fences or extra text — try to extract JSON
            import re
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    raise AIError(f"AI response is not valid JSON: {raw[:200]}")
            else:
                # Try to find raw JSON object in the text
                brace_start = raw.find('{')
                if brace_start >= 0:
                    try:
                        data = json.loads(raw[brace_start:])
                    except json.JSONDecodeError:
                        raise AIError(f"AI response is not valid JSON: {raw[:200]}")
                else:
                    raise AIError(f"AI response is not valid JSON: {raw[:200]}")

        usage = data.get("usage", {})
        return {
            "content": data["choices"][0]["message"]["content"],
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "model": data.get("model", model),
        }


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, (1.0, 2.0))
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)


def get_provider() -> AIProvider:
    kind = os.environ.get("AI_PROVIDER", "openai-compat")
    if kind == "openai-compat":
        return OpenAICompatProvider()
    raise AIError(f"unknown provider kind {kind}")
