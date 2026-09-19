from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .rules import canonical_url, clean_text, matched_entities, priority_for, score_item
from .storage import get_source_state, save_source_state

LOGGER = logging.getLogger("pulse.discovery")
SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "Pulse/0.1 (+https://pulse.moralife.uk)"

PRIMARY_DOMAINS = {
    "activision.com",
    "apple.com",
    "callofduty.com",
    "github.com",
    "gov.co",
    "medellin.gov.co",
    "openai.com",
}
SECONDARY_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "caracol.com.co",
    "dexerto.com",
    "elespectador.com",
    "elcolombiano.com",
    "eltiempo.com",
    "ign.com",
    "macrumors.com",
    "pcgamer.com",
    "reuters.com",
    "semana.com",
    "techcrunch.com",
    "theguardian.com",
    "theverge.com",
}
COMMUNITY_DOMAINS = {
    "discord.com",
    "reddit.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
}
STOPWORDS = {
    "about", "after", "and", "announcement", "breaking", "changes", "for", "from", "latest",
    "news", "notes", "official", "release", "the", "today", "update", "updates", "what", "with",
}


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _matches_domain(domain: str, known: set[str]) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in known)


def source_trust(url: str) -> str:
    domain = _domain(url)
    if _matches_domain(domain, PRIMARY_DOMAINS):
        return "primary"
    if _matches_domain(domain, SECONDARY_DOMAINS):
        return "reliable_secondary"
    if _matches_domain(domain, COMMUNITY_DOMAINS) or domain.startswith("forums."):
        return "community"
    return "community"


def _trust_rank(value: str) -> int:
    return {"primary": 3, "reliable_secondary": 2, "community": 1}.get(value, 0)


def _parse_page(raw: bytes) -> dict | None:
    text = raw.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    description_match = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
        text,
        re.I | re.S,
    )
    article_match = re.search(r"<article\b[^>]*>(.*?)</article>", text, re.I | re.S)
    body = article_match.group(1) if article_match else text
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>|<svg.*?</svg>", " ", body, flags=re.I | re.S)
    body = clean_text(html.unescape(re.sub(r"<[^>]+>", " ", body)), 6000)
    title = clean_text(html.unescape(title_match.group(1) if title_match else ""), 240)
    description = clean_text(html.unescape(description_match.group(1) if description_match else ""), 700)
    if len(body) < 80:
        return None
    published_at = None
    date_match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished|pubdate)["\'][^>]+content=["\'](.*?)["\']',
        text,
        re.I | re.S,
    ) or re.search(r'<time[^>]+datetime=["\'](.*?)["\']', text, re.I | re.S)
    if date_match:
        raw_date = html.unescape(date_match.group(1)).strip()
        try:
            published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            try:
                published_at = parsedate_to_datetime(raw_date).astimezone(timezone.utc).replace(microsecond=0).isoformat()
            except (TypeError, ValueError, OverflowError):
                published_at = None
    return {
        "title": title or "Updated source",
        "summary": description or body[:700],
        "content": body,
        "digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "published_at": published_at,
    }


def fetch_page(url: str) -> dict | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        return _parse_page(response.content[:2_000_000])
    except (requests.RequestException, UnicodeError) as exc:
        LOGGER.info("discovery page skipped url=%s reason=%s", url, type(exc).__name__)
        return None


class BraveSearchProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, count: int, freshness: str, search_lang: str = "en") -> list[dict]:
        response = requests.get(
            SEARCH_URL,
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key, "User-Agent": USER_AGENT},
            params={"q": query[:600], "count": count, "freshness": freshness, "country": "CO", "search_lang": search_lang},
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("web", {}).get("results", [])
        return [
            {
                "title": clean_text(item.get("title", ""), 240),
                "url": canonical_url(item.get("url", "")),
                "snippet": clean_text(item.get("description", ""), 700),
                "age": item.get("age"),
            }
            for item in results
            if item.get("title") and item.get("url")
        ]


