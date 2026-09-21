from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

DEFAULT_THRESHOLDS = {
    "weather": 88,
    "warzone": 84,
    "apple": 88,
    "github": 82,
    "colombia": 90,
    "watcher": 88,
    "earthquake": 92,
    "package": 86,
    "system": 0,
}

# These floors keep an accidentally permissive stored preference or old .env
# value from turning Pulse back into an RSS-to-push relay.
STRICT_MIN_THRESHOLDS = {
    "weather": 88,
    "warzone": 84,
    "apple": 88,
    "github": 82,
    "colombia": 90,
    "watcher": 88,
    "earthquake": 92,
    "package": 86,
    "system": 0,
}

DEFAULT_ROLLING_NOTIFICATION_LIMITS = {
    "global": {"count": 4, "minutes": 60},
    "topic": {"count": 2, "minutes": 180},
    "tier": {},
}

DEFAULT_TOPIC_COOLING = {
    "window_minutes": 180,
    "trigger_count": 3,
    "threshold_step": 4,
    "max_threshold_bonus": 12,
    "decay_minutes": 360,
}

TOPIC_LABELS = {
    "weather": "Weather",
    "warzone": "Warzone / COD",
    "apple": "Apple / tech",
    "github": "Projects",
    "colombia": "Colombia",
    "watcher": "Watchers",
    "earthquake": "Earthquakes",
    "package": "Packages",
    "system": "Pulse",
}

DEFAULT_SEARCH_PROFILES = [
    {
        "id": "warzone-discovery",
        "label": "Warzone updates",
        "topic": "warzone",
        "queries": ["Warzone balance update", "Warzone weapon meta changes", "Warzone patch notes"],
        "keywords": ["warzone", "weapon", "balance", "patch", "meta", "season"],
        "active": True,
    },
    {
        "id": "apple-discovery",
        "label": "Apple and devices",
        "topic": "apple",
        "queries": ["Apple announcement today", "iPhone update", "Apple Watch update"],
        "keywords": ["apple", "iphone", "ios", "ipad", "mac", "watch", "airpods"],
        "active": True,
    },
    {
        "id": "ai-tech-discovery",
        "label": "AI and major tech",
        "topic": "apple",
        "queries": ["OpenAI latest release", "major AI model announcement", "major technology release"],
        "keywords": ["openai", "ai", "model", "release", "announcement", "technology"],
        "active": True,
    },
    {
        "id": "colombia-discovery",
        "label": "Colombia and Medellín",
        "topic": "colombia",
        "queries": ["Colombia major breaking news", "Medellín major event", "Antioquia emergency alert"],
        "keywords": ["colombia", "medellin", "antioquia", "emergency", "alert", "government"],
        "active": True,
    },
]

