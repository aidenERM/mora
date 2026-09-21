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
TRUST_CONTRIBUTION = {"community": -8, "reliable_secondary": 2, "primary": 8}


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
        impact = metadata.get("plan_impact") or f"It may affect plans around {label}."
        why = f"{impact}"
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
            "package": "It is a meaningful delivery-status change.",
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


def cluster_id_for(event: dict) -> str:
    metadata = event.get("metadata") or {}
    if metadata.get("cluster_id"):
        return str(metadata["cluster_id"])
    if metadata.get("weather_category"):
        identity = [event.get("topic"), metadata.get("weather_category"), metadata.get("alert_window")]
    elif metadata.get("earthquake_id"):
        identity = [event.get("topic"), metadata.get("earthquake_id")]
    else:
        identity = [event.get("topic"), sorted(significant_tokens(clean_event_title(event))), sorted(metadata.get("entities") or [])]
    return "cluster-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def domain_adjustment(candidate: dict) -> tuple[int, str]:
    """Small domain-aware quality adjustment, kept deterministic and inspectable."""
    topic = candidate.get("topic")
    text = normalize((candidate.get("title", "") + " " + candidate.get("summary", "")))
    if topic == "warzone":
        useful = ("patch", "balance", "nerf", "buff", "weapon", "ranked", "outage", "season", "ricochet")
        low_value = ("store", "bundle", "skin", "cosmetic", "creator code", "rumor")
        if any(term in text for term in useful):
            return 8, "gaming impact signal"
        if any(term in text for term in low_value):
            return -14, "gaming low-value signal"
    if topic == "apple":
        useful = ("release", "released", "ios", "security", "recall", "available", "update", "announcement")
        low_value = ("rumor", "concept", "case", "accessory", "deal", "wallpaper")
        if any(term in text for term in useful):
            return 8, "product-impact signal"
        if any(term in text for term in low_value):
            return -12, "apple low-value signal"
    if topic == "github":
        useful = ("security", "breaking", "major", "release", "deprecated", "outage")
        if any(term in text for term in useful):
            return 8, "developer-impact signal"
        if candidate.get("source_kind") in {"github", "github_webhook"}:
            return -6, "routine developer activity"
    return 0, "domain profile neutral"


def notification_tier(event: dict, config: dict | None = None) -> str:
    config = config or {"URGENT_NOTIFY_SCORE": 98}
    if _safety_critical(event, config) and int(event.get("score", 0)) >= int(config.get("URGENT_NOTIFY_SCORE", 98)):
        return "urgent"
    score = int(event.get("score", 0))
    if score >= 88:
        return "high"
    if score >= 75:
        return "normal"
    return "low"


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
    components = dict(metadata.get("score_components") or {})
    base_score = int(candidate.get("score", 0))
    components.setdefault("source_rule", base_score)
    trust = metadata.get("source_trust", "reliable_secondary")
    components.setdefault("source_trust", TRUST_CONTRIBUTION.get(trust, 0))
    matches = matched_entities(candidate.get("title", "") + " " + candidate.get("summary", ""), entities)
    boost = min(30, sum(max(0, int(item.get("boost", 0))) for item in matches))
    if boost:
        candidate["score"] = min(100, int(candidate.get("score", 0)) + boost)
        names = ", ".join(item.get("name", item.get("id", "")) for item in matches[:4])
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; tracked: " + names
    components["personal_interest"] = boost
    adjustment, adjustment_reason = domain_adjustment(candidate)
    if adjustment:
        candidate["score"] = max(0, min(100, int(candidate.get("score", 0)) + adjustment))
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; " + adjustment_reason
    components["domain"] = adjustment
    components["final"] = int(candidate.get("score", 0))
    metadata["score_components"] = components
    metadata["cluster_id"] = cluster_id_for(candidate)
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
    manual_weight = int((preferences.get("personal_priorities") or {}).get(candidate.get("topic"), 0))
    manual_weight = max(-20, min(20, manual_weight))
    if manual_weight:
        adjustment += manual_weight
        reasons.append("personal priority")
    temporary = (preferences.get("temporary_priority") or {}).get(candidate.get("topic"))
    if isinstance(temporary, dict):
        try:
            expires_at = datetime.fromisoformat(str(temporary.get("expires_at", "")).replace("Z", "+00:00"))
            if expires_at > datetime.now(timezone.utc):
                temporary_weight = max(-20, min(20, int(temporary.get("boost", 0))))
                adjustment += temporary_weight
                if temporary_weight:
                    reasons.append("temporary priority")
        except (TypeError, ValueError):
            pass
    safety_floor = (candidate.get("metadata") or {}).get("safety_critical") or (int(candidate.get("score", 0)) >= 98 and candidate.get("topic") in {"weather", "earthquake"})
    if safety_floor and adjustment < 0:
        adjustment = max(0, adjustment)
        reasons.append("safety floor")
    if adjustment:
        candidate["score"] = max(0, min(100, int(candidate.get("score", 0)) + adjustment))
        candidate["body"] = (candidate.get("body") or "matched relevance rules") + "; " + ", ".join(reasons)
        candidate["priority"] = priority_for(candidate["score"])
    components = dict((candidate.get("metadata") or {}).get("score_components") or {})
    components["learning"] = adjustment
    components["final"] = int(candidate.get("score", 0))
    candidate.setdefault("metadata", {})["score_components"] = components
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