def discovery_due(conn, config: dict, now: datetime | None = None) -> bool:
    if not config.get("BRAVE_SEARCH_API_KEY"):
        return False
    now = now or datetime.now(timezone.utc)
    value = get_source_state(conn, "discovery:global")
    last_run = value.get("last_run_at")
    if not last_run:
        return True
    try:
        previous = datetime.fromisoformat(last_run)
    except ValueError:
        return True
    return now - previous >= timedelta(minutes=config["DISCOVERY_INTERVAL_MINUTES"])


def _scheduled_jobs(conn, config: dict) -> tuple[list[dict], dict]:
    profiles = [item for item in config.get("SEARCH_PROFILES", []) if item.get("active", True) and item.get("queries")]
    state = get_source_state(conn, "discovery:global")
    if not profiles:
        return [], state
    cursor = int(state.get("profile_cursor", 0)) % len(profiles)
    query_cursors = state.get("query_cursors", {})
    jobs = []
    for offset in range(min(config["DISCOVERY_MAX_QUERIES"], len(profiles))):
        profile = profiles[(cursor + offset) % len(profiles)]
        queries = profile.get("queries", [])
        query_index = int(query_cursors.get(profile.get("id"), 0)) % len(queries)
        jobs.append({"profile": profile, "query": queries[query_index], "trigger": False})
        query_cursors[profile.get("id")] = query_index + 1
    state["profile_cursor"] = (cursor + len(jobs)) % len(profiles)
    state["query_cursors"] = query_cursors
    return jobs, state


def _trigger_jobs(config: dict, triggers: list[dict], state: dict, now: datetime) -> list[dict]:
    jobs = []
    triggered_at = state.setdefault("triggered_at", {})
    for candidate in triggers[:2]:
        title = clean_text(candidate.get("title", ""), 180)
        if not title:
            continue
        canonical_key = candidate.get("canonical_key") or candidate.get("id") or title
        previous = triggered_at.get(canonical_key)
        if previous:
            try:
                if now - datetime.fromisoformat(previous) < timedelta(minutes=60):
                    continue
            except ValueError:
                pass
        profile = {
            "id": "trigger-" + candidate.get("source_id", "source"),
            "label": candidate.get("title", "Watcher update"),
            "topic": candidate.get("topic", "watcher"),
            "keywords": [],
        }
        jobs.append({"profile": profile, "query": '"' + title.replace('"', "") + '"', "trigger": True})
        triggered_at[canonical_key] = now.replace(microsecond=0).isoformat()
    return jobs


def _cluster_key(topic: str, title: str, entities: list[dict]) -> str:
    tokens = [item for item in re.findall(r"[a-z0-9]{3,}", (title or "").casefold()) if item not in STOPWORDS]
    entity_tokens = [item.get("id", "") for item in entities]
    identity = topic + "|" + "|".join(sorted(set(tokens + entity_tokens))[:16])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _confidence(results: list[dict]) -> str:
    domains = {_domain(item["url"]) for item in results}
    trusts = {item["trust"] for item in results}
    if "primary" in trusts:
        return "confirmed"
    if len(domains) >= 2 and "reliable_secondary" in trusts:
        return "likely"
    return "rumor"


