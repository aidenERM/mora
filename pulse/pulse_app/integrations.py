from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from .rules import annotate_candidate, apply_preference_adjustments, score_item
from .storage import (
    connect,
    get_preferences,
    load_credential,
    get_source_state,
    record_event_action,
    save_credential,
    save_integration,
    save_source_state,
    set_context_signal,
    upsert_package,
    upsert_purchase,
    upsert_event,
)

LOGGER = logging.getLogger("pulse.integrations")

PROVIDERS = {
    "apple": "Apple companion",
    "google": "Google account",
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "discord": "Discord",
    "aws-bedrock": "AWS Bedrock AI",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _safe_json(value, fallback=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback if fallback is not None else {}


def provider_status(conn, config: dict) -> list[dict]:
    rows = {row["provider"]: dict(row) for row in conn.execute("SELECT * FROM integrations ORDER BY provider").fetchall()}
    result = []
    for provider, label in PROVIDERS.items():
        item = rows.get(provider, {
            "provider": provider, "label": label, "enabled": 0, "connection_state": "not_configured",
            "authorization_state": "not_configured", "last_success_at": None, "last_attempted_at": None,
            "last_error": "", "metadata": "{}",
        })
        if provider in {"gmail", "calendar"} and provider not in rows and "google" in rows:
            item = {**rows["google"], "provider": provider, "label": label}
        item["enabled"] = bool(item.get("enabled"))
        item["metadata"] = _safe_json(item.get("metadata"), {})
        credential_provider = "google" if provider in {"gmail", "calendar"} else provider
        item["has_credentials"] = bool(conn.execute("SELECT 1 FROM integration_credentials WHERE provider=?", (credential_provider,)).fetchone())
        if provider in {"gmail", "calendar"}:
            item["configured"] = bool(config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"))
        elif provider == "discord":
            item["configured"] = bool(config.get("DISCORD_CLIENT_ID") and config.get("DISCORD_CLIENT_SECRET"))
        elif provider == "aws-bedrock":
            item["configured"] = bool(config.get("BEDROCK_MODEL_ID"))
        else:
            item["configured"] = True
        result.append(item)
    return result


def integration_health(conn, provider: str, label: str | None = None) -> dict:
    save_integration(conn, provider, label or PROVIDERS.get(provider, provider))
    return next(item for item in provider_status(conn, {}) if item["provider"] == provider)


def _metadata(conn, provider: str) -> dict:
    row = conn.execute("SELECT metadata FROM integrations WHERE provider=?", (provider,)).fetchone()
    return _safe_json(row[0] if row else "{}", {})


def start_attempt(conn, provider: str) -> None:
    save_integration(conn, provider, PROVIDERS.get(provider, provider), last_attempted_at=_iso(), connection_state="syncing")


def mark_success(conn, provider: str, metadata: dict | None = None) -> None:
    save_integration(conn, provider, PROVIDERS.get(provider, provider), enabled=True, connection_state="connected", authorization_state="authorized", last_success_at=_iso(), last_attempted_at=_iso(), last_error="", metadata=metadata or _metadata(conn, provider))


def mark_failure(conn, provider: str, error: str) -> None:
    save_integration(conn, provider, PROVIDERS.get(provider, provider), last_attempted_at=_iso(), connection_state="error", last_error=str(error)[:500])


def disconnect(conn, provider: str) -> None:
    conn.execute("DELETE FROM integration_credentials WHERE provider=?", (provider,))
    save_integration(conn, provider, PROVIDERS.get(provider, provider), enabled=False, connection_state="disconnected", authorization_state="revoked", last_error="")


def create_oauth_state(conn, provider: str, metadata: dict | None = None) -> str:
    state = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM oauth_states WHERE expires_at<?", (_iso(),))
    conn.execute("INSERT INTO oauth_states(state,provider,metadata,expires_at) VALUES(?,?,?,?)", (state, provider, json.dumps(metadata or {}, separators=(",", ":")), _iso(_now() + timedelta(minutes=10))))
    conn.commit()
    return state


def consume_oauth_state(conn, provider: str, state: str) -> dict | None:
    row = conn.execute("SELECT * FROM oauth_states WHERE state=? AND provider=? AND expires_at>=?", (state, provider, _iso())).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    conn.commit()
    return _safe_json(row["metadata"], {})


def google_authorization_url(config: dict, state: str) -> str:
    params = {
        "client_id": config["GOOGLE_CLIENT_ID"],
        "redirect_uri": config["GOOGLE_REDIRECT_URI"],
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def discord_authorization_url(config: dict, state: str) -> str:
    params = {
        "client_id": config["DISCORD_CLIENT_ID"], "redirect_uri": config["DISCORD_REDIRECT_URI"],
        "response_type": "code", "scope": "identify guilds", "state": state,
    }
    return "https://discord.com/oauth2/authorize?" + urlencode(params)


def exchange_oauth_code(conn, config: dict, provider: str, code: str, state: str) -> dict:
    metadata = consume_oauth_state(conn, provider, state)
    if metadata is None:
        raise ValueError("invalid or expired OAuth state")
    if provider == "google":
        payload = {"code": code, "client_id": config["GOOGLE_CLIENT_ID"], "client_secret": config["GOOGLE_CLIENT_SECRET"], "redirect_uri": config["GOOGLE_REDIRECT_URI"], "grant_type": "authorization_code"}
        response = requests.post("https://oauth2.googleapis.com/token", data=payload, timeout=15)
    elif provider == "discord":
        payload = {"code": code, "client_id": config["DISCORD_CLIENT_ID"], "client_secret": config["DISCORD_CLIENT_SECRET"], "redirect_uri": config["DISCORD_REDIRECT_URI"], "grant_type": "authorization_code"}
        response = requests.post("https://discord.com/api/oauth2/token", data=payload, timeout=15)
    else:
        raise ValueError("unsupported OAuth provider")
    response.raise_for_status()
    token = response.json()
    if not token.get("access_token"):
        raise ValueError("OAuth provider returned no access token")
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    save_credential(conn, provider, token)
    mark_success(conn, provider, {"scope": token.get("scope", ""), "token_type": token.get("token_type", "Bearer")})
    return {"provider": provider, "authorized": True}


def _google_token(conn, config: dict) -> str:
    token = load_credential(conn, "google") or {}
    if token.get("access_token") and int(token.get("expires_at", 0)) > int(time.time()) + 60:
        return token["access_token"]
    if not token.get("refresh_token"):
        raise RuntimeError("Google authorization is required")
    response = requests.post("https://oauth2.googleapis.com/token", data={"client_id": config["GOOGLE_CLIENT_ID"], "client_secret": config["GOOGLE_CLIENT_SECRET"], "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}, timeout=15)
    response.raise_for_status()
    refreshed = response.json()
    token.update(refreshed)
    token["expires_at"] = int(time.time()) + int(refreshed.get("expires_in", 3600))
    save_credential(conn, "google", token)
    return token["access_token"]


def _google_get(token: str, url: str, params: dict | None = None) -> dict:
    response = requests.get(url, headers={"Authorization": "Bearer " + token}, params=params or {}, timeout=15)
    response.raise_for_status()
    return response.json()


def classify_gmail_message(message: dict) -> dict:
    headers = {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in message.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "").strip()
    sender = headers.get("from", "").strip()
    snippet = str(message.get("snippet", "")).strip()
    text = (subject + " " + sender + " " + snippet).casefold()
    if any(term in text for term in ("security alert", "new sign-in", "verification code", "suspicious activity")):
        topic, keywords, score = "security", ["security", "alert"], 90
    elif any(term in text for term in ("delivered", "out for delivery", "tracking", "shipment", "package", "customs", "delivery exception")):
        topic, keywords, score = "package", ["delivery", "tracking", "package"], 82
    elif any(term in text for term in ("refund", "refunded", "return approved", "cancelled", "canceled")):
        topic, keywords, score = "purchase", ["refund", "return", "cancelled"], 86
    elif any(term in text for term in ("receipt", "order confirmation", "payment", "renewal", "subscription")):
        topic, keywords, score = "purchase", ["order", "payment", "receipt"], 76
    elif any(term in text for term in ("flight", "reservation", "hotel", "boarding", "travel")):
        topic, keywords, score = "travel", ["flight", "reservation", "travel"], 82
    elif any(term in text for term in ("school", "class", "assignment", "teacher", "universidad")):
        topic, keywords, score = "school", ["school", "class", "assignment"], 82
    else:
        return {"relevant": False, "subject": subject, "sender": sender, "snippet": snippet}
    tracking = ""
    match = __import__("re").search(r"\b(?:1Z\w{16}|\d{12,22}|[A-Z]{2}\d{9}[A-Z]{2})\b", subject + " " + snippet)
    if match:
        tracking = match.group(0)
    return {"relevant": True, "topic": topic, "keywords": keywords, "score": score, "subject": subject, "sender": sender, "snippet": snippet, "tracking_number": tracking, "message_id": message.get("id", "")}


def _gmail_event(item: dict, config: dict, preferences: dict) -> dict:
    message_id = item["message_id"]
    title = item["subject"] or "Important Gmail message"
    summary = item["snippet"] or "Important message detected in Gmail."
    score, relevant, reason = score_item(item["topic"], title, summary, item["keywords"], always_relevant=True, boost=max(0, item["score"] - 82))
    event = {
        "id": "gmail-" + hashlib.sha256(message_id.encode()).hexdigest()[:24], "source_id": "gmail", "source_kind": "gmail", "topic": item["topic"],
        "title": title, "summary": summary, "body": f"{reason}; sender: {item['sender']}", "url": "https://mail.google.com/mail/u/0/#all/" + message_id,
        "canonical_key": "gmail:" + message_id, "published_at": None, "score": max(score, item["score"]), "priority": "critical" if score >= 90 else "high", "relevant": relevant,
        "metadata": {"source_trust": "primary", "gmail_message_id": message_id, "sender": item["sender"], "tracking_number": item.get("tracking_number", ""), "sources": [{"url": "https://mail.google.com", "title": "Gmail", "trust": "primary"}]},
    }
    annotate_candidate(event, config.get("TRACKED_ENTITIES", []))
    apply_preference_adjustments(event, preferences)
    return event


def persist_gmail_lifecycle(conn, item: dict) -> None:
    if item.get("topic") == "package" and item.get("tracking_number"):
        text = (item.get("subject", "") + " " + item.get("snippet", "")).casefold()
        if "delivered" in text:
            status = "delivered"
        elif "out for delivery" in text:
            status = "out_for_delivery"
        elif "exception" in text or "customs" in text:
            status = "exception"
        else:
            status = "in_transit"
        upsert_package(conn, item["tracking_number"], item.get("sender", ""), status=status, source="gmail")
    if item.get("topic") == "purchase":
        text = (item.get("subject", "") + " " + item.get("snippet", "")).casefold()
        lifecycle = "refunded" if "refund" in text or "return approved" in text else "cancelled" if "cancel" in text else "ordered"
        upsert_purchase(conn, "gmail:" + item.get("message_id", ""), merchant=item.get("sender", ""), title=item.get("subject", ""), lifecycle=lifecycle, metadata={"source": "gmail"})


def sync_google(conn, config: dict) -> dict:
    token = _google_token(conn, config)
    start_attempt(conn, "google")
    preferences = get_preferences(conn, config)
    created = 0
    gmail = _google_get(token, "https://gmail.googleapis.com/gmail/v1/users/me/messages", {"maxResults": 25, "q": "newer_than:7d"})
    for item in gmail.get("messages", []):
        message = _google_get(token, f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{item['id']}", {"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]})
        classified = classify_gmail_message(message)
        if not classified.get("relevant"):
            continue
        persist_gmail_lifecycle(conn, classified)
        event = _gmail_event(classified, config, preferences)
        stored, was_created = upsert_event(conn, event)
        created += int(was_created)
    calendar = _google_get(token, "https://www.googleapis.com/calendar/v3/calendars/primary/events", {"maxResults": 20, "singleEvents": "true", "orderBy": "startTime", "timeMin": _iso()})
    for item in calendar.get("items", []):
        start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
        if not start:
            continue
        expires = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date") or start
        set_context_signal(conn, "calendar", {"event_id": item.get("id", ""), "title": item.get("summary", ""), "location": item.get("location", ""), "start": start, "end": expires}, "google-calendar", 0.98, expires)
    mark_success(conn, "google", {"gmail_messages": len(gmail.get("messages", [])), "calendar_events": len(calendar.get("items", []))})
    conn.commit()
    return {"created": created, "gmail_messages": len(gmail.get("messages", [])), "calendar_events": len(calendar.get("items", []))}


def sync_discord(conn, config: dict) -> dict:
    row = conn.execute("SELECT ciphertext FROM integration_credentials WHERE provider='discord'").fetchone()
    if not row:
        raise RuntimeError("Discord authorization is required")
    token = load_credential(conn, "discord") or {}
    start_attempt(conn, "discord")
    response = requests.get("https://discord.com/api/users/@me/guilds", headers={"Authorization": "Bearer " + token.get("access_token", "")}, timeout=15)
    response.raise_for_status()
    guilds = response.json()
    mark_success(conn, "discord", {"guilds": [{"id": item.get("id"), "name": item.get("name")} for item in guilds[:50]]})
    conn.commit()
    return {"guilds": len(guilds)}


def sync_provider(conn, config: dict, provider: str) -> dict:
    if provider in {"google", "gmail", "calendar"}:
        return sync_google(conn, config)
    if provider == "discord":
        return sync_discord(conn, config)
    if provider == "apple":
        return {"context": len(active_context(conn))}
    if provider == "aws-bedrock":
        return {"configured": bool(config.get("BEDROCK_MODEL_ID"))}
    raise ValueError("unknown integration")


def active_context(conn) -> list[dict]:
    now = _iso()
    conn.execute("DELETE FROM context_signals WHERE expires_at IS NOT NULL AND expires_at<?", (now,))
    rows = conn.execute("SELECT * FROM context_signals WHERE expires_at IS NULL OR expires_at>=? ORDER BY observed_at DESC", (now,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["value"] = _safe_json(item.pop("value_json"), {})
        item["active"] = True
        result.append(item)
    return result
