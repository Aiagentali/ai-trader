import json
import pytest
from app.ai.client import GeminiProvider, get_provider, AIError


def test_get_provider_gemini(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("AI_API_KEY", "test-key")
    p = get_provider()
    assert isinstance(p, GeminiProvider)


def test_gemini_provider_chat_mock(monkeypatch):
    gemini_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps({"decision": "WAIT", "confidence": 50})}],
                    "role": "model"
                },
                "finishReason": "STOP"
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 35
        }
    }

    import io
    def mock_urlopen(req, **kwargs):
        assert req.headers.get("X-goog-api-key") == "test-key"
        assert "models/gemini-3.8-flash:generateContent" in req.full_url
        return io.BytesIO(json.dumps(gemini_resp).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    p = GeminiProvider(api_key="test-key")
    res = p.chat("system test", "user test", "gemini-3.8-flash")
    assert json.loads(res["content"])["decision"] == "WAIT"
    assert res["input_tokens"] == 120
    assert res["output_tokens"] == 35
    assert res["model"] == "gemini-3.8-flash"