def _freshness_details(event: dict, now: datetime, config: dict) -> dict:
    metadata = event.get("metadata") or {}
    timezone_name = config.get("TIMEZONE", "UTC")
    timestamp_value = metadata.get("latest_forecast_at") if event.get("topic") == "weather" else (event.get("published_at") or event.get("discovered_at"))
    timestamp = _parse_datetime(timestamp_value, timezone_name)
    if not timestamp:
        return {"state": "unknown", "score": 50, "age_minutes": None, "max_age_minutes": None}
    age = round((now - timestamp).total_seconds() / 60)
    if event.get("topic") == "weather":
        fresh = age <= 15
        return {"state": "fresh" if fresh else "stale", "score": 100 if fresh else 0, "age_minutes": age, "max_age_minutes": 15}
    max_age = int((config.get("NOTIFICATION_MAX_AGE") or {}).get(event.get("topic"), 720))
    score = max(0, min(100, round(100 - max(0, age) * 100 / max(1, max_age))))
    return {"state": "fresh" if age <= max_age else "stale", "score": score, "age_minutes": age, "max_age_minutes": max_age}


def _cooldown_details(event: dict, now: datetime, config: dict, conn=None) -> dict:
    cooldown = int((config.get("NOTIFICATION_COOLDOWNS") or {}).get(event.get("topic"), 180))
    details = {"active": False, "minutes": cooldown, "event_last": event.get("last_notification_at"), "topic_last": None, "until": None}
    if cooldown <= 0:
        return details
    if _safety_critical(event, config) and int(event.get("score", 0)) >= int(config.get("URGENT_NOTIFY_SCORE", 98)):
        details["bypassed_for_safety"] = True
        return details
    candidates = []
    event_last = _parse_datetime(event.get("last_notification_at"))
    if event_last:
        candidates.append(("event", event_last))
    if conn is not None:
        row = conn.execute(
            "SELECT MAX(last_notification_at) AS last_sent FROM events WHERE topic=? AND id<>? AND last_notification_at IS NOT NULL",
            (event.get("topic"), event.get("id")),
        ).fetchone()
        topic_last = _parse_datetime(row["last_sent"] if row else None)
        if topic_last:
            candidates.append(("topic", topic_last))
            details["topic_last"] = topic_last.isoformat()
    if candidates:
        kind, latest = max(candidates, key=lambda item: item[1])
        until = latest + timedelta(minutes=cooldown)
        details.update({"active": now < until, "kind": kind, "until": until.isoformat()})
    return details


def _meaningful_development(event: dict, threshold: int, score: int, config: dict) -> bool:
    metadata = event.get("metadata") or {}
    return bool(metadata.get("development_meaningful")) and score >= threshold + int(config.get("DEVELOPMENT_OVERRIDE_DELTA", 8))


