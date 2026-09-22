from __future__ import annotations

import hashlib
import imaplib
import json
import logging
import email
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from xml.etree import ElementTree
from urllib.parse import urlencode, urljoin

import requests

from .rules import annotate_candidate, apply_preference_adjustments, priority_for, score_item
from .actions import extract_plan_candidate, save_extracted_plan
from .bedrock import validate_config
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
    upsert_package_record,
    upsert_purchase,
    upsert_purchase_record,
    upsert_person,
    important_person_for,
    upsert_event,
)

LOGGER = logging.getLogger("pulse.integrations")

PROVIDERS = {
    "apple": "Apple companion",
    "apple-calendar": "Apple Calendar",
    "apple-contacts": "Apple Contacts",
    "apple-mail": "Apple Mail",
    "apple-music": "Apple Music",
    "google": "Google account",
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "discord": "Discord",
    "aws-bedrock": "AWS Bedrock AI",
}
GOOGLE_SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/contacts.readonly"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _safe_json(value, fallback=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback if fallback is not None else {}


CONTEXT_MODES = {"home", "school", "outside", "travel", "unknown"}
CONTEXT_STATES = {"awake", "sleep", "focus", "unknown"}
CONTEXT_KINDS = {"mode", "physical_context", "state", "calendar", "reminders", "health", "home", "music", "contacts", "weather", "location", "battery", "charging", "network", "sleep", "focus", "shortcut"}
CONTEXT_SOURCE_PRIORITY = {"apple-companion": 5, "trusted-inference": 4, "shortcut": 3, "browser-location": 2.5, "google-calendar": 2, "manual": 1}


def normalize_companion_payload(body: dict) -> dict:
    body = body if isinstance(body, dict) else {}
    requested_mode = str(body.get("physical_context", body.get("mode", "unknown"))).strip().casefold()
    state = str(body.get("state", "awake")).strip().casefold()
    if requested_mode == "sleep":
        requested_mode, state = "unknown", "sleep"
    if requested_mode not in CONTEXT_MODES:
        raise ValueError("invalid context mode")
    if state not in CONTEXT_STATES:
        raise ValueError("invalid context state")
    try:
        confidence = max(0.0, min(1.0, float(body.get("confidence", 0.5))))
    except (TypeError, ValueError):
        raise ValueError("invalid confidence")
    expires_at = body.get("expires_at")
    if expires_at:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        expires_at = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    signals = {}
    raw_signals = body.get("signals") if isinstance(body.get("signals"), dict) else {}
    for kind, value in raw_signals.items():
        if kind in CONTEXT_KINDS and isinstance(value, dict):
            # Only derived, bounded context crosses the companion boundary.
            normalized = {str(key)[:40]: str(item)[:160] for key, item in value.items() if key and item is not None}
            if kind == "location":
                try:
                    normalized = normalize_location_payload(normalized)["value"]
                except ValueError:
                    # A Shortcut can accidentally send a street address in
                    # the latitude/longitude fields. Do not treat that as a
                    # fresh location signal.
                    continue
            signals[kind] = normalized
    focus_signal = signals.get("focus", {})
    sleep_signal = signals.get("sleep", {})
    if str(sleep_signal.get("active", sleep_signal.get("enabled", ""))).casefold() in {"1", "true", "yes", "on"}:
        state = "sleep"
    elif str(focus_signal.get("active", focus_signal.get("enabled", ""))).casefold() in {"1", "true", "yes", "on"}:
        state = "focus"
    if not expires_at and (requested_mode != "unknown" or "location" in signals):
        expires_at = _iso(_now() + timedelta(hours=2))
    permissions = body.get("permissions") if isinstance(body.get("permissions"), dict) else {}
    permissions = {str(key)[:40]: str(value)[:40] for key, value in permissions.items() if key and value is not None}
    return {"mode": requested_mode, "physical_context": requested_mode, "state": state, "confidence": confidence, "expires_at": expires_at, "signals": signals, "permissions": permissions}


