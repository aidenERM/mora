from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
import requests
from xml.etree import ElementTree

from .config import TOPIC_LABELS
from .rules import annotate_candidate, canonical_url, clean_text, normalize, score_item
from .storage import get_source_state, save_source_state

LOGGER = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0"

DEFAULT_SOURCES = [
    {
        "id": "apple-newsroom",
        "kind": "rss",
        "label": "Apple Newsroom",
        "url": "https://www.apple.com/newsroom/rss-feed.rss",
        "topic": "apple",
        "trust": "primary",
        "keywords": ["iphone", "ios", "ipad", "mac", "macbook", "apple watch", "airpods", "vision pro", "apple intelligence"],
    },
    {
        "id": "call-of-duty-blog",
        "kind": "url",
        "label": "Call of Duty Blog",
        "url": "https://www.callofduty.com/blog/warzone",
        "topic": "warzone",
        "trust": "primary",
        "keywords": ["warzone", "patch notes", "season", "weapon", "balance", "ricochet", "loadout", "battle royale", "verdansk"],
    },
    {
        "id": "github-status",
        "kind": "rss",
        "label": "GitHub Status",
        "url": "https://www.githubstatus.com/history.rss",
        "topic": "service_status",
        "trust": "primary",
        "keywords": ["github", "incident", "outage", "degraded", "latency", "operational", "resolved"],
        "metadata": {"service": "GitHub"},
    },
    {
        "id": "openai-status",
        "kind": "rss",
        "label": "OpenAI Status",
        "url": "https://status.openai.com/history.rss",
        "topic": "service_status",
        "trust": "primary",
        "keywords": ["openai", "chatgpt", "codex", "incident", "outage", "elevated errors", "degraded", "resolved"],
        "metadata": {"service": "OpenAI"},
    },
    {
        "id": "discord-status",
        "kind": "rss",
        "label": "Discord Status",
        "url": "https://discord.statuspage.io/history.rss",
        "topic": "service_status",
        "trust": "primary",
        "keywords": ["discord", "incident", "outage", "degraded", "latency", "operational", "resolved"],
        "metadata": {"service": "Discord"},
    },
    {
        "id": "discord-blog",
        "kind": "url",
        "label": "Discord Blog",
        "url": "https://discord.com/blog",
        "topic": "discord",
        "trust": "primary",
        "keywords": ["discord", "security", "outage", "announcement", "patch", "release"],
    },
    {
        "id": "openai-news-rss",
        "kind": "rss",
        "label": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
        "topic": "openai",
        "trust": "primary",
        "keywords": ["chatgpt", "openai", "release", "model", "security", "availability", "announcement"],
    },
    {
        "id": "discord-blog-rss",
        "kind": "rss",
        "label": "Discord Blog",
        "url": "https://discord.com/blog/rss.xml",
        "topic": "discord",
        "trust": "primary",
        "keywords": ["discord", "security", "outage", "announcement", "patch", "release"],
    },
    {
        "id": "rocket-league-news-search",
        "kind": "rss",
        "label": "Rocket League major news",
        "url": "https://news.google.com/rss/search?q=Rocket+League+patch+notes+OR+season+OR+RLCS&hl=en-US&gl=US&ceid=US:en",
        "topic": "rocket_league",
        "trust": "reliable_secondary",
        "keywords": ["rocket league", "patch", "season", "rlcs", "maintenance", "update"],
    },
    {
        "id": "instagram-major-news-search",
        "kind": "rss",
        "label": "Instagram major news",
        "url": "https://news.google.com/rss/search?q=Instagram+major+announcement+OR+security+OR+outage&hl=en-US&gl=US&ceid=US:en",
        "topic": "instagram",
        "trust": "reliable_secondary",
        "keywords": ["instagram", "major", "announcement", "security", "outage", "update"],
    },
    {
        "id": "unstable-smp-news-search",
        "kind": "rss",
        "label": "Unstable SMP / Universe",
        "url": "https://news.google.com/rss/search?q=Unstable+SMP+OR+Unstable+Universe&hl=en-US&gl=US&ceid=US:en",
        "topic": "unstable_smp",
        "trust": "reliable_secondary",
        "keywords": ["unstable smp", "unstable universe", "update", "season", "announcement"],
    },
]


