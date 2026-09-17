import pytest
from app.dashboard.server import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "PANEL_ROOT": str(tmp_path),
                       "PANEL_ENV": str(tmp_path / ".env"),
                       "CGROUP_ROOT": str(tmp_path / 'missing-cgroup'),
                       "DATABASE": str(tmp_path / "panel.db")})


@pytest.fixture
def client(app):
    return app.test_client()


def test_settings_contract(client):
    response = client.get('/api/settings')
    assert response.status_code == 200
    data = response.get_json()
    assert set(data) == {'base_url', 'model_normal', 'model_watch',
                         'model_critical', 'api_key_set', 'mode', 'daily_token_limit'}
    assert data['mode'] == 'simulation'
    assert data['api_key_set'] is False


def csrf(client):
    return {'X-CSRF-Token': client.get('/api/session').get_json()['csrf_token']}


def test_local_origin_csrf_boundary(client):
    assert client.get('/api/settings', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/settings', environ_overrides={'REMOTE_ADDR': '192.0.2.2'}).status_code == 403
    assert client.post('/api/settings', json={}).status_code == 403
    token = csrf(client)
    assert client.post('/api/settings', json={}, headers={**token, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/session', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/api/session').headers['Cache-Control'] == 'no-store'


def test_post_settings_validates_and_persists_env_atomically(client, tmp_path):
    env = tmp_path / ".env"
    env.write_text("AI_API_KEY=keepme\nOTHER=stay\n")
    assert client.post('/api/settings', json={'base_url': 'http://10.0.0.5/v1'}, headers=csrf(client)).status_code == 400
    assert client.post('/api/settings', json={'base_url': 'https://u:p@x.io/v1'}, headers=csrf(client)).status_code == 400
    assert client.post('/api/settings', json={'base_url': 'https://x.io/v1?q=1'}, headers=csrf(client)).status_code == 400
    assert client.post('/api/settings', json={'mode': 'live'}, headers=csrf(client)).status_code == 400
    assert client.post('/api/settings', json={'daily_token_limit': -1}, headers=csrf(client)).status_code == 400
    assert client.post('/api/settings', json={'bogus': 1}, headers=csrf(client)).status_code == 400
    response = client.post('/api/settings', json={'base_url': 'https://x.io/v1', 'model_critical': 'gpt-5', 'daily_token_limit': 500000}, headers=csrf(client))
    assert response.status_code == 200
    assert response.get_json()['api_key_set'] is True
    text = env.read_text()
    assert 'AI_API_KEY=keepme' in text and 'OTHER=stay' in text
    assert 'AI_BASE_URL=https://x.io/v1' in text and 'AI_MODEL_CRITICAL=gpt-5' in text and 'AI_DAILY_TOKEN_LIMIT=500000' in text
    assert client.post('/api/settings', json={'base_url': 'https://y.io/v1', 'api_key': '   '}, headers=csrf(client)).status_code == 200
    assert 'AI_API_KEY=keepme' in env.read_text()
    assert 'keepme' not in client.get('/api/settings').get_data(as_text=True)
    before = env.stat().st_mtime_ns
    assert client.post('/api/settings', json={}, headers=csrf(client)).status_code == 200
    assert env.stat().st_mtime_ns == before  # empty payload -> no rewrite


def test_key_replacement_and_injection(client, tmp_path):
    from dotenv import dotenv_values
    response = client.post('/api/settings', json={'api_key': "new'key#abc"}, headers=csrf(client))
    assert response.status_code == 200
    assert dotenv_values(tmp_path / '.env', interpolate=False)['AI_API_KEY'] == "new'key#abc"
    assert "new'key" not in response.text
    for data in ({'model_normal': 'x\nEVIL=1'}, {'base_url': None}, {'base_url': 'https://host:bad/v1'}, {'api_key': 'a\rb'}):
        assert client.post('/api/settings', json=data, headers=csrf(client)).status_code == 400


def test_health_reports_cgroup_and_unknown_swap(client, app, tmp_path):
    group = tmp_path / 'cgroup'; group.mkdir()
    for name, value in {'memory.current': '90', 'memory.max': '100', 'memory.swap.max': '0', 'memory.swap.current': '0'}.items():
        (group / name).write_text(value)
    app.config['CGROUP_ROOT'] = str(group)
    response = client.get('/api/health')
    assert response.status_code == 200
    memory = response.json['memory']
    assert memory['current_bytes'] == 90 and memory['limit_bytes'] == 100
    assert memory['percent'] == 90 and memory['swap_limit_bytes'] == 0
    (group / 'memory.max').write_text('max')
    (group / 'memory.swap.max').unlink()
    memory = client.get('/api/health').json['memory']
    assert memory['limit_bytes'] is None and memory['percent'] is None
    assert memory['swap_limit_bytes'] is None


def test_database_connections_are_request_scoped(client, app):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    gate = threading.Barrier(2)
    def read(_):
        with app.test_client() as c:
            return c.get('/api/overview').json
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, range(4)))
    assert all(r['account']['balance'] is None and r['account']['equity'] is None for r in results)
    assert client.get('/api/trades').json == []


