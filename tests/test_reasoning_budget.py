"""Regression: reasoning models burn hidden tokens and truncate JSON (finish_reason=length).

Client must transparently retry once with a doubled max_tokens budget instead of
failing the whole analysis, and prompt budgets must leave room for reasoning.
"""
import json
import urllib.error
import pytest

from app.ai.client import OpenAICompatProvider, AIError


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()
    def read(self, amt=-1):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _completion(content, finish_reason, prompt_tokens=100, completion_tokens=50):
    return {
        "model": "test-model",
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_length_finish_reason_retries_with_double_budget(monkeypatch):
    """Truncated (reasoning-token starvation) response → one retry with 2x max_tokens."""
    provider = OpenAICompatProvider(api_key="k", base_url="http://x/v1", timeout=5)
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["max_tokens"])
        if len(calls) == 1:
            return FakeHTTPResponse(_completion('{"symbol": "EURUSD", "decision": "WAI',
                                                "length"))
        return FakeHTTPResponse(_completion('{"symbol": "EURUSD", "decision": "WAIT", "confidence": 80}',
                                            "stop", completion_tokens=60))
    monkeypatch.setattr("app.ai.client.urllib.request.urlopen", fake_urlopen)

    result = provider.chat("sys", "user", "test-model", max_tokens=2200)
    assert result["content"].startswith('{"symbol"')
    assert calls == [2200, 4400]  # exactly one doubled retry


def test_stop_finish_reason_never_retries(monkeypatch):
    provider = OpenAICompatProvider(api_key="k", base_url="http://x/v1", timeout=5)
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body["max_tokens"])
        return FakeHTTPResponse(_completion('{"decision": "WAIT"}', "stop"))
    monkeypatch.setattr("app.ai.client.urllib.request.urlopen", fake_urlopen)

    provider.chat("sys", "user", "test-model", max_tokens=1500)
    assert calls == [1500]


def test_length_retry_still_truncated_raises_clean_error(monkeypatch):
    """Even after the doubled retry the answer is cut → clean, non-crashing AIError."""
    provider = OpenAICompatProvider(api_key="k", base_url="http://x/v1", timeout=5)
    seen = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        seen.append(body["max_tokens"])
        return FakeHTTPResponse(_completion('{"decision": "B', "length"))
    monkeypatch.setattr("app.ai.client.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(AIError):
        provider.chat("sys", "user", "test-model", max_tokens=1000)
    assert seen == [1000, 2000]