DEFAULT_TRACKED_ENTITIES = [
    {"id": "rev", "name": "REV", "aliases": ["REV"], "topic": "warzone", "boost": 18},
    {"id": "voyak", "name": "Voyak", "aliases": ["Voyak"], "topic": "warzone", "boost": 18},
    {"id": "warzone", "name": "Warzone", "aliases": ["Warzone", "Call of Duty Warzone"], "topic": "warzone", "boost": 8},
    {"id": "ps5-pro", "name": "PS5 Pro", "aliases": ["PS5 Pro", "PlayStation 5 Pro"], "topic": "apple", "boost": 10},
    {"id": "apple-watch-series-12", "name": "Apple Watch Series 12", "aliases": ["Apple Watch Series 12"], "topic": "apple", "boost": 14},
    {"id": "iphone-18", "name": "iPhone 18", "aliases": ["iPhone 18"], "topic": "apple", "boost": 14},
    {"id": "openai", "name": "OpenAI", "aliases": ["OpenAI", "GPT"], "topic": "apple", "boost": 12},
    {"id": "ultimate-macro", "name": "Ultimate Macro", "aliases": ["Ultimate Macro"], "topic": "github", "boost": 18},
    {"id": "aidenerm-mora", "name": "aidenERM/mora", "aliases": ["aidenERM/mora"], "topic": "github", "boost": 18},
]


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
        "QUIET_BYPASS_PRIORITY": int(os.environ.get("PULSE_QUIET_BYPASS_PRIORITY", "98")),
        "TOPIC_THRESHOLDS": {**DEFAULT_THRESHOLDS, **(_json("PULSE_TOPIC_THRESHOLDS_JSON", {}) or {})},
        "STRICT_MIN_THRESHOLDS": {**STRICT_MIN_THRESHOLDS, **(_json("PULSE_STRICT_MIN_THRESHOLDS_JSON", {}) or {})},
        "NOTIFICATION_COOLDOWNS": {**{
            "weather": 180,
            "warzone": 120,
            "apple": 180,
            "github": 120,
            "colombia": 240,
            "watcher": 240,
            "earthquake": 60,
            "package": 360,
            "system": 0,
        }, **(_json("PULSE_NOTIFICATION_COOLDOWNS_JSON", {}) or {})},
        "NOTIFICATION_MAX_AGE": {**{
            "weather": 180,
            "warzone": 1440,
            "apple": 1440,
            "github": 2880,
            "colombia": 720,
            "watcher": 720,
            "earthquake": 360,
            "package": 1440,
            "system": 1440,
        }, **(_json("PULSE_NOTIFICATION_MAX_AGE_JSON", {}) or {})},
        "ROLLING_NOTIFICATION_LIMITS": {**DEFAULT_ROLLING_NOTIFICATION_LIMITS, **(_json("PULSE_ROLLING_NOTIFICATION_LIMITS_JSON", {}) or {})},
        "TOPIC_COOLING": {**DEFAULT_TOPIC_COOLING, **(_json("PULSE_TOPIC_COOLING_JSON", {}) or {})},
        "DEVELOPMENT_OVERRIDE_DELTA": max(5, min(30, int(os.environ.get("PULSE_DEVELOPMENT_OVERRIDE_DELTA", "8")))),
        "TREND_WINDOW_HOURS": max(6, min(168, int(os.environ.get("PULSE_TREND_WINDOW_HOURS", "72")))),
        "TREND_BONUS": {"source": 4, "velocity": 2, "development": 3, "local": 2, "trajectory": 3},
        "MAX_NOTIFICATIONS_PER_RUN": max(1, min(5, int(os.environ.get("PULSE_MAX_NOTIFICATIONS_PER_RUN", "2")))),
        "URGENT_NOTIFY_SCORE": max(95, min(100, int(os.environ.get("PULSE_URGENT_NOTIFY_SCORE", "98")))),
        "MATERIAL_UPDATE_SCORE_DELTA": max(5, min(40, int(os.environ.get("PULSE_MATERIAL_UPDATE_SCORE_DELTA", "12")))),
        "MORNING_CATCHUP_ENABLED": _bool("PULSE_MORNING_CATCHUP_ENABLED", True),
        "MORNING_CATCHUP_WINDOW_MINUTES": max(15, min(180, int(os.environ.get("PULSE_MORNING_CATCHUP_WINDOW_MINUTES", "60")))),
        "VAPID_PUBLIC_KEY": os.environ.get("PULSE_VAPID_PUBLIC_KEY", ""),
        "VAPID_PRIVATE_KEY": os.environ.get("PULSE_VAPID_PRIVATE_KEY", ""),
        "VAPID_SUBJECT": os.environ.get("PULSE_VAPID_SUBJECT", ""),
        "WEATHER_LAT": float(os.environ.get("PULSE_WEATHER_LAT", "6.03131")),
        "WEATHER_LON": float(os.environ.get("PULSE_WEATHER_LON", "-75.43333")),
        "WEATHER_LABEL": os.environ.get("PULSE_WEATHER_LABEL", "La Ceja, Antioquia"),
        "WEATHER_LOOKAHEAD_HOURS": max(6, min(48, int(os.environ.get("PULSE_WEATHER_LOOKAHEAD_HOURS", "24")))),
        "WEATHER_HOT_C": float(os.environ.get("PULSE_WEATHER_HOT_C", "28")),
        "WEATHER_COLD_C": float(os.environ.get("PULSE_WEATHER_COLD_C", "12")),
        "WEATHER_RAIN_PROBABILITY": max(60, min(100, int(os.environ.get("PULSE_WEATHER_RAIN_PROBABILITY", "75")))),
        "WEATHER_RAIN_MM": max(0.5, float(os.environ.get("PULSE_WEATHER_RAIN_MM", "5"))),
        "WEATHER_WIND_KMH": max(30, float(os.environ.get("PULSE_WEATHER_WIND_KMH", "50"))),
        "EARTHQUAKE_ENABLED": _bool("PULSE_EARTHQUAKE_ENABLED", True),
        "EARTHQUAKE_FEED_URL": os.environ.get("PULSE_EARTHQUAKE_FEED_URL", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"),
        "EARTHQUAKE_LOCAL_RADIUS_KM": max(50, float(os.environ.get("PULSE_EARTHQUAKE_LOCAL_RADIUS_KM", "400"))),
        "EARTHQUAKE_LOCAL_MIN_MAG": max(3.5, float(os.environ.get("PULSE_EARTHQUAKE_LOCAL_MIN_MAG", "4.5"))),
        "EARTHQUAKE_MAJOR_MAG": max(5.5, float(os.environ.get("PULSE_EARTHQUAKE_MAJOR_MAG", "6.5"))),
        "GITHUB_REPOS": [x.strip() for x in os.environ.get("PULSE_GITHUB_REPOS", "").split(",") if x.strip()],
        "GITHUB_TOKEN": os.environ.get("PULSE_GITHUB_TOKEN", ""),
        "GITHUB_WEBHOOK_SECRET": os.environ.get("PULSE_GITHUB_WEBHOOK_SECRET", ""),
        "BRAVE_SEARCH_API_KEY": os.environ.get("PULSE_BRAVE_SEARCH_API_KEY", ""),
        "DISCOVERY_INTERVAL_MINUTES": max(60, min(1440, int(os.environ.get("PULSE_DISCOVERY_INTERVAL_MINUTES", "180")))),
        "DISCOVERY_MAX_QUERIES": max(1, min(20, int(os.environ.get("PULSE_DISCOVERY_MAX_QUERIES", "5")))),
        "DISCOVERY_MAX_RESULTS": max(1, min(10, int(os.environ.get("PULSE_DISCOVERY_MAX_RESULTS", "5")))),
        "DISCOVERY_FRESHNESS": os.environ.get("PULSE_DISCOVERY_FRESHNESS", "pw"),
        "DISCOVERY_ON_WATCHER": _bool("PULSE_DISCOVERY_ON_WATCHER", True),
        "SEARCH_PROFILES": _json("PULSE_SEARCH_PROFILES_JSON", DEFAULT_SEARCH_PROFILES) or DEFAULT_SEARCH_PROFILES,
        "TRACKED_ENTITIES": _json("PULSE_TRACKED_ENTITIES_JSON", DEFAULT_TRACKED_ENTITIES) or DEFAULT_TRACKED_ENTITIES,
        "RSS_SOURCES": _json("PULSE_RSS_SOURCES_JSON", []) or [],
        "URL_WATCHERS": _json("PULSE_URL_WATCHERS_JSON", []) or [],
        "PACKAGE_WATCHERS": _json("PULSE_PACKAGE_WATCHERS_JSON", []) or [],
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
