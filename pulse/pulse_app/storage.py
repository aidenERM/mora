from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_SEARCH_PROFILES, DEFAULT_THRESHOLDS, DEFAULT_TRACKED_ENTITIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_state (
    source_id TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    endpoint TEXT UNIQUE NOT NULL,
    payload TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    canonical_key TEXT UNIQUE NOT NULL,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    score INTEGER NOT NULL,
    priority TEXT NOT NULL,
    relevant INTEGER NOT NULL DEFAULT 0,
    suppress_notification INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT,
    reminded_at TEXT,
    remind_at TEXT,
    clicked_at TEXT,
    created_from TEXT NOT NULL DEFAULT 'source',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_recent ON events(discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_pending ON events(notified_at, suppress_notification, score);
CREATE TABLE IF NOT EXISTS event_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_actions_event ON event_actions(event_id, created_at DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path, initial_password_hash: str = "") -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "metadata" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('preferences',?)", (json.dumps({}),))
        if initial_password_hash:
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('password_hash',?)", (initial_password_hash,))
        conn.commit()
    finally:
        conn.close()


def default_preferences(config: dict) -> dict:
    return {
        "quiet_start": config["QUIET_START"],
        "quiet_end": config["QUIET_END"],
        "timezone": config["TIMEZONE"],
        "quiet_bypass_priority": config["QUIET_BYPASS_PRIORITY"],
        "topic_thresholds": {**DEFAULT_THRESHOLDS, **config["TOPIC_THRESHOLDS"]},
        "muted_topics": {},
    }


def passive_topic_weights(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT events.topic,
                  SUM(CASE WHEN event_actions.action='delivered' THEN 1 ELSE 0 END) AS delivered,
                  SUM(CASE WHEN event_actions.action='opened' THEN 1 ELSE 0 END) AS opened
           FROM event_actions JOIN events ON events.id=event_actions.event_id
           WHERE event_actions.action IN ('delivered','opened')
           GROUP BY events.topic"""
    ).fetchall()
    weights = {}
    for row in rows:
        delivered = int(row["delivered"] or 0)
        opened = int(row["opened"] or 0)
        if delivered < 3:
            continue
        ratio = opened / delivered
        weights[row["topic"]] = max(-6, min(6, round((ratio - 0.5) * 12)))
    return weights


def get_preferences(conn: sqlite3.Connection, config: dict) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key='preferences'").fetchone()
    stored = {}
    if row:
        try:
            stored = json.loads(row[0])
        except json.JSONDecodeError:
            stored = {}
    base = default_preferences(config)
    base.update({key: value for key, value in stored.items() if key in base})
    base["topic_thresholds"] = {**base["topic_thresholds"], **stored.get("topic_thresholds", {})}
    base["muted_topics"] = stored.get("muted_topics", {})
    for key in ("followed_entities", "less_like_entities", "less_like_topics"):
        base[key] = stored.get(key, {}) if isinstance(stored.get(key, {}), dict) else {}
    stored_weights = stored.get("learned_topic_weights", {}) if isinstance(stored.get("learned_topic_weights", {}), dict) else {}
    base["learned_topic_weights"] = {**passive_topic_weights(conn), **stored_weights}
    return base


def save_preferences(conn: sqlite3.Connection, preferences: dict) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(preferences, separators=(",", ":")),),
    )
    conn.commit()


def get_json_setting(conn: sqlite3.Connection, key: str, default):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return default


def save_json_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, separators=(",", ":"))),
    )
    conn.commit()


def runtime_config(conn: sqlite3.Connection, config: dict) -> dict:
    """Overlay settings editable in Pulse on top of environment configuration."""
    result = dict(config)
    profiles = get_json_setting(conn, "search_profiles", config.get("SEARCH_PROFILES", DEFAULT_SEARCH_PROFILES))
    entities = get_json_setting(conn, "tracked_entities", config.get("TRACKED_ENTITIES", DEFAULT_TRACKED_ENTITIES))
    result["SEARCH_PROFILES"] = profiles if isinstance(profiles, list) else config.get("SEARCH_PROFILES", DEFAULT_SEARCH_PROFILES)
    result["TRACKED_ENTITIES"] = entities if isinstance(entities, list) else config.get("TRACKED_ENTITIES", DEFAULT_TRACKED_ENTITIES)
    return result


def get_password_hash(conn: sqlite3.Connection, fallback: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    return str(row[0]) if row and row[0] else fallback


def save_password_hash(conn: sqlite3.Connection, password_hash: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (password_hash,),
    )
    conn.commit()


def record_event_action(conn: sqlite3.Connection, event_id: str, action: str, value: str = "") -> None:
    conn.execute(
        "INSERT INTO event_actions(event_id,action,value,created_at) VALUES(?,?,?,?)",
        (event_id, action[:60], value[:500], utc_now()),
    )


def event_action_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT action,COUNT(*) AS count FROM event_actions GROUP BY action ORDER BY action"
    ).fetchall()
    return {row["action"]: row["count"] for row in rows}


def learning_metrics(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT event_id,action,created_at FROM event_actions WHERE action IN ('delivered','opened') ORDER BY created_at"
    ).fetchall()
    deliveries = {}
    opens = {}
    for row in rows:
        target = deliveries if row["action"] == "delivered" else opens
        target.setdefault(row["event_id"], row["created_at"])
    times = []
    for event_id, opened_at in opens.items():
        delivered_at = deliveries.get(event_id)
        if not delivered_at:
            continue
        try:
            seconds = (datetime.fromisoformat(opened_at) - datetime.fromisoformat(delivered_at)).total_seconds()
        except ValueError:
            continue
        if seconds >= 0:
            times.append(seconds)
    return {
        "measured_opens": len(times),
        "average_time_to_open_seconds": round(sum(times) / len(times)) if times else None,
        "delivered_not_opened": max(0, len(deliveries) - len(times)),
    }


def clear_learning(conn: sqlite3.Connection) -> None:
    preferences = get_json_setting(conn, "preferences", {})
    if not isinstance(preferences, dict):
        preferences = {}
    for key in ("followed_entities", "less_like_entities", "less_like_topics", "learned_topic_weights"):
        preferences.pop(key, None)
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(preferences, separators=(",", ":")),),
    )
    conn.execute("DELETE FROM event_actions WHERE action IN ('follow', 'less_like', 'opened', 'source_clicked', 'delivered')")
    conn.commit()


def subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]


def save_subscription(conn: sqlite3.Connection, subscription: dict, user_agent: str = "") -> str:
    endpoint = str(subscription.get("endpoint", ""))
    sub_id = subscription_id(endpoint)
    now = utc_now()
    conn.execute(
        """INSERT INTO subscriptions(id,endpoint,payload,user_agent,created_at,last_seen_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET payload=excluded.payload,user_agent=excluded.user_agent,last_seen_at=excluded.last_seen_at""",
        (sub_id, endpoint, json.dumps(subscription, separators=(",", ":")), user_agent[:300], now, now),
    )
    conn.commit()
    return sub_id


def delete_subscription(conn: sqlite3.Connection, sub_id: str) -> None:
    conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    conn.commit()


def list_subscriptions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM subscriptions ORDER BY created_at").fetchall()]


def remove_subscriptions(conn: sqlite3.Connection, ids: list[str]) -> None:
    if ids:
        conn.executemany("DELETE FROM subscriptions WHERE id=?", [(item,) for item in ids])
        conn.commit()


def get_source_state(conn: sqlite3.Connection, source_id: str) -> dict:
    row = conn.execute("SELECT value FROM source_state WHERE source_id=?", (source_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}


def save_source_state(conn: sqlite3.Connection, source_id: str, value: dict) -> None:
    conn.execute(
        """INSERT INTO source_state(source_id,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (source_id, json.dumps(value, separators=(",", ":")), utc_now()),
    )


def event_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("relevant", "suppress_notification"):
        item[key] = bool(item[key])
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    return item


def get_event(conn: sqlite3.Connection, event_id: str) -> dict | None:
    return event_from_row(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


def list_events(conn: sqlite3.Connection, limit: int = 30, topic: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    if topic:
        rows = conn.execute("SELECT * FROM events WHERE topic=? ORDER BY discovered_at DESC LIMIT ?", (topic, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY discovered_at DESC LIMIT ?", (limit,)).fetchall()
    return [event_from_row(row) for row in rows]


def pending_events(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM events
           WHERE notified_at IS NULL AND suppress_notification=0 AND relevant=1
           ORDER BY score DESC, discovered_at ASC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [event_from_row(row) for row in rows]


def upsert_event(conn: sqlite3.Connection, event: dict) -> tuple[dict, bool]:
    now = utc_now()
    event = {**event, "discovered_at": event.get("discovered_at") or now, "last_seen_at": now}
    existing = conn.execute("SELECT * FROM events WHERE canonical_key=?", (event["canonical_key"],)).fetchone()
    if existing:
        conn.execute(
            """UPDATE events SET title=?,summary=?,body=?,url=?,published_at=?,last_seen_at=?,score=?,priority=?,relevant=?,suppress_notification=?,metadata=?
               WHERE canonical_key=?""",
            (
                event["title"], event["summary"], event["body"], event["url"], event.get("published_at"),
                now, event["score"], event["priority"], int(event["relevant"]), int(event.get("suppress_notification", False)), json.dumps(event.get("metadata") or {}, separators=(",", ":")), event["canonical_key"],
            ),
        )
        return event_from_row(conn.execute("SELECT * FROM events WHERE canonical_key=?", (event["canonical_key"],)).fetchone()), False
    conn.execute(
        """INSERT INTO events(id,source_id,source_kind,topic,title,summary,body,url,canonical_key,published_at,discovered_at,last_seen_at,score,priority,relevant,suppress_notification,created_from,metadata)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["id"], event["source_id"], event["source_kind"], event["topic"], event["title"],
            event.get("summary", ""), event.get("body", ""), event["url"], event["canonical_key"],
            event.get("published_at"), event["discovered_at"], now, event["score"], event["priority"],
            int(event["relevant"]), int(event.get("suppress_notification", False)), event.get("created_from", "source"), json.dumps(event.get("metadata") or {}, separators=(",", ":")),
        ),
    )
    return event_from_row(conn.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()), True


def mark_notified(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("UPDATE events SET notified_at=? WHERE id=?", (utc_now(), event_id))


def mark_clicked(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("UPDATE events SET clicked_at=? WHERE id=?", (utc_now(), event_id))
    record_event_action(conn, event_id, "opened")


def set_reminder(conn: sqlite3.Connection, event_id: str, minutes: int) -> dict | None:
    remind_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE events SET remind_at=?,reminded_at=NULL WHERE id=?", (remind_at, event_id))
    conn.commit()
    return get_event(conn, event_id)


def clear_reminder(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("UPDATE events SET remind_at=NULL,reminded_at=NULL WHERE id=?", (event_id,))
    conn.commit()


def due_reminders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE remind_at IS NOT NULL AND reminded_at IS NULL AND remind_at<=? ORDER BY remind_at LIMIT 20",
        (utc_now(),),
    ).fetchall()
    return [event_from_row(row) for row in rows]


def mark_reminded(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("UPDATE events SET reminded_at=? WHERE id=?", (utc_now(), event_id))


def create_manual_event(conn: sqlite3.Connection, app_url: str, title: str, summary: str) -> dict:
    now = utc_now()
    event_id = "test-" + hashlib.sha256((now + title).encode()).hexdigest()[:16]
    event = {
        "id": event_id,
        "source_id": "pulse-test",
        "source_kind": "manual",
        "topic": "system",
        "title": title,
        "summary": summary,
        "body": summary,
        "url": app_url + "/event/" + event_id,
        "canonical_key": "pulse-test:" + event_id,
        "published_at": now,
        "discovered_at": now,
        "score": 100,
        "priority": "critical",
        "relevant": True,
        "created_from": "manual",
    }
    created, _ = upsert_event(conn, event)
    conn.commit()
    return created
