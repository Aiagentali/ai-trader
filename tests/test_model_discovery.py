import io
import json
import pytest
from app.ai.client import OpenAICompatProvider, AIError


def test_model_discovery_real_http_shape(monkeypatch):
    calls = []
    def respond(req, **kwargs):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps({'data':[{'id':'oc/nemotron-3.5-lightning-free'}, {'id':'second'}]}).encode())
    monkeypatch.setattr('urllib.request.urlopen', respond)
    client = OpenAICompatProvider('unit-test-key','http://localhost:1234/v1')
    assert client.list_models() == ['oc/nemotron-3.5-lightning-free','second']
    assert calls == ['http://localhost:1234/v1/models']
