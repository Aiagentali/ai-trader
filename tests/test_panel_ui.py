"""Local console must expose usable configuration and real control, not just monitoring."""
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


def test_panel_can_drive_the_whole_trader():
    """Starting/stopping, MT5 readiness and diagnostics must live in the panel."""
    html = (ROOT / 'app/dashboard/templates/index.html').read_text()
    assert 'id="start-agent"' in html
    assert 'id="stop-agent"' in html
    assert 'id="mt5-status"' in html
    assert 'id="diagnostics"' in html
    assert 'id="ai-alert"' in html


def test_debug_tab_has_usable_log_controls():
    html = (ROOT / 'app/dashboard/templates/index.html').read_text()
    for control in ('id="log-filter"', 'id="log-copy"', 'id="log-clear"', 'id="log-state"'):
        assert control in html


def test_panel_script_has_no_inline_code_and_no_inner_html():
    js = (ROOT / 'app/dashboard/static/panel.js').read_text()
    assert 'innerHTML' not in js
    assert 'eval(' not in js