def _request(url: str, headers: dict | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        response = requests.get(url, headers=request_headers, timeout=25)
        response.raise_for_status()
        return response.content[:2_000_000]
    except requests.RequestException:
        # Some public sites close Python HTTP clients but answer a normal curl request.
        # Keep this fallback bounded and only use it for configured source reads.
        curl = "curl.exe" if os.name == "nt" else "curl"
        command = [curl, "-L", "--fail", "--silent", "--show-error", "--max-time", "25", "--http1.1", "-A", USER_AGENT, url]
        result = subprocess.run(command, check=True, capture_output=True, timeout=30)
        return result.stdout[:2_000_000]


def _text(element) -> str:
    return clean_text(" ".join(element.itertext()) if element is not None else "", 1000)


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first(element, names: set[str]):
    if element is None:
        return None
    for child in element.iter():
        if _tag_name(child.tag) in names:
            return child
    return None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except (TypeError, ValueError):
            return None


def parse_feed(raw: bytes) -> list[dict]:
    root = ElementTree.fromstring(raw)
    result = []
    for item in root.iter():
        if _tag_name(item.tag) not in {"item", "entry"}:
            continue
        title = _text(_first(item, {"title"}))
        summary = _text(_first(item, {"description", "summary", "content", "encoded"}))
        link_element = _first(item, {"link"})
        link = (link_element.attrib.get("href", "") if link_element is not None else "") or _text(link_element)
        guid = _text(_first(item, {"guid", "id"}))
        published = _text(_first(item, {"pubdate", "published", "updated", "date"}))
        if title and link:
            result.append({"title": html.unescape(title), "summary": html.unescape(summary), "url": canonical_url(html.unescape(link)), "guid": guid, "published_at": _parse_date(published)})
    return result[:30]


def _html_snapshot(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    description_match = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']', text, re.I | re.S)
    title = clean_text(html.unescape(title_match.group(1) if title_match else ""), 200)
    description = clean_text(html.unescape(description_match.group(1) if description_match else ""), 600)
    signal = clean_text(re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S), 8000)
    digest = hashlib.sha256(signal.encode("utf-8")).hexdigest()
    return {"title": title or "Updated source", "summary": description or clean_text(signal, 400), "digest": digest}


def configured_sources(config: dict) -> list[dict]:
    sources = [*DEFAULT_SOURCES]
    if config.get("COLOMBIA_RSS_URL"):
        sources.append({
            "id": "colombia-news",
            "kind": "rss",
            "label": "Colombia news",
            "url": config["COLOMBIA_RSS_URL"],
            "topic": "colombia",
            "trust": "reliable_secondary",
            "keywords": ["colombia", "gobierno", "emergencia", "alerta", "seguridad", "transporte", "temblor", "cierre", "elecciones", "banco de la república", "decreto"],
        })
    for source in config.get("RSS_SOURCES", []):
        sources.append({"kind": "rss", "trust": "reliable_secondary", **source})
    for source in config.get("URL_WATCHERS", []):
        sources.append({"kind": "url", "trust": "reliable_secondary", **source})
    for source in config.get("PACKAGE_WATCHERS", []):
        sources.append({"kind": "package", "trust": "reliable_secondary", "topic": "package", **source})
    for repo in config.get("GITHUB_REPOS", []):
        source_id = "github:" + repo.lower()
        sources.append({"id": source_id, "kind": "github", "label": repo, "repo": repo, "topic": "github", "trust": "primary", "keywords": [], "always_relevant": True})
    return [source for source in sources if source.get("id") and source.get("url") or source.get("kind") == "github"]