def normalize_location_payload(body: dict, max_age_minutes: int = 180) -> dict:
    body = body if isinstance(body, dict) else {}
    try:
        latitude = float(body.get("latitude"))
        longitude = float(body.get("longitude"))
        accuracy = max(0.0, min(10000.0, float(body.get("accuracy", 0))))
    except (TypeError, ValueError):
        raise ValueError("invalid location")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid location")
    observed_at = _now()
    expires_at = observed_at + timedelta(minutes=max(15, min(1440, int(max_age_minutes))))
    return {
        "value": {"latitude": round(latitude, 3), "longitude": round(longitude, 3), "accuracy_m": round(accuracy, 1), "coarse": True},
        "confidence": max(0.35, min(0.95, 1.0 - min(accuracy, 500.0) / 700.0)),
        "observed_at": _iso(observed_at),
        "expires_at": _iso(expires_at),
    }


def normalize_icloud_calendar_event(event: dict) -> dict:
    event = event if isinstance(event, dict) else {}
    title = str(event.get("title") or event.get("summary") or "Calendar event").strip()[:240]
    start = str(event.get("start") or "").strip()
    end = str(event.get("end") or start).strip()
    location = str(event.get("location") or "").strip()[:240]
    uid = str(event.get("uid") or "").strip()[:180]
    return {"event_id": uid, "title": title, "start": start, "end": end, "location": location, "calendar": str(event.get("calendar") or "")[:120], "status": str(event.get("status") or "confirmed").casefold()}


def normalize_icloud_contact(contact: dict) -> dict:
    contact = contact if isinstance(contact, dict) else {}
    name = str(contact.get("name") or contact.get("fn") or "").strip()[:160]
    emails = [str(value).strip()[:160] for value in contact.get("emails", []) if str(value).strip()][:5]
    phones = [str(value).strip()[:60] for value in contact.get("phones", []) if str(value).strip()][:5]
    return {"id": str(contact.get("id") or contact.get("uid") or "")[:180], "name": name, "emails": emails, "phones": phones, "importance": str(contact.get("importance") or "normal") if contact.get("importance") in {"important", "normal", "ignore"} else "normal"}


def classify_apple_mail_message(message: dict) -> dict:
    return classify_gmail_message(message)