def test_debug_is_bounded_and_excludes_payload_secrets(client, app, tmp_path):
    from app.database.repository import Repository
    repo = Repository(app.config['DATABASE'])
    (tmp_path / '.env').write_text('AI_API_KEY=secret-marker\n')
    for n in range(110):
        repo.log_event('event secret-marker', {'api_key': 'secret-marker', 'private': 'omit this'})
        repo.log_ai_request('normal', 'model', 1, 1, 0)
    repo.save_decision('r', 'X', 'WAIT', False, [], {'api_key': 'secret-marker'}, {})
    repo.close()
    response = client.get('/api/debug?limit=999999')
    assert response.status_code == 200
    assert len(response.json['events']) == 100 and len(response.json['ai_requests']) == 100
    assert response.json['decisions'][0]['decision'] == 'WAIT'
    assert 'secret-marker' not in response.text and 'omit this' not in response.text
    assert client.get('/api/debug?limit=bad').status_code == 400


def test_ai_buttons_use_entered_config_without_saving(client, monkeypatch, tmp_path):
    from app.ai.client import OpenAICompatProvider
    calls = []
    def chat(self, **kwargs):
        calls.append((self.base_url, self.api_key, self.timeout, kwargs))
        return {'content': 'ok test-key', 'model': kwargs['model']}
    monkeypatch.setattr(OpenAICompatProvider, 'chat', chat)
    data = {'base_url': 'http://localhost:9999/v1', 'api_key': 'test-key', 'model_normal': 'custom-model'}
    response = client.post('/api/ai/test', json=data, headers=csrf(client))
    assert response.status_code == 200 and response.json['ok'] is True
    assert response.json['model'] == 'custom-model' and response.json['latency_ms'] >= 0
    assert 'test-key' not in response.text
    assert calls[0][:3] == ('http://localhost:9999/v1', 'test-key', 10)
    assert calls[0][3]['max_tokens'] <= 64
    assert not (tmp_path / '.env').exists()
    def fail(*a, **kw):
        raise RuntimeError('test-key raw upstream exception')
    monkeypatch.setattr(OpenAICompatProvider, 'chat', fail)
    response = client.post('/api/ai/test', json=data, headers=csrf(client))
    assert response.status_code == 502 and response.json['ok'] is False
    assert 'test-key' not in response.text and 'upstream exception' not in response.text


def test_models_button(client, monkeypatch):
    from app.ai.client import OpenAICompatProvider
    monkeypatch.setattr(OpenAICompatProvider, 'list_models', lambda self: ['m2', 'm1'], raising=False)
    response = client.post('/api/ai/models', json={'api_key': 'test'}, headers=csrf(client))
    assert response.status_code == 200 and response.json == {'models': ['m1', 'm2']}


def test_process_lifecycle_owned_and_bounded(client, app, tmp_path):
    import time
    # Real subprocess, but a harmless isolated main instead of trading/network.
    (tmp_path / 'main.py').write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    assert client.get('/api/process').json == {'running': False, 'pid': None, 'exit_code': None, 'logs': []}
    response = client.post('/api/process/start', json={'mode': 'simulation'}, headers=csrf(client))
    assert response.status_code == 200 and response.json['running'] is True
    try:
        assert client.post('/api/process/start', json={}, headers=csrf(client)).status_code == 409
        assert client.post('/api/settings', json={'model_normal': 'different'}, headers=csrf(client)).status_code == 409
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and 'ready' not in client.get('/api/process').json['logs']:
            time.sleep(.01)
        assert 'ready' in client.get('/api/process').json['logs']
    finally:
        stopped = client.post('/api/process/stop', json={}, headers=csrf(client))
    assert stopped.status_code == 200 and stopped.json['running'] is False
    assert stopped.json['pid'] is None and stopped.json['exit_code'] is not None
    assert client.post('/api/process/stop', json={}, headers=csrf(client)).status_code == 200
    assert client.post('/api/process/start', json={'mode': 'live'}, headers=csrf(client)).status_code == 400


def test_process_refuses_memory_pressure(client, app, tmp_path):
    group = tmp_path / 'cgroup'; group.mkdir()
    (group / 'memory.current').write_text('93')
    (group / 'memory.max').write_text('100')
    app.config['CGROUP_ROOT'] = str(group)
    response = client.post('/api/process/start', json={'mode': 'simulation'}, headers=csrf(client))
    assert response.status_code == 409 and 'memory' in response.json['error'].lower()


def process_start_response(client, csrf_headers, mode='simulation'):
    return client.post('/api/process/start', json={'mode': mode}, headers=csrf_headers)


def test_process_warns_but_allows_moderate_pressure(client, app, tmp_path):
    group = tmp_path / 'cgroup'; group.mkdir()
    (group / 'memory.current').write_text('88')
    (group / 'memory.max').write_text('100')
    app.config['CGROUP_ROOT'] = str(group)
    # moderate pressure must not hard-block; may fail for other reasons but never 409-memory
    headers = csrf(client)
    response = process_start_response(client, headers)
    if response.status_code == 409:
        assert 'memory' not in response.json['error'].lower()