def _source_item(source: dict, item: dict, initial_suppress: bool = False, score_override: int | None = None) -> dict:
    title = clean_text(item.get("title", "Untitled"), 240)
    summary = clean_text(item.get("summary", ""), 700)
    score, relevant, reason = score_item(source["topic"], title, summary, source.get("keywords", []), bool(source.get("always_relevant")), int(source.get("boost", 0)))
    if score_override is not None:
        score, relevant, reason = score_override, True, "weather rule matched" if source.get("kind") == "weather" else "watched directly"
    identity = item.get("guid") or item.get("url") or (title + (item.get("published_at") or ""))
    canonical_key = source["id"] + ":" + hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:24]
    event_id = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:20]
    return {
        "id": event_id,
        "source_id": source["id"],
        "source_kind": source["kind"],
        "topic": source["topic"],
        "title": title,
        "summary": summary,
        "body": reason,
        "url": item.get("url") or source.get("url") or "https://pulse.moralife.uk/",
        "canonical_key": canonical_key,
        "published_at": item.get("published_at"),
        "score": score,
        "relevant": relevant,
        "priority": "critical" if score >= 90 else "high" if score >= 80 else "normal" if score >= 65 else "low",
        "suppress_notification": initial_suppress,
        "metadata": {
            "source_label": source.get("label", source["id"]),
            "source_trust": source.get("trust", "reliable_secondary"),
            "confidence": "confirmed" if source.get("trust") == "primary" else "likely",
            "verification": "direct_watcher",
            "source_metadata": source.get("metadata", {}),
            "sources": [{"url": item.get("url") or source.get("url"), "title": title, "trust": source.get("trust", "reliable_secondary")}],
        },
    }


def _rss_candidates(conn, source: dict) -> list[dict]:
    items = parse_feed(_request(source["url"], {"Accept": "application/rss+xml, application/atom+xml, application/xml"}))
    state = get_source_state(conn, source["id"])
    seen = set(state.get("seen", []))
    initializing = not bool(state.get("initialized"))
    candidates = []
    keys = []
    for item in items:
        key = item.get("guid") or item.get("url") or item.get("title")
        keys.append(str(key))
        if key in seen and not initializing:
            continue
        candidates.append(_source_item(source, item, initial_suppress=initializing))
    save_source_state(conn, source["id"], {"initialized": True, "seen": keys[:100]})
    return candidates


def _url_candidates(conn, source: dict) -> list[dict]:
    snapshot = _html_snapshot(_request(source["url"], {"Accept": "text/html,application/xhtml+xml"}))
    state = get_source_state(conn, source["id"])
    changed = snapshot["digest"] != state.get("digest")
    initializing = not bool(state.get("initialized"))
    save_source_state(conn, source["id"], {"initialized": True, "digest": snapshot["digest"]})
    if not changed and not initializing:
        return []
    return [_source_item(source, {"title": snapshot["title"], "summary": snapshot["summary"], "url": source["url"]}, initial_suppress=initializing)]


def _package_status(snapshot: dict) -> str:
    text = normalize(snapshot.get("title", "") + " " + snapshot.get("summary", ""))
    if re.search(r"delayed|delay|exception|returned|return to sender|problema|retras", text):
        return "exception"
    if re.search(r"out for delivery|out for shipment|en reparto|entrega hoy", text):
        return "out_for_delivery"
    if re.search(r"delivered|entregado|entregada", text):
        return "delivered"
    if re.search(r"shipped|in transit|transit|en camino|despach", text):
        return "in_transit"
    if re.search(r"label created|pre-shipment|created", text):
        return "label_created"
    return "unknown"


def _package_candidates(conn, source: dict) -> list[dict]:
    snapshot = _html_snapshot(_request(source["url"], {"Accept": "text/html,application/xhtml+xml"}))
    package_id = str(source.get("package_id") or source["id"])
    status = _package_status(snapshot)
    state = get_source_state(conn, source["id"])
    initializing = not bool(state.get("initialized"))
    changed = status != state.get("status") or snapshot["digest"] != state.get("digest")
    save_source_state(conn, source["id"], {"initialized": True, "status": status, "digest": snapshot["digest"]})
    if not changed and not initializing:
        return []
    if status == "unknown" and not initializing:
        return []
    score = {"exception": 96, "out_for_delivery": 90, "delivered": 86, "in_transit": 72, "label_created": 45, "unknown": 20}[status]
    title = f"Package update: {status.replace('_', ' ')}"
    candidate = _source_item(
        {"id": source["id"], "kind": "package", "label": source.get("label", package_id), "topic": "package", "trust": source.get("trust", "reliable_secondary"), "keywords": []},
        {"title": title, "summary": snapshot["summary"], "url": source["url"], "guid": package_id},
        initial_suppress=initializing,
        score_override=score,
    )
    candidate["canonical_key"] = source["id"] + ":" + hashlib.sha256(package_id.encode("utf-8")).hexdigest()[:24]
    candidate["id"] = hashlib.sha256(candidate["canonical_key"].encode("utf-8")).hexdigest()[:20]
    candidate["body"] = f"package state changed to {status.replace('_', ' ')}"
    candidate["metadata"].update({"package_id": package_id, "package_status": status, "package_label": source.get("label", package_id)})
    return [candidate]


