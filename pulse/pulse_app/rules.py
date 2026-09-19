from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

CRITICAL_TERMS = {
    "outage", "emergency", "warning", "critical", "recall", "security", "breach",
    "earthquake", "temblor", "cerrado", "cierre", "alerta", "patch notes",
}


def clean_text(value: str, limit: int = 800) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def normalize(value: str) -> str:
    return clean_text(value, 2000).casefold()


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        query = "&".join(item for item in parsed.query.split("&") if item and not item.lower().startswith(("utm_", "fbclid", "gclid")))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", query, ""))
    except ValueError:
        return value.strip()


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


def should_notify(event: dict, prefs: dict, now: datetime | None = None) -> tuple[bool, str]:
    if not event.get("relevant"):
        return False, "not relevant"
    threshold = int((prefs.get("topic_thresholds") or {}).get(event["topic"], 80))
    if event["score"] < threshold:
        return False, f"score {event['score']} below {threshold} threshold"
    if muted_until(prefs, event["topic"]):
        return False, "topic muted"
    now = now or datetime.now(timezone.utc)
    bypass = int(prefs.get("quiet_bypass_priority", 90))
    if event["score"] < bypass and is_quiet_hours(now, prefs["quiet_start"], prefs["quiet_end"], prefs["timezone"]):
        return False, "quiet hours"
    return True, "relevant and above threshold"
