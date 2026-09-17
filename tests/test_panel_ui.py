"""Local console must expose usable configuration, not just monitoring."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_console_has_configuration_and_external_safe_script():
    html = (ROOT / 'app/dashboard/templates/index.html').read_text()
    assert 'id="ai-settings"' in html
    for name in ('base_url', 'api_key', 'model_normal', 'model_watch', 'model_critical'):
        assert f'name="{name}"' in html
    assert 'type="password"' in html
    assert '/static/panel.js' in html
    assert 'id="test-ai"' in html
    assert 'id="process-logs"' in html
    assert 'id="memory-label"' in html
    assert '<script>' not in html
