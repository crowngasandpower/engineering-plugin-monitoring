"""
Persistent settings for Crown Monitoring tray app.
Stored in %APPDATA%\\CrownMonitoring\\settings.json so they survive updates.
"""

import json
import os
from pathlib import Path

_PROFILE_KEYS = (
    "name", "grafana_url", "auth_type", "username", "password", "api_token",
    "source_type", "pd_api_key", "pd_user_email", "pd_service_id",
    "toasts_enabled", "toast_pause_until",
)

PROFILE_DEFAULTS: dict = {
    "name":          "Live",
    "grafana_url":   "",
    "auth_type":     "basic",
    "username":      "",
    "password":      "",
    "api_token":     "",
    "source_type":   "grafana",
    "pd_api_key":    "",
    "pd_user_email": "",
    "pd_service_id": "",
    "toasts_enabled":    True,   # per-instance toast on/off
    "toast_pause_until": 0,      # per-instance timed pause (epoch seconds; 0 = not paused)
}

DEFAULTS: dict = {
    "profiles":            [dict(PROFILE_DEFAULTS)],
    "active_profile":      0,
    "poll_interval":       30,
    "flash_on_critical":   True,
    "toast_notifications": True,
    "toast_pause_until":   0,   # epoch seconds; toasts suppressed until this time (0 = not paused)
}

_DIR  = Path(os.environ.get("APPDATA", ".")) / "CrownMonitoring"
_FILE = _DIR / "settings.json"
_live: dict = {}


def _migrate(data: dict) -> dict:
    """Upgrade old flat-format files to the multi-profile structure."""
    if "grafana_url" in data and "profiles" not in data:
        profile = {k: data.get(k, PROFILE_DEFAULTS[k]) for k in _PROFILE_KEYS}
        profile["name"] = "Live"
        return {
            "profiles":          [profile],
            "active_profile":    0,
            "poll_interval":     data.get("poll_interval", 30),
            "flash_on_critical": data.get("flash_on_critical", True),
        }
    return data


def load() -> dict:
    global _live
    try:
        data  = _migrate(json.loads(_FILE.read_text()))
        _live = {**DEFAULTS, **data}
        if not isinstance(_live.get("profiles"), list) or not _live["profiles"]:
            _live["profiles"] = [dict(PROFILE_DEFAULTS)]
    except Exception:
        _live = {**DEFAULTS, "profiles": [dict(PROFILE_DEFAULTS)]}
    return dict(_live)


def _clamp_active(s: dict) -> int:
    idx = s.get("active_profile", 0)
    return max(0, min(idx, len(s.get("profiles", [])) - 1))


def get() -> dict:
    """Flat settings dict with the active profile's fields merged in (backwards-compatible)."""
    s = dict(_live) if _live else load()
    profiles = s.get("profiles", [dict(PROFILE_DEFAULTS)])
    idx      = _clamp_active(s)
    profile  = profiles[idx] if profiles else PROFILE_DEFAULTS
    s.update({k: profile.get(k, PROFILE_DEFAULTS[k]) for k in _PROFILE_KEYS if k != "name"})
    return s


def get_profiles() -> list:
    return list((_live if _live else load()).get("profiles", [dict(PROFILE_DEFAULTS)]))


def get_active_index() -> int:
    return _clamp_active(_live if _live else load())


def set_active(index: int) -> None:
    """Switch the active profile and persist."""
    global _live
    if not (0 <= index < len((_live or load()).get("profiles", []))):
        return
    _live["active_profile"] = index
    _persist()


def save(data: dict) -> None:
    """Save a flat settings dict — updates the active profile and global fields."""
    global _live
    s        = _live if _live else load()
    profiles = list(s.get("profiles", [dict(PROFILE_DEFAULTS)]))
    idx      = _clamp_active(s)
    existing_name = profiles[idx].get("name", "Live") if 0 <= idx < len(profiles) else "Live"
    profile  = {k: data.get(k, PROFILE_DEFAULTS[k]) for k in _PROFILE_KEYS}
    profile["name"] = data.get("name", existing_name)
    if 0 <= idx < len(profiles):
        profiles[idx] = profile
    _live["profiles"]          = profiles
    _live["poll_interval"]     = data.get("poll_interval", s.get("poll_interval", 30))
    _live["flash_on_critical"] = data.get("flash_on_critical", s.get("flash_on_critical", True))
    _persist()


def save_profile(index: int, profile_data: dict) -> None:
    """Write one profile by index without changing which is active."""
    global _live
    profiles = list(get_profiles())
    profile  = {k: profile_data.get(k, PROFILE_DEFAULTS[k]) for k in _PROFILE_KEYS}
    if 0 <= index < len(profiles):
        profiles[index] = profile
    else:
        profiles.append(profile)
    _live["profiles"] = profiles
    _persist()


def save_global(**kwargs) -> None:
    """Persist top-level (non-profile) settings: poll_interval, flash_on_critical, etc."""
    global _live
    for k, v in kwargs.items():
        _live[k] = v
    _persist()


def add_profile(name: str = "New Instance") -> int:
    """Append a blank profile, set it active, and return its index."""
    global _live
    profiles = list(get_profiles())
    profiles.append({**PROFILE_DEFAULTS, "name": name})
    _live["profiles"]       = profiles
    _live["active_profile"] = len(profiles) - 1
    _persist()
    return len(profiles) - 1


def delete_profile(index: int) -> None:
    """Delete a profile (no-op if it is the last one)."""
    global _live
    profiles = list(get_profiles())
    if len(profiles) <= 1:
        return
    profiles.pop(index)
    _live["profiles"] = profiles
    active = _live.get("active_profile", 0)
    if active >= len(profiles):
        _live["active_profile"] = len(profiles) - 1
    elif active > index:
        _live["active_profile"] = active - 1
    _persist()


def _persist() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(_live, indent=2))


def needs_setup() -> bool:
    """Return True if the active profile has no data source configured."""
    if not _FILE.exists():
        return True
    profiles = get_profiles()
    if not profiles:
        return True
    idx     = get_active_index()
    profile = profiles[idx] if 0 <= idx < len(profiles) else {}
    if profile.get("source_type") == "pagerduty":
        return not profile.get("pd_api_key", "").strip()
    return not profile.get("grafana_url", "").strip()
