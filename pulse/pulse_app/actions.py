from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .storage import upsert_plan_candidate


PLAN_WORDS = ("let's", "lets", "meet", "meeting", "call", "appointment", "class", "assignment", "deadline", "remind", "remember", "flight", "reservation", "play", "tonight", "tomorrow")
WEEKDAYS = {name: index for index, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
SUPPORTED_IPHONE_ACTIONS = {"create_calendar_event", "create_reminder", "open_url", "refresh_context"}


def _parse_time(text: str) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b", text.casefold())
    if not match:
        return None
    if match.group(4) is not None:
        return int(match.group(4)), int(match.group(5))
    hour, minute, suffix = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute


def _parse_date(text: str, now: datetime) -> datetime | None:
    lowered = text.casefold()
    if "today" in lowered or "tonight" in lowered:
        return now
    if "tomorrow" in lowered:
        return now + timedelta(days=1)
    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            delta = (weekday - now.weekday()) % 7 or 7
            return now + timedelta(days=delta)
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", lowered)
    if match:
        try:
            return now.replace(year=int(match.group(1)), month=int(match.group(2)), day=int(match.group(3)))
        except ValueError:
            return None
    return None


def extract_plan_candidate(message: dict, source: str, config: dict, source_event_id: str = "") -> dict | None:
    subject = str(message.get("subject") or message.get("title") or "").strip()
    snippet = str(message.get("snippet") or message.get("summary") or "").strip()
    text = f"{subject}. {snippet}".strip()
    lowered = text.casefold()
    if not text or not any(word in lowered for word in PLAN_WORDS):
        return None
    timezone_name = config.get("TIMEZONE", "America/Bogota")
    try:
        local_now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        local_now = datetime.now(timezone.utc)
    date_value = _parse_date(lowered, local_now)
    time_value = _parse_time(lowered)
    if date_value is None or time_value is None:
        return None
    local_start = date_value.replace(hour=time_value[0], minute=time_value[1], second=0, microsecond=0)
    start = local_start.astimezone(timezone.utc)
    confidence = 0.68 + (0.16 if date_value else 0) + (0.12 if time_value else 0)
    digest = hashlib.sha256((source + ":" + str(message.get("message_id") or message.get("id") or text)).encode()).hexdigest()[:24]
    return {
        "id": "plan-" + digest,
        "source": source,
        "source_event_id": source_event_id,
        "title": subject or "Possible plan",
        "summary": snippet[:700],
        "start_at": start.replace(microsecond=0).isoformat(),
        "end_at": (start + timedelta(hours=1)).replace(microsecond=0).isoformat(),
        "location": str(message.get("location") or "")[:240],
        "confidence": round(min(0.99, confidence), 2),
        "status": "proposed",
        "action_type": "calendar_event",
        "action_payload": {"summary": subject or "Pulse plan", "description": snippet[:1000], "location": str(message.get("location") or "")[:240]},
        "provenance": {"source": source, "message_id": str(message.get("message_id") or message.get("id") or "")[:240]},
    }


def save_extracted_plan(conn, plan: dict) -> dict:
    return upsert_plan_candidate(conn, plan)


def _normalized_datetime(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"invalid_{field}")
    if parsed.tzinfo is None:
        raise ValueError(f"timezone_required_{field}")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def validate_iphone_action(action_type: str, payload: dict) -> dict:
    action_type = str(action_type or "").strip()
    if action_type not in SUPPORTED_IPHONE_ACTIONS:
        raise ValueError("unsupported_action_type")
    if not isinstance(payload, dict):
        raise ValueError("action_payload_required")
    if action_type == "create_calendar_event":
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 240:
            raise ValueError("calendar_title_required")
        start = _normalized_datetime(payload.get("start"), "start")
        end = _normalized_datetime(payload.get("end"), "end")
        if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
            raise ValueError("calendar_end_must_follow_start")
        return {"title": title, "start": start, "end": end, "location": str(payload.get("location") or "").strip()[:240], "notes": str(payload.get("notes") or "").strip()[:2000], "calendar_name": str(payload.get("calendar_name") or "").strip()[:120], "pulse_action_id": str(payload.get("pulse_action_id") or "").strip()[:120]}
    if action_type == "create_reminder":
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 240:
            raise ValueError("reminder_title_required")
        due = payload.get("due")
        priority = int(payload.get("priority", 0) or 0)
        if priority not in {0, 1, 5, 9}:
            raise ValueError("invalid_reminder_priority")
        return {"title": title, "due": _normalized_datetime(due, "due") if due else "", "list": str(payload.get("list") or "").strip()[:120], "notes": str(payload.get("notes") or "").strip()[:2000], "priority": priority, "pulse_action_id": str(payload.get("pulse_action_id") or "").strip()[:120]}
    if action_type == "open_url":
        url = str(payload.get("url") or "").strip()[:1000]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("https_url_required")
        return {"url": url, "pulse_action_id": str(payload.get("pulse_action_id") or "").strip()[:120]}
    return {"pulse_action_id": str(payload.get("pulse_action_id") or "").strip()[:120]}
