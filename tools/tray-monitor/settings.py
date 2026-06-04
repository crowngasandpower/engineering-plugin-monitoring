"""
Persistent settings for Crown Monitoring tray app.
Stored in %APPDATA%\CrownMonitoring\settings.json so they survive updates.
Falls back to built-in defaults if the file doesn't exist.
"""

import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "grafana_url":       "http://localhost:9510",
    "auth_type":         "basic",   # "basic" | "token"
    "username":          "admin",
    "password":          "crown2026",
    "api_token":         "",
    "poll_interval":     30,
    "flash_on_critical": True,
}

_DIR  = Path(os.environ.get("APPDATA", ".")) / "CrownMonitoring"
_FILE = _DIR / "settings.json"
_live: dict = {}


def load() -> dict:
    global _live
    try:
        _live = {**DEFAULTS, **json.loads(_FILE.read_text())}
    except Exception:
        _live = dict(DEFAULTS)
    return dict(_live)


def save(data: dict) -> None:
    global _live
    _live = {**DEFAULTS, **data}
    _DIR.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(_live, indent=2))


def get() -> dict:
    return dict(_live) if _live else load()
