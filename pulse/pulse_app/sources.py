from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
import requests
from xml.etree import ElementTree

from .config import TOPIC_LABELS
from .rules import annotate_candidate, canonical_url, clean_text, score_item
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
            "source_trust": source.get("trust", "reliable_secondary"),
            "confidence": "confirmed" if source.get("trust") == "primary" else "likely",
            "verification": "direct_watcher",
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
        candidates.append(_source_item(source, {"title": release.get("name") or release.get("tag_name") or "GitHub release", "summary": clean_text(release.get("body", ""), 700), "url": release.get("html_url", ""), "guid": key, "published_at": release.get("published_at")}, initial_suppress=initializing, score_override=88))
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
    rain_rows = [row for row in rows if row["probability"] >= config["WEATHER_RAIN_PROBABILITY"] or row["amount"] >= config["WEATHER_RAIN_MM"] or row["code"] in rain_codes]
    if rain_rows and all(row["code"] >= 95 for row in rain_rows):
        rain_rows = []
    hot_rows = [row for row in rows if (row["temperature"] is not None and row["temperature"] >= config["WEATHER_HOT_C"]) or (row["apparent"] is not None and row["apparent"] >= config["WEATHER_HOT_C"] + 2)]
    cold_rows = [row for row in rows if (row["temperature"] is not None and row["temperature"] <= config["WEATHER_COLD_C"]) or (row["apparent"] is not None and row["apparent"] <= config["WEATHER_COLD_C"] - 2)]
    wind_rows = [row for row in rows if row["wind"] >= config["WEATHER_WIND_KMH"]]
    fog_rows = [row for row in rows if row["code"] in {45, 48}]
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
            "weather_category": category,
            "alert_window": window,
            "lookahead_hours": config["WEATHER_LOOKAHEAD_HOURS"],
            "peak_precipitation_probability": peak_probability,
            "peak_precipitation_mm": round(peak_amount, 1),
            "peak_wind_kmh": round(peak_wind, 1),
        })
        candidates.append(candidate)
    return candidates


def collect_candidates(conn, config: dict) -> list[dict]:
    candidates = []
    try:
        candidates.extend(weather_candidates(conn, config))
    except Exception as exc:
        LOGGER.warning("weather source failed: %s", exc)
    for source in configured_sources(config):
        try:
            if source["kind"] == "rss":
                candidates.extend(_rss_candidates(conn, source))
            elif source["kind"] == "url":
                candidates.extend(_url_candidates(conn, source))
            elif source["kind"] == "github":
                candidates.extend(_github_candidates(conn, source, config))
        except Exception as exc:
            LOGGER.warning("source %s failed: %s", source.get("id"), exc)
    return [annotate_candidate(candidate, config.get("TRACKED_ENTITIES", [])) for candidate in candidates]