def _rolling_frequency_details(event: dict, now: datetime, config: dict, conn, tier: str) -> dict:
    limits = config.get("ROLLING_NOTIFICATION_LIMITS") or {}
    details = {"active": False, "bypassed_for_safety": False, "blocked_by": [], "checks": []}
    if conn is None:
        return details
    checks = []
    global_limit = limits.get("global") or {}
    if int(global_limit.get("count", 0)) > 0:
        checks.append(("global", global_limit, ""))
    topic_limits = limits.get("topic") or {}
    topic_limit = topic_limits.get(event.get("topic")) or topic_limits.get("default") or {}
    if int(topic_limit.get("count", 0)) > 0:
        checks.append(("topic", topic_limit, event.get("topic", "")))
    tier_limit = (limits.get("tier") or {}).get(tier) or {}
    if int(tier_limit.get("count", 0)) > 0:
        checks.append(("tier", tier_limit, tier))
    for kind, spec, value in checks:
        count_limit = int(spec.get("count", 0))
        minutes = max(1, int(spec.get("minutes", 60)))
        cutoff = (now - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
        if kind == "global":
            row = conn.execute("SELECT COUNT(*) AS count FROM events WHERE notification_count>0 AND last_notification_at>=?", (cutoff,)).fetchone()
        elif kind == "topic":
            row = conn.execute("SELECT COUNT(*) AS count FROM events WHERE notification_count>0 AND last_notification_at>=? AND topic=?", (cutoff, value)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM events WHERE notification_count>0 AND last_notification_at>=? AND priority=?",
                (cutoff, "critical" if value == "urgent" else value),
            ).fetchone()
        count = int(row["count"] if row else 0)
        exceeded = count >= count_limit
        details["checks"].append({"kind": kind, "key": value or "all", "count": count, "limit": count_limit, "minutes": minutes, "exceeded": exceeded})
        if exceeded:
            details["blocked_by"].append(kind if not value else f"{kind}:{value}")
    details["active"] = bool(details["blocked_by"])
    if details["active"] and _safety_critical(event, config) and tier == "urgent":
        details["bypassed_for_safety"] = True
        details["active"] = False
    return details


def _topic_cooling_details(event: dict, now: datetime, config: dict, conn, threshold: int, score: int) -> dict:
    cooling = config.get("TOPIC_COOLING") or {}
    details = {"active": False, "threshold_bonus": 0, "threshold": threshold, "count": 0, "decay": 0, "overridden_for_development": False}
    if conn is None or int(cooling.get("trigger_count", 0)) <= 0:
        return details
    window = max(1, int(cooling.get("window_minutes", 180)))
    cutoff = (now - timedelta(minutes=window)).replace(microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS count,MAX(last_notification_at) AS latest FROM events WHERE notification_count>0 AND last_notification_at>=? AND topic=?",
        (cutoff, event.get("topic")),
    ).fetchone()
    count = int(row["count"] if row else 0)
    if count < int(cooling.get("trigger_count", 3)):
        return details
    latest = _parse_datetime(row["latest"] if row else None)
    decay_minutes = max(1, int(cooling.get("decay_minutes", 360)))
    decay = max(0.0, min(1.0, 1.0 - ((now - latest).total_seconds() / 60 / decay_minutes))) if latest else 1.0
    base_bonus = min(int(cooling.get("max_threshold_bonus", 12)), (count - int(cooling.get("trigger_count", 3)) + 1) * int(cooling.get("threshold_step", 4)))
    bonus = max(0, round(base_bonus * decay))
    details.update({"active": bonus > 0, "threshold_bonus": bonus, "threshold": threshold + bonus, "count": count, "decay": round(decay, 2), "latest": row["latest"] if row else None})
    if details["active"] and _meaningful_development(event, threshold, score, config):
        details["overridden_for_development"] = True
        details["threshold"] = threshold
    return details


def _trend_details(event: dict, now: datetime, config: dict, conn) -> dict:
    details = {"bonus": 0, "credible_sources": 0, "recent_observations": 0, "meaningful_developments": 0, "local": False, "score_trajectory": 0}
    if conn is None or not event.get("canonical_event_id"):
        return details
    cutoff = (now - timedelta(hours=int(config.get("TREND_WINDOW_HOURS", 72)))).replace(microsecond=0).isoformat()
    observations = conn.execute(
        "SELECT source_id,source_trust,observed_at FROM event_observations WHERE canonical_event_id=? AND observed_at>=?",
        (event["canonical_event_id"], cutoff),
    ).fetchall()
    source_trust = {}
    for row in observations:
        source_trust.setdefault(row["source_id"], row["source_trust"])
    credible_sources = {source for source, trust in source_trust.items() if TRUST_RANK.get(trust, 0) >= TRUST_RANK["reliable_secondary"]}
    details["credible_sources"] = len(credible_sources)
    details["recent_observations"] = len(observations)
    development_row = conn.execute(
        "SELECT COUNT(*) AS count FROM event_developments WHERE canonical_event_id=? AND discovered_at>=? AND meaningful=1",
        (event["canonical_event_id"], cutoff),
    ).fetchone()
    details["meaningful_developments"] = int(development_row["count"] if development_row else 0)
    metadata = event.get("metadata") or {}
    details["local"] = bool(metadata.get("local_impact") or metadata.get("weather_label") or metadata.get("location"))
    prior_rows = conn.execute(
        "SELECT trace FROM event_decisions WHERE cluster_id=? AND evaluated_at>=? ORDER BY evaluated_at DESC LIMIT 20",
        (event.get("cluster_id", ""), cutoff),
    ).fetchall()
    prior_scores = []
    for row in prior_rows:
        try:
            prior_scores.append(int(json.loads(row["trace"] or "{}").get("effective_score", 0)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if prior_scores and int(event.get("score", 0)) >= max(prior_scores) + 6:
        details["score_trajectory"] = 1
    weights = config.get("TREND_BONUS") or {}
    if details["credible_sources"] < 2:
        return details
    bonus = int(weights.get("source", 4)) * min(2, details["credible_sources"] - 1)
    bonus += int(weights.get("velocity", 2)) if details["recent_observations"] >= 3 else 0
    bonus += int(weights.get("development", 3)) * min(2, max(0, details["meaningful_developments"] - 1))
    bonus += int(weights.get("local", 2)) if details["local"] else 0
    bonus += int(weights.get("trajectory", 3)) if details["score_trajectory"] else 0
    details["bonus"] = min(20, max(0, bonus))
    return details


def _quiet_hours_details(event: dict, now: datetime, prefs: dict, config: dict, tier: str, score: int) -> dict:
    active = is_quiet_hours(now, prefs["quiet_start"], prefs["quiet_end"], prefs["timezone"])
    safety_bypass = bool(active and _safety_critical(event, config) and tier == "urgent" and score >= int(config.get("URGENT_NOTIFY_SCORE", 98)))
    metadata = event.get("metadata") or {}
    time_sensitive = bool(metadata.get("time_sensitive")) or bool(metadata.get("safety_critical"))
    high_bypass = bool(active and tier == "high" and time_sensitive and score >= 95)
    bypassed = safety_bypass or high_bypass
    return {
        "active": active,
        "affected": bool(active and not bypassed),
        "bypassed": bypassed,
        "tier": tier,
        "time_sensitive": time_sensitive,
        "reason": "safety-critical bypass" if safety_bypass else "time-sensitive high-tier bypass" if high_bypass else "quiet hours" if active else "outside quiet hours",
    }


def evaluate_notification(event: dict, prefs: dict, now: datetime | None = None, config: dict | None = None, conn=None) -> tuple[bool, str, dict]:
    config = config or {
        "STRICT_MIN_THRESHOLDS": STRICT_MIN_THRESHOLDS,
        "URGENT_NOTIFY_SCORE": 98,
        "NOTIFICATION_COOLDOWNS": {},
        "NOTIFICATION_MAX_AGE": {},
        "TIMEZONE": prefs.get("timezone", "UTC"),
    }
    now = now or datetime.now(timezone.utc)
    base_threshold = max(
        int((prefs.get("topic_thresholds") or {}).get(event["topic"], 80)),
        int((config.get("STRICT_MIN_THRESHOLDS") or STRICT_MIN_THRESHOLDS).get(event.get("topic"), 80)),
    )
    raw_score = int(event.get("score", 0))
    trend = _trend_details(event, now, config, conn)
    score_after_trend = min(100, raw_score + int(trend.get("bonus", 0)))
    threshold = base_threshold
    cooling = _topic_cooling_details(event, now, config, conn, base_threshold, score_after_trend)
    if cooling["active"] and not cooling["overridden_for_development"]:
        threshold = int(cooling["threshold"])
    freshness = _freshness_details(event, now, config)
    cooldown = _cooldown_details(event, now, config, conn)
    components = dict((event.get("metadata") or {}).get("score_components") or {})
    # Keep source scoring stable, but make older stories gradually less likely
    # to interrupt. Unknown timestamps are left unchanged; they should be
    # observable in the trace rather than silently penalized.
    if freshness["state"] == "unknown":
        effective_score = raw_score
    else:
        freshness_factor = 0.5 + (float(freshness["score"]) / 200.0)
        effective_score = max(0, min(100, round(score_after_trend * freshness_factor)))
    components["trend_escalation"] = int(trend.get("bonus", 0))
    components["freshness_decay"] = score_after_trend - effective_score
    components.setdefault("local_relevance", int((event.get("metadata") or {}).get("local_relevance_contribution", 0)))
    tier_event = {**event, "score": score_after_trend}
    tier = notification_tier(tier_event, config)
    quiet = _quiet_hours_details(event, now, prefs, config, tier, effective_score)
    frequency = _rolling_frequency_details(tier_event, now, config, conn, tier)
    trace = {
        "evaluated_at": now.replace(microsecond=0).isoformat(),
        "canonical_event_id": event.get("canonical_event_id", ""),
        "cluster_id": event.get("cluster_id") or (event.get("metadata") or {}).get("cluster_id", ""),
        "development_id": event.get("development_id") or (event.get("metadata") or {}).get("development_id", ""),
        "normalized_title": event.get("normalized_title") or normalize(clean_event_title(event)),
        "topic": event.get("topic"),
        "entities": event.get("normalized_entities") or (event.get("metadata") or {}).get("entities", []),
        "location": event.get("normalized_location") or (event.get("metadata") or {}).get("location", ""),
        "score": raw_score,
        "score_after_trend": score_after_trend,
        "effective_score": effective_score,
        "base_threshold": base_threshold,
        "threshold": threshold,
        "score_components": components,
        "source_trust": (event.get("metadata") or {}).get("source_trust", "unknown"),
        "source_trust_contribution": components.get("source_trust", 0),
        "personalized_interest_contribution": components.get("personal_interest", 0) + components.get("learning", 0),
        "local_relevance": (event.get("metadata") or {}).get("local_impact") or (event.get("metadata") or {}).get("weather_label", ""),
        "local_relevance_contribution": components.get("local_relevance", 0),
        "domain_contribution": components.get("domain", 0),
        "freshness": freshness,
        "notification_tier": tier,
        "cooldown": cooldown,
        "rolling_frequency": frequency,
        "topic_cooling": cooling,
        "quiet_hours": quiet,
        "trend": trend,
        "pushed": bool(event.get("notification_count", 0)),
        "safety_critical": _safety_critical(event, config),
    }
    if event.get("suppress_notification"):
        return False, "source initialization", trace
    if not event.get("relevant"):
        return False, "not relevant", trace
    if effective_score < base_threshold:
        return False, f"score {effective_score} below {base_threshold} threshold after freshness decay", trace
    if cooling["active"] and not cooling["overridden_for_development"] and effective_score < threshold:
        return False, "temporary topic cooling", trace
    if muted_until(prefs, event["topic"]):
        return False, "topic muted", trace
    if freshness["state"] == "stale":
        return False, "stale event", trace
    if quiet["affected"]:
        return False, "quiet hours", trace
    if cooldown["active"]:
        if _meaningful_development(event, base_threshold, effective_score, config):
            cooldown["overridden_for_development"] = True
            trace["cooldown"] = cooldown
        else:
            return False, "topic or event cooldown", trace
    if frequency["active"]:
        return False, "rolling notification ceiling", trace
    if cooling["active"] and not cooling["overridden_for_development"]:
        return False, "temporary topic cooling", trace
    return True, "relevant, fresh, and above strict threshold", trace


def notification_decision(event: dict, prefs: dict, now: datetime | None = None, config: dict | None = None, conn=None) -> tuple[bool, str]:
    allowed, reason, _trace = evaluate_notification(event, prefs, now, config, conn)
    return allowed, reason


def should_notify(event: dict, prefs: dict, now: datetime | None = None, config: dict | None = None, conn=None) -> tuple[bool, str]:
    return notification_decision(event, prefs, now, config, conn)
