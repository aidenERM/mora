from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .config import STRICT_MIN_THRESHOLDS

CRITICAL_TERMS = {
    "outage", "emergency", "warning", "critical", "recall", "security", "breach",
    "earthquake", "temblor", "cerrado", "cierre", "alerta", "patch notes",
}

TOKEN_STOPWORDS = {
    "about", "after", "all", "and", "announcement", "are", "for", "from", "has", "have",
    "into", "latest", "news", "notes", "official", "possible", "release", "that", "the",
    "this", "today", "update", "updates", "what", "with", "your",
}
TRUST_RANK = {"community": 1, "reliable_secondary": 2, "primary": 3}


def clean_text(value: str, limit: int = 800) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def normalize(value: str) -> str:
    return clean_text(value, 2000).casefold()


def significant_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9](?:[a-z0-9'./-]*[a-z0-9])?", normalize(value))
        if len(token) >= 3 and token not in TOKEN_STOPWORDS and not token.isdigit()
    }


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        query = "&".join(item for item in parsed.query.split("&") if item and not item.lower().startswith(("utm_", "fbclid", "gclid")))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", query, ""))
    except ValueError:
        return value.strip()


def _source_prefixes(event: dict) -> list[str]:
    metadata = event.get("metadata") or {}
    values = [metadata.get("source_label", ""), event.get("source_id", "")]
    for source in metadata.get("sources") or []:
        values.append(source.get("title", ""))
    prefixes = []
    for value in values:
        value = clean_text(str(value or ""), 100)
        if value:
            prefixes.append(value)
            prefixes.append(value.replace("-", " ").replace("_", " "))
    return list(dict.fromkeys(item for item in prefixes if len(item) >= 3))


def clean_event_title(event: dict) -> str:
    title = clean_text(event.get("title", "Untitled"), 240)
    for prefix in _source_prefixes(event):
        title = re.sub(r"^\s*" + re.escape(prefix) + r"\s*[:|\-]\s*", "", title, flags=re.I)
    title = re.sub(r"^\s*\[[^\]]{2,60}\]\s*", "", title)
    title = re.sub(r"\s*[|]\s*(?:via|from)\s+[^|]+$", "", title, flags=re.I)
    title = clean_text(title, 180)
    return title or "Pulse event"


def clean_event_summary(event: dict) -> str:
    title = clean_event_title(event)
    summary = clean_text(event.get("summary") or event.get("body") or "Open Pulse for details.", 700)
    if normalize(summary).startswith(normalize(title)):
        summary = re.sub(r"^\s*" + re.escape(title) + r"\s*[:|\-]?\s*", "", summary, flags=re.I)
    summary = re.sub(r"^(?:source|feed|via)\s*:\s*", "", summary, flags=re.I)
    return clean_text(summary, 700) or "Open Pulse for details."


def notification_copy(event: dict) -> tuple[str, str]:
    """Produce short, user-facing copy instead of forwarding feed wording."""
    title = clean_event_title(event)
    body = clean_event_summary(event)
    metadata = event.get("metadata") or {}
    topic = event.get("topic")
    if topic == "weather":
        label = metadata.get("weather_label") or "your area"
        why = f"It may affect plans around {label}."
    elif topic == "earthquake":
        why = "Pulse estimated that it could matter near La Ceja."
    elif metadata.get("entity_names"):
        names = ", ".join(str(item) for item in metadata["entity_names"][:3])
        why = f"It matches your tracked interest in {names}."
    else:
        why = {
            "warzone": "It is a potentially meaningful Warzone update.",
            "apple": "It is a potentially meaningful Apple or tech update.",
            "github": "It affects a project Pulse is watching.",
            "colombia": "It may affect Colombia or local plans.",
            "watcher": "It changed a source you asked Pulse to watch.",
            "system": "It was created directly in Pulse.",
        }.get(topic, "Pulse judged it worth checking.")
    if normalize(why.rstrip(".")) not in normalize(body):
        body = clean_text(body.rstrip(". ") + ". " + why, 220)
    return title[:120], body[:220]


def _weather_signature(metadata: dict) -> str:
    signature = metadata.get("alert_signature")
    if signature:
        return str(signature)
    return "|".join(str(metadata.get(key, "")) for key in ("weather_category", "alert_window"))


def event_fingerprint(event: dict) -> str:
    """Hash only material event state, not source bookkeeping or explanatory text."""
    metadata = event.get("metadata") or {}
    if metadata.get("weather_category"):
        payload = [event.get("topic"), _weather_signature(metadata)]
    elif metadata.get("earthquake_id"):
        payload = [event.get("topic"), metadata.get("earthquake_id")]
    else:
        payload = [
            event.get("topic"),
            clean_event_title(event),
            clean_event_summary(event),
            sorted(metadata.get("entities") or []),
        ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def events_similar(left: dict, right: dict) -> bool:
    if left.get("topic") != right.get("topic"):
        return False
    left_meta = left.get("metadata") or {}
    right_meta = right.get("metadata") or {}
    if left_meta.get("weather_category") or right_meta.get("weather_category"):
        return (
            left_meta.get("weather_category") == right_meta.get("weather_category")
            and left_meta.get("alert_window") == right_meta.get("alert_window")
        )
    if left_meta.get("earthquake_id") or right_meta.get("earthquake_id"):
        return left_meta.get("earthquake_id") == right_meta.get("earthquake_id")
    left_tokens = significant_tokens(clean_event_title(left))
    right_tokens = significant_tokens(clean_event_title(right))
    overlap = left_tokens & right_tokens
    if len(overlap) >= 3:
        union = left_tokens | right_tokens
        if len(overlap) / max(1, len(union)) >= 0.35:
            return True
    return SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio() >= 0.78


def materially_changed(old: dict, new: dict, score_delta: int = 12) -> bool:
    old_meta = old.get("metadata") or {}
    new_meta = new.get("metadata") or {}
    if old_meta.get("weather_category") or new_meta.get("weather_category"):
        return _weather_signature(old_meta) != _weather_signature(new_meta)
    if old_meta.get("earthquake_id") or new_meta.get("earthquake_id"):
        return (
            old_meta.get("local_impact") != new_meta.get("local_impact")
            or int(new.get("score", 0)) - int(old.get("score", 0)) >= score_delta
        )
    old_title = clean_event_title(old)
    new_title = clean_event_title(new)
    old_summary = clean_event_summary(old)
    new_summary = clean_event_summary(new)
    if old_title.casefold() != new_title.casefold() and SequenceMatcher(None, normalize(old_title), normalize(new_title)).ratio() < 0.82:
        return True
    summary_ratio = SequenceMatcher(None, normalize(old_summary), normalize(new_summary)).ratio()
    old_summary_tokens = significant_tokens(old_summary)
    new_summary_tokens = significant_tokens(new_summary)
    summary_overlap = len(old_summary_tokens & new_summary_tokens) / max(1, len(old_summary_tokens | new_summary_tokens))
    if summary_ratio < 0.64 and summary_overlap < 0.65:
        return True
    old_terms = significant_tokens(old_title + " " + old_summary)
    new_terms = significant_tokens(new_title + " " + new_summary)
    if any(term in new_terms and term not in old_terms for term in CRITICAL_TERMS):
        return True
    return int(new.get("score", 0)) - int(old.get("score", 0)) >= score_delta


def score_item(topic: str, title: str, summary: str, keywords: list[str], always_relevant: bool = False, boost: int = 0) -> tuple[int, bool, str]:
    haystack = normalize(title + " " + summary)
    if always_relevant:
        score = 82 + boost
        reason = "watched directly"
    else:
        hits = [item for item in keywords if normalize(item) in haystack]
        if not hits:
            return 0, False, "no matching relevance rule"
        score = 48 + min(36, len(hits) * 12) + boost
        reason = "matched " + ", ".join(hits[:3])
    if any(term in haystack for term in CRITICAL_TERMS):
        score += 12
        reason += "; high-impact wording"
    score = max(0, min(100, score))
    return score, True, reason


def matched_entities(text: str, entities: list[dict]) -> list[dict]:
    normalized = normalize(text)
    matches = []
    for entity in entities or []:
        aliases = entity.get("aliases") or [entity.get("name", "")]
        if any(re.search(r"(?<!\w)" + re.escape(normalize(alias)) + r"(?!\w)", normalized) for alias in aliases if alias):
            matches.append(entity)
    return matches


def annotate_candidate(candidate: dict, entities: list[dict]) -> dict:
    metadata = dict(candidate.get("metadata") or {})
    matches = matched_entities(candidate.get("title", "") + " " + candidate.get("summary", ""), entities)
    boost = min(30, sum(max(0, int(item.get("boost", 0))) for item in matches))
    if boost:
        candidate["score"] = min(100, int(candidate.get("score", 0)) + boost)
        names = ", ".join(item.get("name", item.get("id", "")) for item in matches[:4])
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; tracked: " + names
    metadata["entities"] = [item.get("id") for item in matches if item.get("id")]
    metadata["entity_names"] = [item.get("name", item.get("id", "")) for item in matches]
    candidate["metadata"] = metadata
    candidate["priority"] = priority_for(int(candidate.get("score", 0)))
    return candidate


def apply_preference_adjustments(candidate: dict, preferences: dict) -> dict:
    """Apply small, inspectable preference changes after deterministic scoring."""
    metadata = candidate.get("metadata") or {}
    entity_ids = set(metadata.get("entities") or [])
    followed = set((preferences.get("followed_entities") or {}).keys())
    less_like_entities = set((preferences.get("less_like_entities") or {}).keys())
    less_like_topics = set((preferences.get("less_like_topics") or {}).keys())
    adjustment = 0
    reasons = []
    base_score = int(candidate.get("score", 0))
    followed_matches = entity_ids & followed
    if followed_matches:
        adjustment += min(24, 12 * len(followed_matches))
        reasons.append("followed interest")
    less_matches = entity_ids & less_like_entities
    if less_matches:
        adjustment -= min(36, 18 * len(less_matches))
        reasons.append("less-like feedback")
    if candidate.get("topic") in less_like_topics:
        adjustment -= 24
        reasons.append("less-like topic")
    topic_weight = int((preferences.get("learned_topic_weights") or {}).get(candidate.get("topic"), 0))
    if topic_weight:
        adjustment += max(-12, min(12, topic_weight))
    source_weight = int((preferences.get("learned_source_weights") or {}).get(candidate.get("source_id"), 0))
    if source_weight:
        adjustment += max(-8, min(8, source_weight))
        reasons.append("source learning")
    if (candidate.get("metadata") or {}).get("safety_critical") and adjustment < 0:
        adjustment = max(0, adjustment)
        reasons.append("safety floor")
    if adjustment:
        candidate["score"] = max(0, min(100, int(candidate.get("score", 0)) + adjustment))
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; " + ", ".join(reasons)
        candidate["priority"] = priority_for(candidate["score"])
    return candidate


def priority_for(score: int) -> str:
    if score >= 90:
        return "critical"
    if score >= 80:
        return "high"
    if score >= 65:
        return "normal"
    return "low"


def _minutes(value: str) -> int:
    hour, minute = [int(part) for part in value.split(":", 1)]
    if hour not in range(24) or minute not in range(60):
        raise ValueError
    return hour * 60 + minute


def is_quiet_hours(now: datetime, start: str, end: str, timezone_name: str) -> bool:
    local = now.astimezone(ZoneInfo(timezone_name))
    current = local.hour * 60 + local.minute
    begin, finish = _minutes(start), _minutes(end)
    if begin == finish:
        return False
    return current >= begin or current < finish if begin > finish else begin <= current < finish


def muted_until(prefs: dict, topic: str) -> str | None:
    value = (prefs.get("muted_topics") or {}).get(topic)
    return value if value and value > datetime.now(timezone.utc).replace(microsecond=0).isoformat() else None


def _parse_datetime(value: str | None, timezone_name: str = "UTC") -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safety_critical(event: dict, config: dict) -> bool:
    metadata = event.get("metadata") or {}
    return bool(metadata.get("safety_critical")) or (
        int(event.get("score", 0)) >= int(config.get("URGENT_NOTIFY_SCORE", 98))
        and event.get("topic") in {"weather", "earthquake"}
    )


def _fresh_for_notification(event: dict, now: datetime, config: dict) -> bool:
    metadata = event.get("metadata") or {}
    timezone_name = config.get("TIMEZONE", "UTC")
    if event.get("topic") == "weather" and metadata.get("latest_forecast_at"):
        latest = _parse_datetime(metadata.get("latest_forecast_at"), timezone_name)
        if latest and latest < now - timedelta(minutes=15):
            return False
        return True
    max_age = int((config.get("NOTIFICATION_MAX_AGE") or {}).get(event.get("topic"), 720))
    timestamp = _parse_datetime(event.get("published_at") or event.get("discovered_at"), timezone_name)
    if timestamp and now - timestamp > timedelta(minutes=max_age):
        return False
    return True


def _cooldown_active(event: dict, now: datetime, config: dict, conn=None) -> bool:
    cooldown = int((config.get("NOTIFICATION_COOLDOWNS") or {}).get(event.get("topic"), 180))
    if cooldown <= 0:
        return False
    if _safety_critical(event, config) and int(event.get("score", 0)) >= int(config.get("URGENT_NOTIFY_SCORE", 98)):
        return False
    last = _parse_datetime(event.get("last_notification_at"))
    if last and now - last < timedelta(minutes=cooldown):
        return True
    if conn is None:
        return False
    row = conn.execute(
        "SELECT MAX(last_notification_at) AS last_sent FROM events WHERE topic=? AND id<>? AND last_notification_at IS NOT NULL",
        (event.get("topic"), event.get("id")),
    ).fetchone()
    last_topic = _parse_datetime(row["last_sent"] if row else None)
    return bool(last_topic and now - last_topic < timedelta(minutes=cooldown))


def notification_decision(event: dict, prefs: dict, now: datetime | None = None, config: dict | None = None, conn=None) -> tuple[bool, str]:
    config = config or {
        "STRICT_MIN_THRESHOLDS": STRICT_MIN_THRESHOLDS,
        "URGENT_NOTIFY_SCORE": 98,
        "NOTIFICATION_COOLDOWNS": {},
        "NOTIFICATION_MAX_AGE": {},
        "TIMEZONE": prefs.get("timezone", "UTC"),
    }
    now = now or datetime.now(timezone.utc)
    if not event.get("relevant"):
        return False, "not relevant"
    threshold = max(
        int((prefs.get("topic_thresholds") or {}).get(event["topic"], 80)),
        int((config.get("STRICT_MIN_THRESHOLDS") or STRICT_MIN_THRESHOLDS).get(event.get("topic"), 80)),
    )
    if event["score"] < threshold:
        return False, f"score {event['score']} below {threshold} threshold"
    if muted_until(prefs, event["topic"]):
        return False, "topic muted"
    if not _fresh_for_notification(event, now, config):
        return False, "stale event"
    bypass = max(int(prefs.get("quiet_bypass_priority", 98)), int(config.get("URGENT_NOTIFY_SCORE", 98)))
    if event["score"] < bypass and is_quiet_hours(now, prefs["quiet_start"], prefs["quiet_end"], prefs["timezone"]):
        return False, "quiet hours"
    if _cooldown_active(event, now, config, conn):
        return False, "topic or event cooldown"
    return True, "relevant, fresh, and above strict threshold"


def should_notify(event: dict, prefs: dict, now: datetime | None = None, config: dict | None = None, conn=None) -> tuple[bool, str]:
    return notification_decision(event, prefs, now, config, conn)