def _candidate(cluster: list[dict], profile: dict, query: str) -> dict:
    unique = []
    seen_urls = set()
    for item in cluster:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        unique.append(item)
    cluster = unique
    cluster.sort(key=lambda item: (_trust_rank(item["trust"]), len(item["page"].get("content", ""))), reverse=True)
    best = cluster[0]
    profile = best.get("profile") or profile
    query = best.get("query") or query
    entities = []
    seen_entities = set()
    for item in cluster:
        for entity in item["entities"]:
            if entity.get("id") not in seen_entities:
                entities.append(entity)
                seen_entities.add(entity.get("id"))
    confidence = _confidence(cluster)
    score, _, reason = score_item(profile.get("topic", "watcher"), best["page"]["title"], best["page"]["content"], profile.get("keywords", []), always_relevant=True)
    score += {"primary": 8, "reliable_secondary": 2, "community": -8}.get(best["trust"], 0)
    score += min(30, sum(max(0, int(entity.get("boost", 0))) for entity in entities))
    score += {"confirmed": 8, "likely": 3, "rumor": -18}[confidence]
    score = max(0, min(100, score))
    cluster_key = _cluster_key(profile.get("topic", "watcher"), best["page"]["title"], entities)
    source_rows = [{"url": item["url"], "title": item["page"]["title"], "trust": item["trust"], "domain": _domain(item["url"])} for item in cluster]
    metadata = {
        "source_trust": best["trust"],
        "confidence": confidence,
        "verification": "cross_source" if len({_domain(item["url"]) for item in cluster}) > 1 else "single_source",
        "sources": source_rows,
        "discovery_query": query,
        "profile_id": profile.get("id"),
        "entities": [entity.get("id") for entity in entities],
        "entity_names": [entity.get("name", entity.get("id", "")) for entity in entities],
    }
    matched = ", ".join(metadata["entity_names"][:4])
    body = f"{confidence} discovery result from {len(source_rows)} source(s); {reason}"
    if matched:
        body += "; tracked: " + matched
    event_key = "discovery:" + cluster_key
    return {
        "id": hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:20],
        "source_id": "discovery:" + str(profile.get("id", "watcher")),
        "source_kind": "search",
        "topic": profile.get("topic", "watcher"),
        "title": best["page"]["title"],
        "summary": best["page"]["summary"],
        "body": body,
        "url": best["url"],
        "canonical_key": event_key,
        "published_at": best["page"].get("published_at"),
        "score": score,
        "relevant": confidence != "rumor",
        "priority": priority_for(score),
        "suppress_notification": confidence == "rumor",
        "metadata": metadata,
    }


def collect_discovery_candidates(conn, config: dict, triggers: list[dict] | None = None) -> list[dict]:
    if not config.get("BRAVE_SEARCH_API_KEY"):
        return []
    now = datetime.now(timezone.utc)
    triggers = [item for item in (triggers or []) if item.get("score", 0) >= 80 and not item.get("suppress_notification") and item.get("source_kind") != "weather"]
    state = get_source_state(conn, "discovery:global")
    jobs = _trigger_jobs(config, triggers, state, now) if triggers and config.get("DISCOVERY_ON_WATCHER", True) else []
    if not jobs:
        if not discovery_due(conn, config, now):
            return []
        jobs, state = _scheduled_jobs(conn, config)
    if not jobs:
        return []
    provider = BraveSearchProvider(config["BRAVE_SEARCH_API_KEY"])
    clusters: dict[str, list[dict]] = {}
    for job in jobs:
        profile = job["profile"]
        language = "es" if profile.get("topic") == "colombia" else "en"
        try:
            results = provider.search(job["query"], config["DISCOVERY_MAX_RESULTS"], config["DISCOVERY_FRESHNESS"], language)
        except requests.RequestException as exc:
            LOGGER.warning("discovery search failed profile=%s reason=%s", profile.get("id"), type(exc).__name__)
            continue
        for result in results:
            page = fetch_page(result["url"])
            if not page:
                continue
            entities = matched_entities(page["title"] + " " + page["content"], config.get("TRACKED_ENTITIES", []))
            key = _cluster_key(profile.get("topic", "watcher"), page["title"], entities)
            clusters.setdefault(key, []).append({
                "url": result["url"],
                "trust": source_trust(result["url"]),
                "page": page,
                "entities": entities,
                "profile": profile,
                "query": job["query"],
            })
    state["last_run_at"] = now.replace(microsecond=0).isoformat()
    save_source_state(conn, "discovery:global", state)
    conn.commit()
    candidates = []
    for cluster in clusters.values():
        profile = cluster[0].get("profile", jobs[0]["profile"])
        query = cluster[0].get("query", jobs[0]["query"])
        candidates.append(_candidate(cluster, profile, query))
    return candidates
