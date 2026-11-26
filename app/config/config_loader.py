"""Configuration loader for ai-trader."""
import os
import yaml

APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS_FILE = os.path.join(APP_ROOT, "app", "config", "settings.yaml")
SYMBOLS_FILE = os.path.join(APP_ROOT, "app", "config", "symbols.yaml")
RISK_FILE = os.path.join(APP_ROOT, "app", "config", "risk.yaml")


def load_yaml(name: str) -> dict:
    path = os.path.join(APP_ROOT, "app", "config", name)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    return load_yaml("settings.yaml")


def load_symbols() -> dict:
    return load_yaml("symbols.yaml")


def load_risk() -> dict:
    return load_yaml("risk.yaml")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)