def _github_candidates(conn, source: dict, config: dict) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json"}
    if config.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + config["GITHUB_TOKEN"]
    url = "https://api.github.com/repos/" + source["repo"] + "/releases?per_page=5"
    response = requests.get(url, headers={"User-Agent": USER_AGENT, **headers}, timeout=25)
    response.raise_for_status()
    releases = response.json()
    state = get_source_state(conn, source["id"])
    seen = set(state.get("seen", []))
    initializing = not bool(state.get("initialized"))
    candidates = []
    keys = []
    for release in releases:
        key = str(release.get("id") or release.get("html_url"))
        keys.append(key)
        if key in seen and not initializing:
            continue
        title = release.get("name") or release.get("tag_name") or "GitHub release"
        summary = clean_text(release.get("body", ""), 700)
        text = (str(title) + " " + summary).casefold()
        major = bool(re.search(r"\b(v?\d+\.0(?:\.0)?|major|breaking|security|critical|deprecated)\b", text))
        release_score = 92 if major else 78
        candidates.append(_source_item(source, {"title": title, "summary": summary, "url": release.get("html_url", ""), "guid": key, "published_at": release.get("published_at")}, initial_suppress=initializing, score_override=release_score))
    save_source_state(conn, source["id"], {"initialized": True, "seen": keys[:100]})
    return candidates


def github_webhook_candidate(payload: dict, event_type: str, config: dict) -> dict | None:
    """Turn a validated GitHub delivery into the same event shape as polling."""
    repository = payload.get("repository") or {}
    repo = str(repository.get("full_name") or "").strip()
    if not repo or (config.get("GITHUB_REPOS") and repo.casefold() not in {item.casefold() for item in config["GITHUB_REPOS"]}):
        return None
    action = str(payload.get("action") or "")
    source = {
        # Keep release keys compatible with the 15-minute release fallback.
        "id": "github:" + repo.lower(),
        "kind": "github_webhook",
        "label": repo,
        "topic": "github",
        "trust": "primary",
        "keywords": [],
        "always_relevant": True,
    }
    repository_url = repository.get("html_url") or "https://github.com/" + repo
    item = None
    if event_type == "release" and action in {"published", "created", "released"}:
        release = payload.get("release") or {}
        release_id = release.get("id") or release.get("tag_name") or release.get("html_url")
        item = {
            "title": f"{repo} release: {release.get('name') or release.get('tag_name') or 'new release'}",
            "summary": clean_text(release.get("body", ""), 700),
            "url": release.get("html_url") or repository_url,
            "guid": str(release_id),
            "published_at": release.get("published_at") or release.get("created_at"),
        }
    elif event_type == "push":
        commits = payload.get("commits") or []
        if not commits:
            return None
        messages = [clean_text(commit.get("message", "").splitlines()[0], 180) for commit in commits if commit.get("message")]
        item = {
            "title": f"{repo} push: {len(commits)} commit" + ("s" if len(commits) != 1 else ""),
            "summary": clean_text("; ".join(messages), 700) or "new repository activity",
            "url": payload.get("compare") or repository_url,
            "guid": f"push:{payload.get('after') or payload.get('head_commit', {}).get('id') or payload.get('ref')}",
            "published_at": None,
        }
    elif event_type in {"issues", "pull_request"} and action in {"opened", "reopened", "closed", "ready_for_review"}:
        item_data = payload.get("issue") or payload.get("pull_request") or {}
        kind = "pull request" if event_type == "pull_request" else "issue"
        number = item_data.get("number") or payload.get("number")
        item = {
            "title": f"{repo} {kind} {number}: {item_data.get('title') or action}",
            "summary": clean_text(item_data.get("body", ""), 700) or f"{kind} {action}",
            "url": item_data.get("html_url") or repository_url,
            "guid": f"{kind}:{number}:{action}",
            "published_at": item_data.get("updated_at"),
        }
    if not item:
        return None
    candidate = _source_item(source, item)
    candidate["metadata"].update({
        "source_trust": "primary",
        "confidence": "confirmed",
        "verification": "github_webhook",
        "sources": [{"url": item["url"], "title": candidate["title"], "trust": "primary"}],
    })
    return annotate_candidate(candidate, config.get("TRACKED_ENTITIES", []))


