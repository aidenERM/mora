from __future__ import annotations

import json
import hashlib
import hmac
from pathlib import Path

from werkzeug.security import generate_password_hash

from pulse_app.app import create_app
from pulse_app import discovery
from pulse_app.config import load_config
from pulse_app.rules import is_quiet_hours, score_item
from pulse_app.sources import parse_feed
from pulse_app.storage import connect, init_db


def make_client(tmp_path: Path, overrides: dict | None = None):
    config = {
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
    }
    config.update(overrides or {})
    app = create_app(config)
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


def test_change_password_replaces_old_password(tmp_path):
    client = make_client(tmp_path)
    login(client)
    changed = client.post(
        "/api/auth/password",
        json={"current_password": "test-password", "new_password": "new-short-password", "confirm_password": "new-short-password"},
    )
    assert changed.status_code == 200
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": "test-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "new-short-password"}).status_code == 200


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


def test_discovery_reads_pages_and_confirms_cross_source_result(tmp_path, monkeypatch):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database),
        "BRAVE_SEARCH_API_KEY": "test-key",
        "DISCOVERY_MAX_QUERIES": 1,
        "DISCOVERY_MAX_RESULTS": 5,
        "DISCOVERY_FRESHNESS": "pd",
        "SEARCH_PROFILES": [{"id": "warzone", "topic": "warzone", "queries": ["REV Warzone update"], "keywords": ["warzone", "update"], "active": True}],
        "TRACKED_ENTITIES": [{"id": "rev", "name": "REV", "aliases": ["REV"], "boost": 18}],
    })

    monkeypatch.setattr(discovery.BraveSearchProvider, "search", lambda self, query, count, freshness, search_lang: [
        {"title": "REV Warzone update", "url": "https://www.callofduty.com/blog/rev", "snippet": "official"},
        {"title": "REV Warzone update", "url": "https://www.reuters.com/world/rev", "snippet": "reported"},
    ])
    monkeypatch.setattr(discovery, "fetch_page", lambda url: {"title": "REV Warzone update", "summary": "Weapon balance changed.", "content": "REV weapon balance changed in Warzone.", "digest": url})

    candidates = discovery.collect_discovery_candidates(conn, config)
    assert len(candidates) == 1
    assert candidates[0]["relevant"] is True
    assert candidates[0]["metadata"]["confidence"] == "confirmed"
    assert len(candidates[0]["metadata"]["sources"]) == 2
    assert candidates[0]["metadata"]["entity_names"] == ["REV"]


def test_discovery_does_not_notify_single_community_claim(tmp_path, monkeypatch):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database),
        "BRAVE_SEARCH_API_KEY": "test-key",
        "DISCOVERY_MAX_QUERIES": 1,
        "DISCOVERY_MAX_RESULTS": 1,
        "SEARCH_PROFILES": [{"id": "warzone", "topic": "warzone", "queries": ["Warzone rumor"], "keywords": ["warzone"], "active": True}],
        "TRACKED_ENTITIES": [],
    })
    monkeypatch.setattr(discovery.BraveSearchProvider, "search", lambda self, query, count, freshness, search_lang: [
        {"title": "Warzone rumor", "url": "https://www.youtube.com/watch?v=abc", "snippet": "unconfirmed"},
    ])
    monkeypatch.setattr(discovery, "fetch_page", lambda url: {"title": "Warzone rumor", "summary": "A claim.", "content": "A claim with no independent confirmation.", "digest": "abc"})

    candidates = discovery.collect_discovery_candidates(conn, config)
    assert len(candidates) == 1
    assert candidates[0]["metadata"]["confidence"] == "rumor"
    assert candidates[0]["relevant"] is False
    assert candidates[0]["suppress_notification"] is True


def test_discovery_settings_feedback_and_github_webhook(tmp_path):
    client = make_client(tmp_path, {"GITHUB_WEBHOOK_SECRET": "webhook-secret", "GITHUB_REPOS": ["aidenERM/mora"]})
    login(client)
    settings = client.get("/api/discovery")
    assert settings.status_code == 200
    updated = client.put("/api/discovery", json={
        "profiles": [{"id": "test", "label": "Test", "topic": "warzone", "queries": ["REV update"], "keywords": ["REV"], "active": True}],
        "entities": [{"id": "rev", "name": "REV", "aliases": ["REV"], "topic": "warzone", "boost": 20}],
    })
    assert updated.status_code == 200
    assert updated.json["profiles"][0]["id"] == "test"

    conn = connect(tmp_path / "pulse.sqlite3")
    event = {
        "id": "feedback-event",
        "source_id": "test",
        "source_kind": "search",
        "topic": "warzone",
        "title": "REV update",
        "summary": "test",
        "body": "matched",
        "url": "https://example.test/rev",
        "canonical_key": "test:feedback-event",
        "score": 85,
        "priority": "high",
        "relevant": True,
        "metadata": {"entities": ["rev"], "entity_names": ["REV"]},
    }
    from pulse_app.storage import upsert_event
    upsert_event(conn, event)
    conn.commit()
    assert client.post("/api/events/feedback-event/feedback", json={"action": "follow", "entity_id": "rev"}).status_code == 200
    assert client.post("/api/events/feedback-event/feedback", json={"action": "less_like", "entity_id": "rev"}).status_code == 200
    assert "follow" in client.get("/api/learning").json["summary"]

    payload = {"action": "published", "repository": {"full_name": "aidenERM/mora", "html_url": "https://github.com/aidenERM/mora"}, "release": {"id": 42, "name": "v-test", "tag_name": "v-test", "body": "test release", "html_url": "https://github.com/aidenERM/mora/releases/tag/v-test"}}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"webhook-secret", raw, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "X-GitHub-Delivery": "delivery-1", "X-GitHub-Event": "release"}
    delivered = client.post("/api/webhooks/github", data=raw, headers={**headers, "Content-Type": "application/json"})
    assert delivered.status_code == 200
    assert delivered.json["created"] is True
    duplicate = client.post("/api/webhooks/github", data=raw, headers={**headers, "Content-Type": "application/json"})
    assert duplicate.status_code == 200
    assert duplicate.json["duplicate"] is True
