from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

DEFAULT_THRESHOLDS = {
    "weather": 60,
    "warzone": 72,
    "apple": 78,
    "github": 65,
    "colombia": 84,
    "watcher": 80,
    "system": 0,
}

TOPIC_LABELS = {
    "weather": "Weather",
    "warzone": "Warzone / COD",
    "apple": "Apple / tech",
    "github": "Projects",
    "colombia": "Colombia",
    "watcher": "Watchers",
    "system": "Pulse",
}


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _json(name: str, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be valid JSON") from exc
    return value


def load_config(test_config: dict | None = None) -> dict:
    _load_dotenv()
    config = {
        "ENV": os.environ.get("PULSE_ENV", "development"),
        "DEV_NO_AUTH": _bool("PULSE_DEV_NO_AUTH", False),
        "APP_URL": os.environ.get("PULSE_APP_URL", "http://127.0.0.1:8791").rstrip("/"),
        "BIND": os.environ.get("PULSE_BIND", "127.0.0.1:8791"),
        "DATABASE_PATH": os.environ.get("PULSE_DATABASE_PATH", str(ROOT / "data" / "pulse.sqlite3")),
        "SECRET_KEY": os.environ.get("PULSE_SECRET_KEY", ""),
        "PASSWORD_HASH": os.environ.get("PULSE_PASSWORD_HASH", ""),
        "COOKIE_SECURE": _bool("PULSE_COOKIE_SECURE", True),
        "TRUST_PROXY": _bool("PULSE_TRUST_PROXY", False),
        "TIMEZONE": os.environ.get("PULSE_TIMEZONE", "America/Bogota"),
        "POLL_MINUTES": max(5, int(os.environ.get("PULSE_POLL_MINUTES", "15"))),
        "QUIET_START": os.environ.get("PULSE_QUIET_START", "23:00"),
        "QUIET_END": os.environ.get("PULSE_QUIET_END", "07:00"),
        "QUIET_BYPASS_PRIORITY": int(os.environ.get("PULSE_QUIET_BYPASS_PRIORITY", "90")),
        "TOPIC_THRESHOLDS": {**DEFAULT_THRESHOLDS, **(_json("PULSE_TOPIC_THRESHOLDS_JSON", {}) or {})},
        "VAPID_PUBLIC_KEY": os.environ.get("PULSE_VAPID_PUBLIC_KEY", ""),
        "VAPID_PRIVATE_KEY": os.environ.get("PULSE_VAPID_PRIVATE_KEY", ""),
        "VAPID_SUBJECT": os.environ.get("PULSE_VAPID_SUBJECT", ""),
        "WEATHER_LAT": float(os.environ.get("PULSE_WEATHER_LAT", "4.7110")),
        "WEATHER_LON": float(os.environ.get("PULSE_WEATHER_LON", "-74.0721")),
        "WEATHER_LABEL": os.environ.get("PULSE_WEATHER_LABEL", "Bogotá"),
        "GITHUB_REPOS": [x.strip() for x in os.environ.get("PULSE_GITHUB_REPOS", "").split(",") if x.strip()],
        "GITHUB_TOKEN": os.environ.get("PULSE_GITHUB_TOKEN", ""),
        "RSS_SOURCES": _json("PULSE_RSS_SOURCES_JSON", []) or [],
        "URL_WATCHERS": _json("PULSE_URL_WATCHERS_JSON", []) or [],
        "COLOMBIA_RSS_URL": os.environ.get("PULSE_COLOMBIA_RSS_URL", ""),
        "SHORTCUT_TOKEN": os.environ.get("PULSE_SHORTCUT_TOKEN", ""),
        "SESSION_SECRET": os.environ.get("PULSE_SECRET_KEY") or secrets.token_urlsafe(32),
    }
    if test_config:
        config.update(test_config)
    if config["ENV"] == "production":
        required = ["SECRET_KEY", "PASSWORD_HASH", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"]
        missing = [key for key in required if not config[key]]
        if missing:
            raise RuntimeError("missing required Pulse environment variables: " + ", ".join(missing))
    return config