def weather_candidates(conn, config: dict) -> list[dict]:
    params = urlencode({
        "latitude": config["WEATHER_LAT"],
        "longitude": config["WEATHER_LON"],
        "timezone": "auto",
        "forecast_days": 2,
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m",
    })
    raw = json.loads(_request("https://api.open-meteo.com/v1/forecast?" + params).decode("utf-8"))
    hourly = raw.get("hourly", {})
    lookahead = config.get("WEATHER_LOOKAHEAD_HOURS", 24)
    times = (hourly.get("time") or [])[:lookahead]

    def series(name: str, default):
        values = list(hourly.get(name) or [])[:lookahead]
        return values + [default] * max(0, len(times) - len(values))

    temperatures = series("temperature_2m", None)
    apparent = series("apparent_temperature", None)
    probabilities = series("precipitation_probability", 0)
    amounts = series("precipitation", 0)
    codes = series("weather_code", 0)
    winds = series("wind_speed_10m", 0)
    rows = []
    for index, when in enumerate(times):
        try:
            rows.append({
                "when": str(when),
                "temperature": float(temperatures[index]) if temperatures[index] is not None else None,
                "apparent": float(apparent[index]) if apparent[index] is not None else None,
                "probability": int(probabilities[index] or 0),
                "amount": float(amounts[index] or 0),
                "code": int(codes[index] or 0),
                "wind": float(winds[index] or 0),
            })
        except (TypeError, ValueError):
            continue

    source = {"id": "weather:" + config["WEATHER_LABEL"].lower(), "kind": "weather", "label": config["WEATHER_LABEL"], "topic": "weather", "trust": "primary", "keywords": [], "always_relevant": True}
    state = get_source_state(conn, source["id"])
    initializing = not bool(state.get("initialized"))
    save_source_state(conn, source["id"], {"initialized": True})
    if not rows:
        return []

    rain_codes = set(range(51, 68)) | set(range(80, 83))
    storm_rows = [row for row in rows if row["code"] >= 95]
    rain_rows = [row for row in rows if (row["probability"] >= config["WEATHER_RAIN_PROBABILITY"] and row["amount"] >= 2) or row["amount"] >= config["WEATHER_RAIN_MM"]]
    if storm_rows:
        rain_rows = []
    hot_rows = [row for row in rows if (row["temperature"] is not None and row["temperature"] >= config["WEATHER_HOT_C"]) or (row["apparent"] is not None and row["apparent"] >= config["WEATHER_HOT_C"] + 2)]
    cold_rows = [row for row in rows if (row["temperature"] is not None and row["temperature"] <= config["WEATHER_COLD_C"]) or (row["apparent"] is not None and row["apparent"] <= config["WEATHER_COLD_C"] - 2)]
    wind_rows = [] if storm_rows else [row for row in rows if row["wind"] >= config["WEATHER_WIND_KMH"]]
    fog_rows = []
    alerts = {
        "storm": storm_rows,
        "rain": rain_rows,
        "hot": hot_rows,
        "cold": cold_rows,
        "wind": wind_rows,
        "fog": fog_rows,
    }
    titles = {
        "storm": "Thunderstorms possible",
        "rain": "Rain likely",
        "hot": "Hot weather expected",
        "cold": "Cold weather expected",
        "wind": "Strong wind possible",
        "fog": "Fog possible",
    }
    scores = {"storm": 96, "rain": 86, "hot": 82, "cold": 82, "wind": 86, "fog": 80}
    window = rows[0]["when"][:10]
    candidates = []
    for category, matching in alerts.items():
        if not matching:
            continue
        earliest = matching[0]
        peak_probability = max(row["probability"] for row in matching)
        peak_amount = max(row["amount"] for row in matching)
        peak_wind = max(row["wind"] for row in matching)
        temperatures_seen = [row["temperature"] for row in matching if row["temperature"] is not None]
        apparent_seen = [row["apparent"] for row in matching if row["apparent"] is not None]
        time_text = earliest["when"].replace("T", " ")
        if category == "storm":
            summary = f"Thunderstorms possible from {time_text}; {peak_probability}% precipitation probability, up to {peak_amount:g} mm, wind up to {peak_wind:g} km/h."
        elif category == "rain":
            summary = f"Rain or showers possible from {time_text}; {peak_probability}% precipitation probability, up to {peak_amount:g} mm, wind up to {peak_wind:g} km/h."
        elif category == "hot":
            summary = f"Air temperature may reach {max(temperatures_seen or [0]):g}°C and feel like {max(apparent_seen or temperatures_seen or [0]):g}°C around {time_text}."
        elif category == "cold":
            summary = f"Air temperature may fall to {min(temperatures_seen or [0]):g}°C and feel like {min(apparent_seen or temperatures_seen or [0]):g}°C around {time_text}."
        elif category == "wind":
            summary = f"Wind may reach {peak_wind:g} km/h from {time_text}; precipitation probability is {peak_probability}%."
        else:
            summary = f"Low visibility or fog is possible from {time_text}; wind up to {peak_wind:g} km/h."
        item = {
            "title": f"{titles[category]} near {config['WEATHER_LABEL']}",
            "summary": summary,
            "url": "https://open-meteo.com/en/docs",
            "guid": f"{window}:{category}",
            "published_at": None,
        }
        candidate = _source_item(source, item, initial_suppress=initializing, score_override=scores[category])
        candidate["body"] = f"forecast weather alert: {category}; checked {config['WEATHER_LOOKAHEAD_HOURS']}-hour outlook"
        candidate["metadata"].update({
            "weather_label": config["WEATHER_LABEL"],
            "weather_category": category,
            "alert_window": window,
            "earliest_forecast_at": matching[0]["when"],
            "latest_forecast_at": matching[-1]["when"],
            "alert_signature": "|".join([
                category, window, matching[0]["when"][:13], str(peak_probability // 10),
                str(int(peak_amount // 5)), str(int(peak_wind // 10)),
                str(int(max(temperatures_seen or [0]) // 2)), str(int(min(temperatures_seen or [0]) // 2)),
            ]),
            "safety_critical": category in {"storm", "wind"},
            "plan_impact": {
                "storm": "Outdoor plans or travel around La Ceja may be disrupted.",
                "rain": "Carry rain protection and expect outdoor plans around La Ceja to be affected.",
                "hot": "Heat may affect outdoor plans and comfort around La Ceja.",
                "cold": "The unusual cold may affect early or outdoor plans around La Ceja.",
                "wind": "Strong wind may affect travel or exposed outdoor plans around La Ceja.",
                "fog": "Reduced visibility may affect travel around La Ceja.",
            }.get(category, "It may affect plans around La Ceja."),
            "lookahead_hours": config["WEATHER_LOOKAHEAD_HOURS"],
            "peak_precipitation_probability": peak_probability,
            "peak_precipitation_mm": round(peak_amount, 1),
            "peak_wind_kmh": round(peak_wind, 1),
        })
        candidates.append(candidate)
    return candidates


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(lon2 - lon1)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return radius * 2 * math.asin(math.sqrt(min(1, value)))


def _earthquake_relevance(magnitude: float, distance: float, depth: float, config: dict) -> tuple[bool, int, str]:
    major = magnitude >= config["EARTHQUAKE_MAJOR_MAG"]
    if major:
        return True, 99, "major earthquake"
    if distance <= 50:
        minimum = config["EARTHQUAKE_LOCAL_MIN_MAG"]
    elif distance <= 100:
        minimum = max(config["EARTHQUAKE_LOCAL_MIN_MAG"], 5.0)
    elif distance <= 250:
        minimum = max(config["EARTHQUAKE_LOCAL_MIN_MAG"], 5.5)
    elif distance <= config["EARTHQUAKE_LOCAL_RADIUS_KM"]:
        minimum = max(config["EARTHQUAKE_LOCAL_MIN_MAG"], 6.0)
    else:
        return False, 15, "too distant for local impact"
    if depth > 120:
        minimum += 0.5
    if magnitude < minimum:
        return False, max(10, round(magnitude * 6)), "below local felt-impact threshold"
    score = min(97, 88 + round((magnitude - minimum) * 8) + (3 if depth <= 50 else 0))
    return True, score, "could be felt near La Ceja"


def earthquake_candidates(conn, config: dict) -> list[dict]:
    if not config.get("EARTHQUAKE_ENABLED", True):
        return []
    payload = json.loads(_request(config["EARTHQUAKE_FEED_URL"], {"Accept": "application/geo+json, application/json"}).decode("utf-8"))
    state = get_source_state(conn, "earthquakes:usgs")
    seen = set(state.get("seen", []))
    initializing = not bool(state.get("initialized"))
    keys = []
    candidates = []
    for feature in payload.get("features", [])[:200]:
        props = feature.get("properties") or {}
        event_key = str(feature.get("id") or props.get("code") or props.get("url") or "")
        if not event_key:
            continue
        keys.append(event_key)
        if event_key in seen and not initializing:
            continue
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) < 3 or props.get("mag") is None:
            continue
        try:
            magnitude = float(props["mag"])
            lon, lat, depth = float(coordinates[0]), float(coordinates[1]), float(coordinates[2])
        except (TypeError, ValueError):
            continue
        distance = _distance_km(config["WEATHER_LAT"], config["WEATHER_LON"], lat, lon)
        relevant, score, reason = _earthquake_relevance(magnitude, distance, depth, config)
        place = clean_text(props.get("place", "unknown location"), 180)
        title = f"M{magnitude:g} earthquake near La Ceja" if relevant else f"M{magnitude:g} earthquake detected"
        summary = f"{place}; about {distance:,.0f} km from La Ceja and {depth:g} km deep. {reason}."
        item = {
            "title": title,
            "summary": summary,
            "url": props.get("url") or "https://earthquake.usgs.gov/earthquakes/map/",
            "guid": event_key,
            "published_at": datetime.fromtimestamp(props["time"] / 1000, timezone.utc).replace(microsecond=0).isoformat() if props.get("time") else None,
        }
        source = {"id": "earthquakes:usgs", "kind": "earthquake", "label": "USGS earthquakes", "topic": "earthquake", "trust": "primary", "keywords": []}
        candidate = _source_item(source, item, initial_suppress=initializing, score_override=score)
        candidate["relevant"] = relevant
        candidate["metadata"].update({
            "earthquake_id": event_key,
            "location": "La Ceja, Antioquia",
            "magnitude": magnitude,
            "distance_km": round(distance, 1),
            "depth_km": round(depth, 1),
            "local_impact": reason,
            "safety_critical": magnitude >= config["EARTHQUAKE_MAJOR_MAG"],
        })
        candidates.append(candidate)
    save_source_state(conn, "earthquakes:usgs", {"initialized": True, "seen": keys[:200]})
    return candidates


def collect_candidates(conn, config: dict) -> list[dict]:
    candidates = []
    try:
        candidates.extend(weather_candidates(conn, config))
    except Exception as exc:
        LOGGER.warning("weather source failed: %s", exc)
    try:
        candidates.extend(earthquake_candidates(conn, config))
    except Exception as exc:
        LOGGER.warning("earthquake source failed: %s", exc)
    for source in configured_sources(config):
        try:
            if source["kind"] == "rss":
                candidates.extend(_rss_candidates(conn, source))
            elif source["kind"] == "url":
                candidates.extend(_url_candidates(conn, source))
            elif source["kind"] == "package":
                candidates.extend(_package_candidates(conn, source))
            elif source["kind"] == "github":
                candidates.extend(_github_candidates(conn, source, config))
        except Exception as exc:
            LOGGER.warning("source %s failed: %s", source.get("id"), exc)
    return [annotate_candidate(candidate, config.get("TRACKED_ENTITIES", [])) for candidate in candidates]
