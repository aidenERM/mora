from __future__ import annotations

import json
import hashlib
import hmac
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

from pulse_app.app import create_app
from pulse_app import discovery, sources
from pulse_app.config import load_config
from pulse_app.rules import apply_preference_adjustments, domain_adjustment, evaluate_notification, is_quiet_hours, notification_copy, should_notify, score_item
from pulse_app.sources import parse_feed
from pulse_app.integrations import classify_apple_mail_message, classify_gmail_message, consume_oauth_state, create_oauth_state, google_authorization_url, normalize_companion_payload, normalize_icloud_calendar_event, normalize_icloud_contact, normalize_location_payload, prepare_event_candidate
from pulse_app.storage import connect, create_morning_catchup, game_event_candidates, get_event, get_preferences, init_db, list_notification_decisions, mark_notified, pending_events, prune_history, record_notification_decision, set_context_signal, upsert_event, upsert_package, upsert_package_record, upsert_purchase, upsert_purchase_record, upsert_person, list_people, set_person_importance, upsert_game_event


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


def test_database_initialization_is_idempotent(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    init_db(database)
    conn = connect(database)
    columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_events_pending)").fetchall()]
    assert columns == ["notification_pending", "suppress_notification", "score"]


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


def test_message_reply_draft_is_review_only_and_falls_back_safely(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    login(client)
    database = tmp_path / "pulse.sqlite3"
    conn = connect(database)
    event, _ = upsert_event(conn, _decision_event("mail-draft", topic="security", score=90))
    conn.execute("UPDATE events SET metadata=? WHERE id=?", (json.dumps({"sender": "alerts@example.test", "gmail_message_id": "gmail-1"}), event["id"]))
    conn.commit()
    monkeypatch.setattr("pulse_app.app.classify_or_fallback", lambda *args, **kwargs: (None, {"safe_to_continue": True}))
    result = client.post("/api/events/mail-draft/draft-reply", json={})
    assert result.status_code == 200
    assert result.json["needs_review"] is True
    assert "send" not in result.json
    assert result.json["generated_by"] == "deterministic fallback"
    assert client.post("/api/events/mail-draft/draft-reply", json={}).json["metadata"]["ai_failed"] is True


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


def test_integrations_status_pairing_and_context(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/integrations").status_code == 401
    login(client)
    status = client.get("/api/integrations")
    assert status.status_code == 200
    assert {item["provider"] for item in status.json["integrations"]} >= {"apple", "google", "aws-bedrock"}
    challenge = client.post("/api/companion/pair/start", json={})
    assert challenge.status_code == 200
    redeemed = client.post("/api/companion/pair/redeem", json={"code": challenge.json["code"], "label": "test iPhone"})
    assert redeemed.status_code == 200
    context = client.post("/api/companion/context", json={"mode": "outside", "confidence": 0.9, "permissions": {"calendar": "granted"}, "signals": {"battery": {"level": "0.4", "charging": "false"}}}, headers={"Authorization": "Bearer " + redeemed.json["token"]})
    assert context.status_code == 200
    assert next(item for item in context.json["context"] if item["kind"] == "mode")["value"]["mode"] == "outside"
    assert client.get("/api/integrations").json["devices"][0]["metadata"]["permissions"]["calendar"] == "granted"
    assert client.post("/api/companion/context", json={"mode": "home"}, headers={"Authorization": "Bearer bad"}).status_code == 401
    location = client.post("/api/location", json={"latitude": 6.031314, "longitude": -75.433331, "accuracy": 20})
    assert location.status_code == 200
    assert next(item for item in location.json["context"] if item["kind"] == "location")["value"]["coarse"] is True
    assert client.delete("/api/location").status_code == 200


def test_gmail_classification_and_lifecycle_storage(tmp_path):
    message = {"id": "m1", "snippet": "Your package is out for delivery 1Z1234567890123456", "payload": {"headers": [{"name": "Subject", "value": "Package update"}, {"name": "From", "value": "store@example.test"}]}}
    classified = classify_gmail_message(message)
    assert classified["topic"] == "package"
    assert "body" not in classified
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    package_id = upsert_package(conn, classified["tracking_number"], "store@example.test", status="out_for_delivery", source="gmail")
    assert upsert_package(conn, classified["tracking_number"], "store@example.test", status="delivered", source="gmail") == package_id
    purchase_id = upsert_purchase(conn, "gmail:m2", merchant="store@example.test", title="Order confirmation", lifecycle="ordered")
    assert upsert_purchase(conn, "gmail:m2", merchant="store@example.test", title="Order confirmation", lifecycle="shipped") == purchase_id
    conn.commit()
    assert conn.execute("SELECT id FROM packages").fetchone()["id"] == package_id
    assert conn.execute("SELECT id FROM purchases").fetchone()["id"] == purchase_id


def test_oauth_state_is_single_use(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    state = create_oauth_state(conn, "google")
    assert consume_oauth_state(conn, "google", state) == {}
    assert consume_oauth_state(conn, "google", state) is None


def test_google_authorization_requests_enabled_apis():
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(google_authorization_url({"GOOGLE_CLIENT_ID": "client", "GOOGLE_REDIRECT_URI": "https://pulse.moralife.uk/api/integrations/google/callback"}, "state" )).query)
    assert "https://www.googleapis.com/auth/gmail.readonly" in query["scope"][0]
    assert "https://www.googleapis.com/auth/calendar.readonly" in query["scope"][0]
    assert "https://www.googleapis.com/auth/contacts.readonly" in query["scope"][0]


def test_icloud_normalization_reuses_mail_classification():
    calendar = normalize_icloud_calendar_event({"uid": "event-1", "summary": "School meeting", "start": "2026-09-21T14:00:00Z", "end": "2026-09-21T15:00:00Z", "location": "School", "calendar": "Family"})
    contact = normalize_icloud_contact({"uid": "contact-1", "fn": "Aiden", "emails": ["aiden@example.test"], "phones": ["+57 300"], "importance": "important"})
    mail = classify_apple_mail_message({"id": "mail-1", "snippet": "Your package is out for delivery", "payload": {"headers": [{"name": "Subject", "value": "Package update"}, {"name": "From", "value": "store@example.test"}]}})
    assert calendar["event_id"] == "event-1" and calendar["calendar"] == "Family"
    assert contact["name"] == "Aiden" and contact["importance"] == "important"
    assert mail["topic"] == "package"


def test_shortcut_setup_and_authentication(tmp_path):
    client = make_client(tmp_path, {"SHORTCUT_TOKEN": "shortcut-test-token"})
    login(client)
    setup = client.get("/api/shortcut/setup")
    assert setup.status_code == 200 and setup.json["endpoint"].endswith("/api/shortcut/context")
    assert client.post("/api/shortcut/context", json={"mode": "home"}, headers={"X-Pulse-Shortcut-Token": setup.json["token"]}).status_code == 200
    assert client.post("/api/shortcut/context", json={"mode": "home"}, headers={"X-Pulse-Shortcut-Token": "bad"}).status_code == 403


def test_bedrock_structured_validation(monkeypatch):
    from pulse_app import bedrock

    class FakeClient:
        def converse(self, **_kwargs):
            return {"output": {"message": {"content": [{"text": '{"status":"ok"}'}]}}, "usage": {"inputTokens": 2, "outputTokens": 3}, "stopReason": "end_turn"}

    monkeypatch.setattr(bedrock, "_client", lambda _config: FakeClient())
    bedrock._CACHE.clear()
    value, metadata = bedrock.invoke_json({"AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": "openai.gpt-5.6-luna", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}, "health", {}, {"status": "string"})
    assert value == {"status": "ok"}
    assert metadata["output_tokens"] == 3
    _, cached = bedrock.invoke_json({"AWS_REGION": "us-east-1", "BEDROCK_MODEL_ID": "openai.gpt-5.6-luna", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}, "health", {}, {"status": "string"})
    assert cached["cached"] is True


def test_fixture_inputs_use_existing_normalizers():
    fixtures = json.loads((Path(__file__).parent / "fixtures" / "integration_inputs.json").read_text(encoding="utf-8"))
    def message(item):
        return {"id": item["id"], "snippet": item["snippet"], "payload": {"headers": [{"name": "Subject", "value": item["subject"]}, {"name": "From", "value": "fixture@example.test"}]}}
    assert classify_gmail_message(message(fixtures["package"]))["topic"] == "package"
    assert classify_gmail_message(message(fixtures["purchase"]))["topic"] == "purchase"
    assert classify_gmail_message(message(fixtures["school"]))["topic"] == "school"
    assert classify_gmail_message(message(fixtures["travel"]))["topic"] == "travel"
    assert normalize_companion_payload(fixtures["apple_context"])["signals"]["battery"]["charging"] == "false"


def test_bedrock_failure_is_safe_and_config_is_explicit(monkeypatch):
    from pulse_app import bedrock
    monkeypatch.setattr(bedrock, "invoke_json", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    value, metadata = bedrock.classify_or_fallback({"BEDROCK_MODEL_ID": "openai.gpt-5.6-luna", "AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}, "health", {}, {"status": "string"})
    assert value is None and metadata["safe_to_continue"] and metadata["fallback"] == "deterministic"


def test_context_normalization_precedence_and_expiry(tmp_path):
    payload = normalize_companion_payload({"mode": "outside", "confidence": 0.9, "permissions": {"calendar": "granted"}, "signals": {"battery": {"level": "0.4"}}})
    assert payload["mode"] == "outside" and payload["permissions"]["calendar"] == "granted"
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    set_context_signal(conn, "mode", {"mode": "sleep"}, "apple-companion", 0.98, "2099-01-01T00:00:00+00:00")
    set_context_signal(conn, "mode", {"mode": "outside"}, "apple-companion", 0.98, "2099-01-01T00:00:00+00:00")
    from pulse_app.integrations import active_context
    assert active_context(conn)[0]["value"]["mode"] == "outside"
    set_context_signal(conn, "battery", {"level": "0.1"}, "shortcut", 0.5, "2000-01-01T00:00:00+00:00")
    assert all(item["kind"] != "battery" for item in active_context(conn))


def test_location_is_coarse_and_expires(tmp_path):
    location = normalize_location_payload({"latitude": 6.031314, "longitude": -75.433331, "accuracy": 18}, 30)
    assert location["value"] == {"latitude": 6.031, "longitude": -75.433, "accuracy_m": 18.0, "coarse": True}
    assert location["expires_at"] > location["observed_at"]
    with pytest.raises(ValueError):
        normalize_location_payload({"latitude": 120, "longitude": 0}, 30)


def test_recent_location_context_can_raise_weather_relevance():
    candidate = {"topic": "weather", "score": 84, "metadata": {}, "body": "rain"}
    prepared = prepare_event_candidate(candidate, [{"kind": "location", "value": {"coarse": True}}])
    assert prepared["score"] == 89
    assert prepared["metadata"]["score_components"]["context_preparation"] == 5


def test_package_and_purchase_state_merges_and_suppresses_duplicates(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    first = upsert_package_record(conn, "1Z 999-AA10123456784", "UPS", carrier="ups", status="in transit")
    duplicate = upsert_package_record(conn, "1Z999AA10123456784", "another sender", carrier="UPS", status="in transit")
    changed = upsert_package_record(conn, "1Z999AA10123456784", "UPS", status="delayed")
    assert first["id"] == duplicate["id"] == changed["id"]
    assert duplicate["changed"] is False and changed["changed"] is True and len(changed["history"]) == 2
    purchase = upsert_purchase_record(conn, "message-1", merchant="store", title="Phone", order_id="ORDER-1", lifecycle="ordered")
    merged = upsert_purchase_record(conn, "message-2", merchant="store", title="Phone", order_id="ORDER-1", lifecycle="shipped", package_id=first["id"])
    assert purchase["id"] == merged["id"] and merged["changed"] is True


def test_event_preparation_and_sleep_aware_delivery(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    candidate = {"topic": "weather", "score": 80, "priority": "high", "metadata": {"score_components": {}}}
    prepare_event_candidate(candidate, [{"kind": "mode", "value": {"mode": "outside"}}])
    assert candidate["score"] == 88 and candidate["metadata"]["score_components"]["context_preparation"] == 8
    set_context_signal(conn, "mode", {"mode": "sleep"}, "apple-companion", 0.99, "2099-01-01T00:00:00+00:00")
    event, _ = upsert_event(conn, _decision_event("sleep-held", topic="warzone", score=88))
    config = load_config({"DATABASE_PATH": str(database), "ROLLING_NOTIFICATION_LIMITS": {}, "NOTIFICATION_COOLDOWNS": {"warzone": 0}})
    allowed, reason, trace = evaluate_notification(event, get_preferences(conn, config), datetime(2026, 9, 20, 12, tzinfo=timezone.utc), config, conn)
    assert not allowed and reason == "sleep context" and trace["context_delivery"]["affected"]
    set_context_signal(conn, "mode", {"mode": "outside"}, "apple-companion", 0.99, "2099-01-01T00:00:00+00:00")
    allowed, _, _ = evaluate_notification(event, get_preferences(conn, config), datetime(2026, 9, 20, 12, tzinfo=timezone.utc), config, conn)
    assert allowed


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


def test_default_high_value_sources_are_official_and_domain_scoped():
    configured = {item["id"]: item for item in sources.configured_sources({})}
    assert configured["discord-blog"]["url"] == "https://discord.com/blog"
    assert configured["discord-blog"]["topic"] == "discord"
    assert configured["discord-blog"]["trust"] == "primary"
    assert configured["openai-news-rss"]["url"] == "https://openai.com/news/rss.xml"
    assert configured["openai-news-rss"]["topic"] == "openai"
    assert configured["discord-blog-rss"]["url"] == "https://discord.com/blog/rss.xml"
    assert configured["discord-blog-rss"]["topic"] == "discord"
    assert configured["rocket-league-news-search"]["topic"] == "rocket_league"
    assert configured["instagram-major-news-search"]["topic"] == "instagram"
    assert configured["unstable-smp-news-search"]["topic"] == "unstable_smp"
    assert all(configured[key]["trust"] == "reliable_secondary" for key in ("rocket-league-news-search", "instagram-major-news-search", "unstable-smp-news-search"))
    assert configured["github-status"]["topic"] == "service_status"
    assert configured["openai-status"]["topic"] == "service_status"
    assert configured["discord-status"]["topic"] == "service_status"


def test_personal_discovery_profiles_keep_domains_separate():
    profiles = {item["id"]: item for item in load_config({})["SEARCH_PROFILES"]}
    assert profiles["instagram-discovery"]["topic"] == "instagram"
    assert profiles["discord-discovery"]["topic"] == "discord"
    assert profiles["rocket-league-discovery"]["topic"] == "rocket_league"
    assert profiles["unstable-smp-discovery"]["topic"] == "unstable_smp"
    assert all("discord" not in " ".join(profiles[key]["queries"]).casefold() for key in ("instagram-discovery",))
    assert all("unstable" not in " ".join(profiles[key]["queries"]).casefold() for key in ("rocket-league-discovery",))


def test_openai_profile_rejects_editorial_case_studies_but_keeps_product_changes():
    low, _ = domain_adjustment({"topic": "openai", "title": "How a researcher uses ChatGPT", "summary": "A case study about adoption."})
    high, _ = domain_adjustment({"topic": "openai", "title": "Introducing ChatGPT Images", "summary": "A new model release and availability update."})
    assert low < 0 and high > 0


def test_ios_only_allows_confirmed_leaks():
    rumor, _ = domain_adjustment({"topic": "ios", "title": "iOS leak", "summary": "An unconfirmed rumor."})
    confirmed, _ = domain_adjustment({"topic": "ios", "title": "Confirmed iOS leak", "summary": "Apple confirmed the feature."})
    assert rumor < 0 and confirmed > 0


def test_service_status_only_rewards_active_incidents():
    healthy, _ = domain_adjustment({"topic": "service_status", "title": "All systems operational", "summary": "All impacted services fully recovered."})
    incident, _ = domain_adjustment({"topic": "service_status", "title": "Elevated errors affecting ChatGPT", "summary": "OpenAI is investigating an incident."})
    assert healthy < 0 and incident > 0


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


def test_discovery_uses_keyless_rss_provider_through_same_pipeline(tmp_path, monkeypatch):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database),
        "BRAVE_SEARCH_API_KEY": "",
        "DISCOVERY_PUBLIC_RSS_ENABLED": True,
        "DISCOVERY_MAX_QUERIES": 1,
        "DISCOVERY_MAX_RESULTS": 2,
        "SEARCH_PROFILES": [{"id": "warzone", "topic": "warzone", "queries": ["REV Warzone update"], "keywords": ["warzone", "update"], "active": True}],
        "TRACKED_ENTITIES": [],
    })
    assert discovery.discovery_enabled(config) is True
    assert discovery.discovery_provider(config)[1] == "google_news_rss"
    monkeypatch.setattr(discovery.GoogleNewsRssProvider, "search", lambda self, query, count, freshness, search_lang: [
        {"title": "REV Warzone update", "url": "https://www.callofduty.com/blog/rev", "snippet": "official", "published_at": None},
    ])
    monkeypatch.setattr(discovery, "fetch_page", lambda url: {"title": "REV Warzone update", "summary": "Weapon balance changed.", "content": "REV weapon balance changed in Warzone.", "digest": url})

    candidates = discovery.collect_discovery_candidates(conn, config)
    assert len(candidates) == 1
    assert candidates[0]["source_kind"] == "search"
    assert candidates[0]["metadata"]["verification"] == "single_source"


def test_public_rss_discovery_can_be_disabled():
    config = load_config({"BRAVE_SEARCH_API_KEY": "", "DISCOVERY_PUBLIC_RSS_ENABLED": False})
    assert discovery.discovery_enabled(config) is False
    assert discovery.discovery_provider(config)[1] == "disabled"


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


def test_topic_relationships_followed_story_and_people_are_scoped(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    person = upsert_person(conn, "icloud", "person-1", "Aiden's teacher", ["teacher@example.test"])
    assert set_person_importance(conn, person["id"], "important")["importance"] == "important"
    assert list_people(conn)[0]["importance"] == "important"
    candidate = {"topic": "apple", "source_id": "mail", "score": 70, "canonical_event_id": "canonical-1", "cluster_id": "cluster-1", "metadata": {"important_person": {"id": person["id"]}}}
    preferences = {"personal_priorities": {"iphone": 10}, "learned_topic_weights": {}, "learned_source_weights": {}, "followed_stories": {"canonical-1": datetime.now(timezone.utc).isoformat()}}
    apply_preference_adjustments(candidate, preferences)
    assert candidate["score"] == 100
    assert candidate["metadata"]["score_components"]["topic_relationship"] > 0
    assert candidate["metadata"]["score_components"]["followed_story"] == 18
    assert candidate["metadata"]["score_components"]["important_person"] == 12


def test_calendar_location_makes_weather_preparation_time_aware():
    candidate = {"topic": "weather", "score": 78, "priority": "normal", "metadata": {}}
    start = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)).isoformat()
    contexts = [{"kind": "calendar", "value": {"title": "school", "start": start, "location": "School"}}]
    prepared = prepare_event_candidate(candidate, contexts)
    assert prepared["metadata"]["score_components"]["context_preparation"] == 10


def test_game_event_countdowns_emit_once_per_window(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = upsert_game_event(conn, "Warzone", "Season update", (now + timedelta(hours=1)).isoformat(), "season")
    candidates = game_event_candidates(conn, now)
    assert candidates[0]["metadata"]["countdown_window"] == "1h"
    assert game_event_candidates(conn, now) == []
    assert event["game"] == "Warzone"
    expired = upsert_game_event(conn, "Warzone", "old event", (now - timedelta(hours=3)).isoformat(), "event")
    assert all(item["id"].split(":")[0] != expired["id"] for item in game_event_candidates(conn, now))


def test_purchase_watch_is_a_bounded_scoring_signal(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    purchase = upsert_purchase_record(conn, "mail:1", merchant="store", title="Phone", lifecycle="ordered")
    from pulse_app.storage import set_purchase_watch
    watched = set_purchase_watch(conn, purchase["id"], "high")
    assert watched["metadata"]["watch_priority"] == "high"
    candidate = {"topic": "purchase", "source_id": "mail", "score": 70, "metadata": {"lifecycle": {"purchase": {"id": purchase["id"], "watch_priority": "high"}}}}
    apply_preference_adjustments(candidate, {"personal_priorities": {}, "learned_topic_weights": {}, "learned_source_weights": {}, "followed_stories": {}})
    assert candidate["metadata"]["score_components"]["purchase_watch"] == 10


def test_history_retention_prunes_old_low_value_rows_but_keeps_important_events(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    old = "2020-01-01T00:00:00+00:00"
    base = {"source_id": "test", "source_kind": "rss", "topic": "watcher", "summary": "old", "body": "old", "url": "https://example.test", "relevant": True, "metadata": {}}
    upsert_event(conn, {**base, "id": "old-low", "title": "old low", "canonical_key": "old-low", "published_at": old, "discovered_at": old, "last_seen_at": old, "score": 40, "priority": "low"})
    upsert_event(conn, {**base, "id": "old-high", "topic": "apple", "title": "old high", "canonical_key": "old-high", "published_at": old, "discovered_at": old, "last_seen_at": old, "score": 95, "priority": "critical"})
    result = prune_history(conn, {"HISTORY_RETENTION_DAYS": 90, "DECISION_RETENTION_DAYS": 30}, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["events"] == 1
    assert get_event(conn, "old-low") is None
    assert get_event(conn, "old-high") is not None


def test_weather_alerts_cover_rain_storm_heat_cold_and_wind(tmp_path, monkeypatch):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database),
        "WEATHER_LABEL": "La Ceja, Antioquia",
        "WEATHER_LOOKAHEAD_HOURS": 24,
        "WEATHER_HOT_C": 28,
        "WEATHER_COLD_C": 12,
        "WEATHER_RAIN_PROBABILITY": 60,
        "WEATHER_RAIN_MM": 2,
        "WEATHER_WIND_KMH": 40,
    })
    forecast = {
        "hourly": {
            "time": ["2026-09-19T10:00", "2026-09-19T11:00", "2026-09-19T12:00"],
            "temperature_2m": [29, 13, 22],
            "apparent_temperature": [31, 10, 22],
            "precipitation_probability": [70, 95, 0],
            "precipitation": [3, 8, 0],
            "weather_code": [61, 95, 0],
            "wind_speed_10m": [20, 45, 50],
        }
    }
    monkeypatch.setattr(sources, "_request", lambda url, headers=None: json.dumps(forecast).encode())
    candidates = sources.weather_candidates(conn, config)
    categories = {item["metadata"]["weather_category"] for item in candidates}
    assert {"storm", "hot", "cold"}.issubset(categories)
    assert "rain" not in categories and "wind" not in categories
    storm = next(item for item in candidates if item["metadata"]["weather_category"] == "storm")
    assert "precipitation probability" in storm["summary"]
    assert storm["metadata"]["source_trust"] == "primary"


def test_strict_notification_lifecycle_keeps_history_without_repeat_push(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    config = load_config({
        "DATABASE_PATH": str(database),
        "TIMEZONE": "UTC",
        "NOTIFICATION_COOLDOWNS": {"warzone": 120},
        "NOTIFICATION_MAX_AGE": {"warzone": 1440},
    })
    init_db(database)
    conn = connect(database)
    event = {
        "id": "story-1", "source_id": "feed-a", "source_kind": "rss", "topic": "warzone",
        "title": "Feed A: Warzone REV recoil changes", "summary": "REV recoil increased in the balance patch.",
        "body": "matched", "url": "https://a.test/story", "canonical_key": "feed-a:1",
        "published_at": "2026-09-20T10:00:00+00:00", "score": 92, "priority": "critical", "relevant": True,
        "metadata": {"source_label": "Feed A", "sources": [{"url": "https://a.test/story", "title": "story"}]},
    }
    stored, created = upsert_event(conn, event)
    assert created and pending_events(conn)
    mark_notified(conn, stored["id"])
    duplicate = {**event, "id": "story-2", "source_id": "feed-b", "canonical_key": "feed-b:2", "title": "Warzone update: REV recoil changed", "summary": "The balance patch increased REV recoil."}
    stored, created = upsert_event(conn, duplicate)
    assert not created
    assert pending_events(conn) == []
    title, body = notification_copy(stored)
    assert "Feed A:" not in title
    assert "meaningful Warzone update" in body
    prefs = get_preferences(conn, config)
    assert should_notify(stored, prefs, config=config, conn=conn)[0] is False


def test_freshness_decay_lowers_old_but_not_stale_events(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    config = load_config({
        "DATABASE_PATH": str(database),
        "TIMEZONE": "UTC",
        "NOTIFICATION_MAX_AGE": {"warzone": 1440},
        "STRICT_MIN_THRESHOLDS": {"warzone": 84},
    })
    init_db(database)
    conn = connect(database)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    event, _ = upsert_event(conn, {
        "id": "freshness-1", "source_id": "official", "source_kind": "rss", "topic": "warzone",
        "title": "Warzone balance patch changes REV", "summary": "REV recoil changed in the patch.",
        "body": "patch", "url": "https://example.test/patch", "canonical_key": "freshness:1",
        "published_at": "2026-09-20T00:00:00+00:00", "score": 92, "priority": "high", "relevant": True,
        "metadata": {"source_trust": "primary"},
    })
    prefs = get_preferences(conn, config)
    allowed, reason, trace = evaluate_notification(event, prefs, now, config, conn)
    assert not allowed
    assert "freshness decay" in reason
    assert trace["effective_score"] < trace["score"]
    assert trace["freshness"]["state"] == "fresh"


def test_canonical_model_merges_observations_and_persists_suppressed_trace(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    base = {
        "id": "canonical-1", "source_id": "official", "source_kind": "rss", "topic": "apple",
        "title": "Apple security update affects iPhone", "summary": "A security update is available for iPhone users.",
        "body": "security", "url": "https://apple.com/news", "canonical_key": "official:1", "score": 92,
        "priority": "critical", "relevant": True, "metadata": {"source_trust": "primary", "sources": [{"url": "https://apple.com/news", "title": "official", "trust": "primary"}]},
    }
    first, created = upsert_event(conn, base)
    second, duplicate = upsert_event(conn, {**base, "id": "secondary-1", "source_id": "secondary", "canonical_key": "secondary:1", "url": "https://reuters.com/news", "title": "iPhone security update from Apple", "metadata": {"source_trust": "reliable_secondary", "sources": [{"url": "https://reuters.com/news", "title": "secondary", "trust": "reliable_secondary"}]}})
    assert created and not duplicate
    assert first["canonical_event_id"] == second["canonical_event_id"]
    assert second["cluster_id"]
    assert conn.execute("select count(*) from story_clusters where id=?", (second["cluster_id"],)).fetchone()[0] == 1
    assert conn.execute("select count(*) from event_observations where canonical_event_id=?", (second["canonical_event_id"],)).fetchone()[0] == 2
    prefs = get_preferences(conn, load_config({"DATABASE_PATH": str(database)}))
    allowed, reason, trace = evaluate_notification(second, prefs, datetime.now(timezone.utc), load_config({"DATABASE_PATH": str(database)}), conn)
    record_notification_decision(conn, second, False, "stale event", trace)
    conn.commit()
    stored = get_event(conn, second["id"])
    assert stored["decision_trace"]["cluster_id"] == second["cluster_id"]
    assert stored["notification_reason"] == "stale event"
    assert list_notification_decisions(conn, near_only=True)


def test_legacy_schema_migration_backfills_model_without_requeueing(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_kind TEXT NOT NULL,
            topic TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL, canonical_key TEXT UNIQUE NOT NULL,
            published_at TEXT, discovered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            score INTEGER NOT NULL, priority TEXT NOT NULL, relevant INTEGER NOT NULL DEFAULT 0,
            suppress_notification INTEGER NOT NULL DEFAULT 0, notified_at TEXT, reminded_at TEXT,
            remind_at TEXT, clicked_at TEXT, created_from TEXT NOT NULL DEFAULT 'source',
            metadata TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL DEFAULT '',
            notified_hash TEXT NOT NULL DEFAULT '', notification_pending INTEGER NOT NULL DEFAULT 0,
            notification_count INTEGER NOT NULL DEFAULT 0, last_notification_at TEXT,
            notification_reason TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO events(id,source_id,source_kind,topic,title,summary,body,url,canonical_key,
            discovered_at,last_seen_at,score,priority,relevant,metadata,notification_pending)
        VALUES('legacy-1','feed','rss','apple','Apple security update','update','body',
            'https://apple.test/update','feed:1','2026-09-20T10:00:00+00:00',
            '2026-09-20T10:00:00+00:00',92,'high',1,'{"source_trust":"primary"}',1);
        CREATE TABLE event_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL DEFAULT '', cluster_id TEXT NOT NULL DEFAULT '',
            evaluated_at TEXT NOT NULL, allowed INTEGER NOT NULL, reason TEXT NOT NULL,
            score INTEGER NOT NULL, threshold INTEGER NOT NULL, tier TEXT NOT NULL,
            near_threshold INTEGER NOT NULL DEFAULT 0, trace TEXT NOT NULL DEFAULT '{}'
        );
    """)
    conn.commit()
    conn.close()

    init_db(database)
    conn = connect(database)
    migrated = get_event(conn, "legacy-1")
    assert migrated["canonical_event_id"]
    assert migrated["cluster_id"]
    assert migrated["decision_trace"] == {}
    assert migrated["notification_pending"] is False
    assert conn.execute("select count(*) from canonical_events").fetchone()[0] == 1
    assert conn.execute("select count(*) from event_observations").fetchone()[0] == 1
    assert conn.execute("select count(*) from event_developments").fetchone()[0] == 1
    decision_columns = {row[1] for row in conn.execute("pragma table_info(event_decisions)")}
    assert {"distance_from_threshold", "later_became_important", "later_development_notified"}.issubset(decision_columns)


def test_feedback_simulator_and_morning_catchup(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    client = make_client(tmp_path)
    login(client)
    simulation = client.post("/api/debug/simulate", json={"topic": "warzone", "title": "REV balance patch", "summary": "REV recoil changed."})
    assert simulation.status_code == 200
    assert simulation.json["push_sent"] is False
    assert "score_components" in simulation.json["trace"]
    conn = connect(database)
    event = {
        "id": "feedback-one", "source_id": "test", "source_kind": "rss", "topic": "warzone", "title": "REV patch",
        "summary": "A patch changed REV recoil.", "body": "test", "url": "https://example.test", "canonical_key": "feedback:1",
        "score": 90, "priority": "critical", "relevant": True, "metadata": {"entities": ["rev"], "entity_names": ["REV"]},
    }
    upsert_event(conn, event)
    conn.commit()
    for action in ("useful", "not_useful", "too_late"):
        assert client.post("/api/events/feedback-one/feedback", json={"action": action}).status_code == 200
    assert client.get("/api/learning").json["summary"]["useful"] == 1
    now = datetime.now(timezone.utc).replace(hour=7, minute=30, second=0, microsecond=0)
    for index in range(2):
        upsert_event(conn, {**event, "id": f"catchup-{index}", "canonical_key": f"catchup:{index}", "title": f"Important signal {index}", "topic": "apple", "score": 90, "priority": "critical"})
    digest, held = create_morning_catchup(conn, {**load_config({"DATABASE_PATH": str(database), "TIMEZONE": "UTC", "QUIET_END": "07:00"}), "MORNING_CATCHUP_WINDOW_MINUTES": 60}, now)
    assert digest and len(held) >= 2


def test_package_watcher_only_surfaces_meaningful_status_changes(tmp_path, monkeypatch):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    source = {"id": "package-test", "label": "my order", "package_id": "ABC123", "url": "https://carrier.test/ABC123", "trust": "primary"}
    html = b"<html><head><title>Package delayed</title><meta name='description' content='Delayed by weather'></head><body>Delayed by weather</body></html>"
    monkeypatch.setattr(sources, "_request", lambda url, headers=None: html)
    config = load_config({"DATABASE_PATH": str(database)})
    candidates = sources._package_candidates(conn, source)
    assert candidates[0]["metadata"]["package_status"] == "exception"
    assert candidates[0]["score"] >= 90


def test_domain_profiles_penalize_low_value_gaming_and_apple_noise():
    gaming = sources._source_item({"id": "gaming", "kind": "rss", "label": "COD", "topic": "warzone", "trust": "primary", "keywords": ["warzone"]}, {"title": "Warzone store bundle", "summary": "New skin bundle", "url": "https://example.test"})
    apple = sources._source_item({"id": "apple", "kind": "rss", "label": "Apple", "topic": "apple", "trust": "primary", "keywords": ["iphone"]}, {"title": "iPhone case accessory deal", "summary": "New case deal", "url": "https://example.test"})
    sources.annotate_candidate(gaming, [])
    sources.annotate_candidate(apple, [])
    assert gaming["metadata"]["score_components"]["domain"] < 0
    assert apple["metadata"]["score_components"]["domain"] < 0


def _decision_event(event_id: str, topic: str = "warzone", score: int = 92, metadata: dict | None = None) -> dict:
    return {
        "id": event_id, "source_id": event_id, "source_kind": "rss", "topic": topic,
        "title": f"{topic} meaningful update {event_id}", "summary": "A meaningful update changed the situation.",
        "body": "update", "url": f"https://example.test/{event_id}", "canonical_key": f"test:{event_id}",
        "score": score, "priority": "critical" if score >= 90 else "high", "relevant": True,
        "metadata": {"source_trust": "primary", **(metadata or {})},
    }


def test_false_negative_audit_records_distance_and_later_outcome(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    event, _ = upsert_event(conn, _decision_event("audit-1", score=83))
    config = load_config({"DATABASE_PATH": str(database), "NOTIFICATION_COOLDOWNS": {"warzone": 0}, "ROLLING_NOTIFICATION_LIMITS": {}})
    prefs = get_preferences(conn, config)
    allowed, reason, trace = evaluate_notification(event, prefs, datetime.now(timezone.utc), config, conn)
    assert not allowed
    record_notification_decision(conn, event, allowed, reason, trace)
    conn.commit()
    mark_notified(conn, event["id"])
    conn.commit()
    reviewed = list_notification_decisions(conn, near_only=True, suppressed_only=True)
    assert reviewed[0]["distance_from_threshold"] <= 0
    assert reviewed[0]["later_became_important"] is True
    assert reviewed[0]["trace"]["freshness"]


def test_rolling_ceiling_and_safety_bypass_are_in_shared_decision_trace(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database),
        "TIMEZONE": "UTC", "QUIET_START": "00:00", "QUIET_END": "00:00",
        "NOTIFICATION_COOLDOWNS": {"warzone": 0, "earthquake": 0},
        "ROLLING_NOTIFICATION_LIMITS": {"global": {"count": 1, "minutes": 60}, "topic": {}, "tier": {}},
    })
    sent, _ = upsert_event(conn, _decision_event("ceiling-sent"))
    mark_notified(conn, sent["id"])
    candidate, _ = upsert_event(conn, _decision_event("ceiling-next", score=94))
    prefs = get_preferences(conn, config)
    allowed, reason, trace = evaluate_notification(candidate, prefs, datetime.now(timezone.utc), config, conn)
    assert not allowed and reason == "rolling notification ceiling"
    assert trace["rolling_frequency"]["active"]
    safety, _ = upsert_event(conn, _decision_event("ceiling-safety", "earthquake", 99, {"safety_critical": True}))
    allowed, _, safety_trace = evaluate_notification(safety, prefs, datetime.now(timezone.utc), config, conn)
    assert allowed
    assert safety_trace["rolling_frequency"]["bypassed_for_safety"] is True


def test_quiet_hours_cooling_and_trend_escalation_are_explainable(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    conn = connect(database)
    config = load_config({
        "DATABASE_PATH": str(database), "TIMEZONE": "UTC", "QUIET_START": "00:00", "QUIET_END": "23:59",
        "NOTIFICATION_COOLDOWNS": {"warzone": 0, "apple": 0}, "ROLLING_NOTIFICATION_LIMITS": {},
    })
    prefs = get_preferences(conn, config)
    quiet, _ = upsert_event(conn, _decision_event("quiet-1", score=88))
    allowed, reason, trace = evaluate_notification(quiet, prefs, datetime(2026, 9, 20, 12, tzinfo=timezone.utc), config, conn)
    assert not allowed and reason == "quiet hours" and trace["quiet_hours"]["affected"]
    cooling_titles = ("Warzone alpha outage", "Ranked cobalt disruption", "Ricochet delta incident")
    for index, title in enumerate(cooling_titles):
        cooling_event = _decision_event(f"cool-{index}", score=95)
        cooling_event["title"] = title
        cooling_event["summary"] = f"A separate event {index} requires attention."
        sent, _ = upsert_event(conn, cooling_event)
        mark_notified(conn, sent["id"])
    cooled, _ = upsert_event(conn, _decision_event("cooled", score=86))
    config["QUIET_START"], config["QUIET_END"] = "23:00", "23:01"
    prefs = get_preferences(conn, config)
    allowed, reason, cooling_trace = evaluate_notification(cooled, prefs, datetime(2026, 9, 20, 12, tzinfo=timezone.utc), config, conn)
    assert not allowed and reason == "temporary topic cooling"
    assert cooling_trace["topic_cooling"]["threshold_bonus"] > 0
    override, _ = upsert_event(conn, _decision_event("cool-development", score=96, metadata={"development_meaningful": True}))
    allowed, _, override_trace = evaluate_notification(override, prefs, datetime(2026, 9, 20, 12, tzinfo=timezone.utc), config, conn)
    assert allowed and override_trace["topic_cooling"]["overridden_for_development"]
    first, _ = upsert_event(conn, _decision_event("trend-1", "apple", 80, {"sources": [{"url": "https://one.test", "title": "one"}]}))
    second, _ = upsert_event(conn, _decision_event("trend-2", "apple", 80, {"sources": [{"url": "https://two.test", "title": "two"}]}))
    trend_config = {**config, "ROLLING_NOTIFICATION_LIMITS": {}, "QUIET_START": "23:00", "QUIET_END": "23:01"}
    _, _, trend_trace = evaluate_notification(second, get_preferences(conn, trend_config), datetime.now(timezone.utc), trend_config, conn)
    assert trend_trace["trend"]["credible_sources"] >= 2
    assert trend_trace["trend"]["bonus"] > 0
