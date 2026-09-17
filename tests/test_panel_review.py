"""Independent review regressions for the local panel."""
import pytest
from tests.test_panel import app, client, csrf


def test_launcher_rejects_public_host(monkeypatch):
    import panel
    monkeypatch.setattr('sys.argv', ['panel.py', '--host', '0.0.0.0', '--no-browser'])
    monkeypatch.setattr(panel, 'create_app', lambda: pytest.fail('must reject before creating app'))
    with pytest.raises(SystemExit):
        panel.main()


@pytest.mark.parametrize('raw', ['', 'nonsense', None])
def test_bad_env_limit_returns_controlled_error(client, tmp_path, raw):
    (tmp_path / '.env').write_text('AI_DAILY_TOKEN_LIMIT' + ('=' + raw if raw is not None else '') + '\n')
    response = client.get('/api/settings')
    assert response.status_code == 400
    assert response.json['error'] == 'Invalid AI_DAILY_TOKEN_LIMIT in environment; set a positive integer <= 1000000000'
    assert response.status_code == 400


def test_default_panel_paths_point_to_project():
    from pathlib import Path
    from app.dashboard.server import create_app
    application = create_app()
    root = Path(__file__).resolve().parents[1]
    assert Path(application.config['PANEL_ROOT']) == root
    assert Path(application.config['PANEL_ENV']) == root / '.env'
