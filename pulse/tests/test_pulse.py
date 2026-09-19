from __future__ import annotations

import json
from pathlib import Path

from werkzeug.security import generate_password_hash

from pulse_app.app import create_app
from pulse_app.rules import is_quiet_hours, score_item
from pulse_app.sources import parse_feed


def make_client(tmp_path: Path):
    app = create_app({
        "ENV": "test",
        "APP_URL": "http://localhost",
        "DATABASE_PATH": str(tmp_path / "pulse.sqlite3"),
        "SECRET_KEY": "test-secret",
        "PASSWORD_HASH": generate_password_hash("test-password"),
        "COOKIE_SECURE": False,
        "TRUST_PROXY": False,
        "VAPID_PUBLIC_KEY": "test-public",
        "VAPID_PRIVATE_KEY": "test-private",
        "VAPID_SUBJECT": "mailto:test@example.com",
    })
    return app.test_client()


def login(client):
    result = client.post("/api/auth/login", json={"password": "test-password"})
    assert result.status_code == 200


def test_auth_and_health(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/events").status_code == 401
    login(client)
    assert client.get("/api/events").json["events"] == []


def test_subscription_validation_and_preferences(tmp_path):
    client = make_client(tmp_path)
    login(client)
    bad = client.post("/api/subscriptions", json={"endpoint": "http://bad"})
    assert bad.status_code == 400
    subscription = {"endpoint": "https://push.example.test/abc", "keys": {"p256dh": "public", "auth": "auth"}}
    assert client.post("/api/subscriptions", json=subscription).status_code == 200
    result = client.put("/api/preferences", json={"quiet_start": "22:00", "quiet_end": "06:00", "topic_thresholds": {"warzone": 80}})
    assert result.status_code == 200
    assert result.json["preferences"]["topic_thresholds"]["warzone"] == 80


def test_relevance_and_quiet_hours():
    score, relevant, reason = score_item("warzone", "Warzone patch notes", "weapon balance update", ["warzone", "patch notes"])
    assert relevant and score >= 60 and "matched" in reason
    assert is_quiet_hours(__import__("datetime").datetime.fromisoformat("2026-09-18T04:00:00+00:00"), "23:00", "07:00", "UTC")


def test_feed_parser():
    raw = b"""<rss><channel><item><title>Test release</title><link>https://example.test/release?utm_source=x</link><description>Hello</description><guid>one</guid><pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    item = parse_feed(raw)[0]
    assert item["title"] == "Test release"
    assert item["url"] == "https://example.test/release"
    assert item["guid"] == "one"


def test_notification_route_is_exact_event_route():
    sw = Path(__file__).resolve().parents[1] / "static" / "sw.js"
    source = sw.read_text(encoding="utf-8")
    assert "notificationclick" in source
    assert "event.notification.data?.url" in source
    assert "clients.openWindow(target)" in source
