"""MT5-native mode tests (no real MT5 on CI — test safety gates & wiring)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestMT5NativeWiring:
    def test_mt5_mode_refused_without_terminal(self):
        """When MT5 is unavailable, build_mt5_stack must fail cleanly."""
        from unittest.mock import patch
        from app.execution.mt5_native import build_mt5_stack
        from app.database.repository import Repository
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        repo = Repository(db_path=path)
        try:
            with patch.dict("sys.modules", {"MetaTrader5": None}):
                build_mt5_stack(repo)
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "MetaTrader 5" in str(exc) or "MT5" in str(exc)
        assert raised, "expected RuntimeError when MT5 unavailable"

    def test_settings_mt5_mode_defaults_safe(self):
        from app.config.config_loader import load_settings
        s = load_settings()
        mode = s.get("execution", {}).get("mode")
        assert mode in ("simulation", "mt5")
        assert s.get("trading", {}).get("live_trading_enabled") is False