def prepare_event_candidate(candidate: dict, contexts: list[dict] | None = None) -> dict:
    """Apply conservative context elevation to an already-normalized event."""
    contexts = contexts or []
    mode = next((item.get("value", {}).get("mode") for item in contexts if item.get("kind") == "physical_context"), None)
    mode = mode or next((item.get("value", {}).get("mode") for item in contexts if item.get("kind") == "mode"), None)
    context_kinds = {item.get("kind") for item in contexts}
    boost = 0
    reason = ""
    topic = candidate.get("topic")
    upcoming_calendar = [item for item in contexts if item.get("kind") == "calendar"]
    calendar_soon = False
    calendar_has_location = False
    now = _now()
    for item in upcoming_calendar:
        value = item.get("value") or {}
        try:
            start = datetime.fromisoformat(str(value.get("start", "")).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            hours = (start.astimezone(timezone.utc) - now).total_seconds() / 3600
            if 0 <= hours <= 18:
                calendar_soon = True
                calendar_has_location = calendar_has_location or bool(value.get("location"))
        except (TypeError, ValueError):
            continue
    if topic == "weather" and calendar_soon and calendar_has_location:
        boost, reason = 10, "weather may affect an upcoming plan"
    elif topic == "weather" and mode in {"outside", "travel"}:
        boost, reason = 8, "weather affects current plans"
    elif topic == "travel" and mode == "travel":
        boost, reason = 10, "travel context"
    elif topic in {"package", "purchase"} and "calendar" in context_kinds:
        boost, reason = 5, "calendar-aware preparation"
    elif topic == "package" and mode in {"outside", "travel"}:
        boost, reason = 5, "delivery while away"
    elif topic == "weather" and "location" in context_kinds:
        boost, reason = 5, "recent location context"
    elif topic in {"security", "school"} and mode == "school":
        boost, reason = 5, "school context"
    if (candidate.get("metadata") or {}).get("status_change"):
        candidate.setdefault("metadata", {})["time_sensitive"] = True
    if boost:
        candidate["score"] = min(100, int(candidate.get("score", 0)) + boost)
        candidate.setdefault("metadata", {}).setdefault("score_components", {})["context_preparation"] = boost
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; " + reason
        candidate["priority"] = priority_for(candidate["score"])
    return candidate


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
        credential_provider = "google" if provider in {"gmail", "calendar"} else "icloud" if provider in {"apple-calendar", "apple-contacts", "apple-mail"} else provider
        item["has_credentials"] = bool(conn.execute("SELECT 1 FROM integration_credentials WHERE provider=?", (credential_provider,)).fetchone())
        if provider in {"gmail", "calendar"}:
            item["configured"] = bool(config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"))
        elif provider == "discord":
            item["configured"] = bool(config.get("DISCORD_CLIENT_ID") and config.get("DISCORD_CLIENT_SECRET"))
        elif provider in {"apple-calendar", "apple-contacts", "apple-mail"}:
            item["configured"] = bool(item["has_credentials"] or (config.get("ICLOUD_APPLE_ID") and config.get("ICLOUD_APP_PASSWORD")))
        elif provider == "apple-music":
            item["configured"] = False
        elif provider == "aws-bedrock":
            bedrock = validate_config(config)
            item["configured"] = bool(bedrock["valid"])
            item["metadata"].update({"region": bedrock["region"], "model": bedrock["model"], "credentials_present": bedrock["credentials_present"], "configuration_errors": bedrock["errors"]})
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
        "scope": GOOGLE_SCOPES,
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


def create_google_calendar_event(conn, config: dict, plan: dict) -> dict:
    token = _google_token(conn, config)
    action = plan.get("action_payload") or {}
    payload = {
        "summary": str(action.get("summary") or plan.get("title") or "Pulse plan")[:240],
        "description": str(action.get("description") or plan.get("summary") or "")[:2000],
        "location": str(action.get("location") or plan.get("location") or "")[:240],
        "start": {"dateTime": plan["start_at"], "timeZone": config.get("TIMEZONE", "America/Bogota")},
        "end": {"dateTime": plan["end_at"], "timeZone": config.get("TIMEZONE", "America/Bogota")},
        "visibility": "private",
    }
    response = requests.post("https://www.googleapis.com/calendar/v3/calendars/primary/events", headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, json=payload, timeout=15)
    response.raise_for_status()
    value = response.json()
    return {"provider": "google-calendar", "event_id": value.get("id", ""), "html_link": value.get("htmlLink", "")}


def _icloud_credentials(conn, config: dict) -> dict:
    stored = load_credential(conn, "icloud") or {}
    apple_id = stored.get("apple_id") or config.get("ICLOUD_APPLE_ID", "")
    app_password = stored.get("app_password") or config.get("ICLOUD_APP_PASSWORD", "")
    if not apple_id or not app_password:
        raise RuntimeError("Apple iCloud setup is required")
    return {"apple_id": apple_id, "app_password": app_password}


def _icloud_request(method: str, url: str, credentials: dict, body: str, headers: dict | None = None):
    response = requests.request(method, url, data=body.encode("utf-8"), headers=headers or {}, auth=(credentials["apple_id"], credentials["app_password"]), timeout=20)
    response.raise_for_status()
    return response


def _hrefs(response_text: str) -> list[str]:
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError:
        return []
    return [node.text.strip() for node in root.iter() if node.tag.endswith("href") and node.text and node.text.strip()]


def _property_href(response_text: str, property_name: str) -> str:
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError:
        return ""
    for node in root.iter():
        if not node.tag.endswith(property_name):
            continue
        for child in node.iter():
            if child.tag.endswith("href") and child.text and child.text.strip():
                return child.text.strip()
    return ""


def _calendar_data(response_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError:
        return []
    results = []
    for node in root.iter():
        if not node.tag.endswith("calendar-data") or not node.text:
            continue
        fields = {}
        current = None
        for raw in node.text.replace("\r\n ", "").replace("\r\n\t", "").splitlines():
            if raw.startswith("BEGIN:VEVENT"):
                fields = {}
                current = fields
                continue
            if raw.startswith("END:VEVENT"):
                if current is not None:
                    results.append(current)
                current = None
                continue
            if current is None or ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            key = key.split(";", 1)[0].upper()
            if key in {"UID", "SUMMARY", "DTSTART", "DTEND", "LOCATION", "STATUS"}:
                current[key] = value.strip()
    return results


def _contact_data(response_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError:
        return []
    contacts = []
    for node in root.iter():
        if not node.tag.endswith("address-data") or not node.text:
            continue
        fields = {"emails": [], "phones": []}
        for raw in node.text.replace("\r\n ", "").replace("\r\n\t", "").splitlines():
            if ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            key = key.split(";", 1)[0].upper()
            if key == "FN":
                fields["fn"] = value.strip()
            elif key == "UID":
                fields["uid"] = value.strip()
            elif key == "EMAIL":
                fields["emails"].append(value.strip())
            elif key == "TEL":
                fields["phones"].append(value.strip())
        contacts.append(normalize_icloud_contact(fields))
    return contacts


def _sync_icloud_calendar(conn, config: dict, credentials: dict) -> dict:
    propfind = """<?xml version="1.0"?><propfind xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><prop><current-user-principal/><c:calendar-home-set/></prop></propfind>"""
    root_response = _icloud_request("PROPFIND", config["ICLOUD_CALDAV_URL"], credentials, propfind, {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
    hrefs = _hrefs(root_response.text)
    principal = _property_href(root_response.text, "current-user-principal") or next((href for href in hrefs if "principal" in href), "")
    home = _property_href(root_response.text, "calendar-home-set")
    if principal and not home:
        if principal.startswith("/"):
            principal = urljoin(config["ICLOUD_CALDAV_URL"].rstrip("/") + "/", principal)
        principal_response = _icloud_request("PROPFIND", principal, credentials, propfind, {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
        home = _property_href(principal_response.text, "calendar-home-set")
    if not home:
        raise RuntimeError("Apple Calendar home was not discovered")
    if home.startswith("/"):
        home = urljoin(config["ICLOUD_CALDAV_URL"].rstrip("/") + "/", home)
    collection_response = _icloud_request("PROPFIND", home, credentials, "<propfind xmlns=\"DAV:\"><prop><displayname/><resourcetype/></prop></propfind>", {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    calendars = [href for href in _hrefs(collection_response.text) if href.rstrip("/") != home.rstrip("/")]
    now = _now()
    future = now + timedelta(days=14)
    report = f"""<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{now.strftime('%Y%m%dT%H%M%SZ')}" end="{future.strftime('%Y%m%dT%H%M%SZ')}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"""
    event_count = 0
    for calendar_url in calendars[:30]:
        if calendar_url.startswith("/"):
            calendar_url = urljoin(config["ICLOUD_CALDAV_URL"].rstrip("/") + "/", calendar_url)
        try:
            response = _icloud_request("REPORT", calendar_url, credentials, report, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        except requests.RequestException:
            continue
        for raw in _calendar_data(response.text):
            normalized = normalize_icloud_calendar_event({"uid": raw.get("UID"), "title": raw.get("SUMMARY"), "start": raw.get("DTSTART"), "end": raw.get("DTEND"), "location": raw.get("LOCATION"), "status": raw.get("STATUS")})
            if not normalized["start"] or normalized["status"] == "cancelled":
                continue
            set_context_signal(conn, "calendar", normalized, "apple-calendar", 0.98, normalized["end"] or normalized["start"])
            event_count += 1
    mark_success(conn, "apple-calendar", {"calendars": len(calendars), "events": event_count})
    return {"calendars": len(calendars), "events": event_count}


def _sync_icloud_contacts(conn, config: dict, credentials: dict) -> dict:
    response = _icloud_request("PROPFIND", config["ICLOUD_CARDDAV_URL"], credentials, "<propfind xmlns=\"DAV:\" xmlns:card=\"urn:ietf:params:xml:ns:carddav\"><prop><current-user-principal/><card:addressbook-home-set/></prop></propfind>", {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
    hrefs = _hrefs(response.text)
    home = _property_href(response.text, "addressbook-home-set")
    if not home:
        principal = _property_href(response.text, "current-user-principal") or next((href for href in hrefs if "principal" in href), "")
        if principal:
            if principal.startswith("/"):
                principal = urljoin(config["ICLOUD_CARDDAV_URL"].rstrip("/") + "/", principal)
            principal_response = _icloud_request("PROPFIND", principal, credentials, "<propfind xmlns=\"DAV:\" xmlns:card=\"urn:ietf:params:xml:ns:carddav\"><prop><card:addressbook-home-set/></prop></propfind>", {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
            home = _property_href(principal_response.text, "addressbook-home-set")
    if not home:
        raise RuntimeError("Apple Contacts home was not discovered")
    if home.startswith("/"):
        home = urljoin(config["ICLOUD_CARDDAV_URL"].rstrip("/") + "/", home)
    collection = _icloud_request("PROPFIND", home, credentials, "<propfind xmlns=\"DAV:\"><prop><displayname/><resourcetype/></prop></propfind>", {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    addressbooks = [href for href in _hrefs(collection.text) if href.rstrip("/") != home.rstrip("/")]
    contact_query = "<c:addressbook-query xmlns:d=\"DAV:\" xmlns:c=\"urn:ietf:params:xml:ns:carddav\"><d:prop><c:address-data/></d:prop></c:addressbook-query>"
    contact_count = 0
    for addressbook_url in addressbooks[:20]:
        if addressbook_url.startswith("/"):
            addressbook_url = urljoin(config["ICLOUD_CARDDAV_URL"].rstrip("/") + "/", addressbook_url)
        try:
            contact_response = _icloud_request("REPORT", addressbook_url, credentials, contact_query, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
            contacts = _contact_data(contact_response.text)
            for contact in contacts:
                if contact.get("id") and contact.get("name"):
                    upsert_person(conn, "icloud", contact["id"], contact["name"], contact.get("emails", []), contact.get("importance", "normal"))
            contact_count += len(contacts)
        except requests.RequestException:
            continue
    set_context_signal(conn, "contacts", {"count": contact_count}, "apple-contacts", 0.7, _iso(_now() + timedelta(days=1)))
    mark_success(conn, "apple-contacts", {"addressbooks": len(addressbooks), "contacts": contact_count})
    return {"addressbooks": len(addressbooks), "contacts": contact_count}


def _apple_mail_event(item: dict, config: dict, preferences: dict, contexts: list[dict] | None = None) -> dict:
    message_id = item["message_id"]
    score, relevant, reason = score_item(item["topic"], item["subject"] or "Important iCloud Mail message", item["snippet"], item["keywords"], always_relevant=True, boost=max(0, item["score"] - 82))
    event = {"id": "icloud-mail-" + hashlib.sha256(message_id.encode()).hexdigest()[:24], "source_id": "icloud-mail", "source_kind": "icloud-mail", "topic": item["topic"], "title": item["subject"] or "Important iCloud Mail message", "summary": item["snippet"] or "Important message detected in iCloud Mail.", "body": f"{reason}; sender: {item['sender']}", "url": "https://www.icloud.com/mail/", "canonical_key": "mail:" + message_id, "published_at": None, "score": max(score, item["score"]), "priority": "critical" if score >= 90 else "high", "relevant": relevant, "metadata": {"source_trust": "primary", "mail_message_id": message_id, "sender": item["sender"], "tracking_number": item.get("tracking_number", ""), "important_person": item.get("important_person"), "sources": [{"url": "https://www.icloud.com/mail/", "title": "iCloud Mail", "trust": "primary"}]}}
    annotate_candidate(event, config.get("TRACKED_ENTITIES", []))
    apply_preference_adjustments(event, preferences)
    prepare_event_candidate(event, contexts)
    return event


def _sync_icloud_mail(conn, config: dict, credentials: dict) -> dict:
    mailbox = imaplib.IMAP4_SSL(config["ICLOUD_IMAP_HOST"], 993)
    try:
        mailbox.login(credentials["apple_id"], credentials["app_password"])
        mailbox.select("INBOX", readonly=True)
        _, data = mailbox.uid("search", None, "SINCE", (_now() - timedelta(days=7)).strftime("%d-%b-%Y"))
        uids = (data[0] or b"").split()[-25:]
        preferences = get_preferences(conn, config)
        created = 0
        for uid in uids:
            _, fetched = mailbox.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            raw = next((part[1] for part in fetched if isinstance(part, tuple)), b"")
            parsed = email.message_from_bytes(raw)
            subject = str(parsed.get("Subject", ""))
            try:
                subject = "".join(str(part, enc or "utf-8") if isinstance(part, bytes) else str(part) for part, enc in decode_header(subject))
            except (LookupError, UnicodeError):
                pass
            item = classify_apple_mail_message({"id": uid.decode(errors="ignore"), "snippet": subject, "payload": {"headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": str(parsed.get("From", ""))}]}})
            if not item.get("relevant"):
                continue
            item["important_person"] = important_person_for(conn, item.get("sender", ""))
            item["message_id"] = "icloud:" + uid.decode(errors="ignore")
            stored, was_created = upsert_event(conn, _apple_mail_event(item, config, preferences, active_context(conn)))
            plan = extract_plan_candidate(item, "apple-mail", config, stored["id"])
            if plan:
                save_extracted_plan(conn, plan)
            created += int(was_created)
        mark_success(conn, "apple-mail", {"messages_checked": len(uids), "created": created})
        return {"messages_checked": len(uids), "created": created}
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


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


def _gmail_event(item: dict, config: dict, preferences: dict, contexts: list[dict] | None = None) -> dict:
    message_id = item["message_id"]
    title = item["subject"] or "Important Gmail message"
    summary = item["snippet"] or "Important message detected in Gmail."
    score, relevant, reason = score_item(item["topic"], title, summary, item["keywords"], always_relevant=True, boost=max(0, item["score"] - 82))
    event = {
        "id": "gmail-" + hashlib.sha256(message_id.encode()).hexdigest()[:24], "source_id": "gmail", "source_kind": "gmail", "topic": item["topic"],
        "title": title, "summary": summary, "body": f"{reason}; sender: {item['sender']}", "url": "https://mail.google.com/mail/u/0/#all/" + message_id,
        "canonical_key": "gmail:" + message_id, "published_at": None, "score": max(score, item["score"]), "priority": "critical" if score >= 90 else "high", "relevant": relevant,
        "metadata": {"source_trust": "primary", "gmail_message_id": message_id, "sender": item["sender"], "important_person": item.get("important_person"), "tracking_number": item.get("tracking_number", ""), "lifecycle": item.get("lifecycle", {}), "sources": [{"url": "https://mail.google.com", "title": "Gmail", "trust": "primary"}]},
    }
    annotate_candidate(event, config.get("TRACKED_ENTITIES", []))
    apply_preference_adjustments(event, preferences)
    prepare_event_candidate(event, contexts)
    return event


def persist_gmail_lifecycle(conn, item: dict) -> None:
    result = {}
    if item.get("topic") == "package" and item.get("tracking_number"):
        text = (item.get("subject", "") + " " + item.get("snippet", "")).casefold()
        if "failed delivery" in text or "delivery failed" in text:
            status = "failed_delivery"
        elif "delivered" in text:
            status = "delivered"
        elif "out for delivery" in text:
            status = "out_for_delivery"
        elif "customs" in text:
            status = "customs"
        elif "exception" in text:
            status = "delivery_exception"
        elif "delay" in text or "delayed" in text:
            status = "delayed"
        else:
            status = "in_transit"
        result["package"] = upsert_package_record(conn, item["tracking_number"], item.get("sender", ""), status=status, source="gmail")
    if item.get("topic") == "purchase":
        text = (item.get("subject", "") + " " + item.get("snippet", "")).casefold()
        lifecycle = "refunded" if "refund" in text else "returned" if "return" in text else "cancelled" if "cancel" in text else "shipped" if "shipped" in text else "paid" if "payment" in text else "ordered"
        result["purchase"] = upsert_purchase_record(conn, "gmail:" + item.get("message_id", ""), merchant=item.get("sender", ""), title=item.get("subject", ""), lifecycle=lifecycle, metadata={"source": "gmail"})
    return result


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
        classified["important_person"] = important_person_for(conn, classified.get("sender", ""))
        classified["lifecycle"] = persist_gmail_lifecycle(conn, classified)
        event = _gmail_event(classified, config, preferences, active_context(conn))
        stored, was_created = upsert_event(conn, event)
        plan = extract_plan_candidate({**classified, "message_id": classified.get("message_id", item.get("id", ""))}, "gmail", config, stored["id"])
        if plan:
            save_extracted_plan(conn, plan)
        created += int(was_created)
    calendar = _google_get(token, "https://www.googleapis.com/calendar/v3/calendars/primary/events", {"maxResults": 20, "singleEvents": "true", "orderBy": "startTime", "timeMin": _iso()})
    for item in calendar.get("items", []):
        start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
        if not start:
            continue
        expires = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date") or start
        set_context_signal(conn, "calendar", {"event_id": item.get("id", ""), "title": item.get("summary", ""), "location": item.get("location", ""), "start": start, "end": expires}, "google-calendar", 0.98, expires)
    people_error = ""
    try:
        people = _google_get(token, "https://people.googleapis.com/v1/people/me/connections", {"pageSize": 100, "personFields": "names,emailAddresses"})
    except Exception as exc:
        # Contacts are optional; a People API issue must not block Gmail or Calendar.
        LOGGER.warning("Google People sync skipped error=%s", type(exc).__name__)
        people = {"connections": []}
        people_error = type(exc).__name__
    for person in people.get("connections", []):
        names = person.get("names") or []
        emails = person.get("emailAddresses") or []
        name = (names[0].get("displayName") if names else "") or ""
        addresses = [item.get("value", "") for item in emails if item.get("value")]
        if person.get("resourceName") and name:
            upsert_person(conn, "google", person["resourceName"], name, addresses)
    set_context_signal(conn, "contacts", {"count": len(people.get("connections", []))}, "google-people", 0.7, _iso(_now() + timedelta(days=1)))
    mark_success(conn, "google", {"gmail_messages": len(gmail.get("messages", [])), "calendar_events": len(calendar.get("items", [])), "contacts_count": len(people.get("connections", [])), "contacts_error": people_error})
    conn.commit()
    return {"created": created, "gmail_messages": len(gmail.get("messages", [])), "calendar_events": len(calendar.get("items", [])), "contacts_count": len(people.get("connections", []))}


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
    if provider in {"apple-calendar", "apple-contacts", "apple-mail"}:
        credentials = _icloud_credentials(conn, config)
        start_attempt(conn, provider)
        if provider == "apple-calendar":
            return _sync_icloud_calendar(conn, config, credentials)
        if provider == "apple-contacts":
            return _sync_icloud_contacts(conn, config, credentials)
        return _sync_icloud_mail(conn, config, credentials)
    if provider in {"apple", "apple-music"}:
        return {"context": len(active_context(conn))}
    if provider == "aws-bedrock":
        return {"configured": bool(config.get("BEDROCK_MODEL_ID"))}
    raise ValueError("unknown integration")


def active_context(conn) -> list[dict]:
    now = _iso()
    conn.execute("DELETE FROM context_signals WHERE expires_at IS NOT NULL AND expires_at<?", (now,))
    rows = conn.execute("SELECT rowid,* FROM context_signals WHERE expires_at IS NULL OR expires_at>=? ORDER BY observed_at DESC,rowid DESC", (now,)).fetchall()
    selected = {}
    for row in rows:
        rank = CONTEXT_SOURCE_PRIORITY.get(row["source"], 0)
        key = row["kind"]
        candidate = (rank, float(row["confidence"]), row["observed_at"], int(row["rowid"]))
        if key not in selected or candidate > selected[key][0]:
            selected[key] = (candidate, row)
    result = []
    for _, row in sorted(selected.values(), key=lambda item: item[1]["observed_at"], reverse=True):
        item = dict(row)
        item.pop("rowid", None)
        item["value"] = _safe_json(item.pop("value_json"), {})
        item["active"] = True
        result.append(item)
    return result
