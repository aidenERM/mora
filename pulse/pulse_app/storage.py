from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - optional until integrations are enabled
    Fernet = None

from .config import DEFAULT_SEARCH_PROFILES, DEFAULT_THRESHOLDS, DEFAULT_TRACKED_ENTITIES, PERSONAL_PRIORITY_CATEGORIES
from .rules import cluster_id_for, clean_event_summary, clean_event_title, event_fingerprint, events_similar, materially_changed, normalize

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
    metadata TEXT NOT NULL DEFAULT '{}',
    canonical_event_id TEXT NOT NULL DEFAULT '',
    cluster_id TEXT NOT NULL DEFAULT '',
    development_id TEXT NOT NULL DEFAULT '',
    normalized_title TEXT NOT NULL DEFAULT '',
    normalized_entities TEXT NOT NULL DEFAULT '[]',
    normalized_location TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '{}',
    decision_trace TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    notified_hash TEXT NOT NULL DEFAULT '',
    notification_pending INTEGER NOT NULL DEFAULT 0,
    notification_count INTEGER NOT NULL DEFAULT 0,
    last_notification_at TEXT,
    notification_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS integrations (
    provider TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    connection_state TEXT NOT NULL DEFAULT 'not_configured',
    authorization_state TEXT NOT NULL DEFAULT 'not_configured',
    last_success_at TEXT,
    last_attempted_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS integration_credentials (
    provider TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_signals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_context_active ON context_signals(kind, expires_at, observed_at DESC);
CREATE TABLE IF NOT EXISTS companion_devices (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pairing_challenges (
    id TEXT PRIMARY KEY,
    code_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    tracking_number TEXT NOT NULL,
    carrier TEXT NOT NULL DEFAULT '',
    merchant TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    estimated_delivery TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    status_history TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(tracking_number, merchant)
);
CREATE TABLE IF NOT EXISTS purchases (
    id TEXT PRIMARY KEY,
    external_key TEXT UNIQUE NOT NULL,
    merchant TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    amount REAL,
    currency TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'interested',
    order_id TEXT NOT NULL DEFAULT '',
    package_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_recent ON events(discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, discovered_at DESC);
CREATE TABLE IF NOT EXISTS canonical_events (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_entities TEXT NOT NULL DEFAULT '[]',
    normalized_location TEXT NOT NULL DEFAULT '',
    cluster_id TEXT NOT NULL,
    current_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_canonical_cluster ON canonical_events(cluster_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS story_clusters (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_entities TEXT NOT NULL DEFAULT '[]',
    normalized_location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_story_clusters_recent ON story_clusters(updated_at DESC);
CREATE TABLE IF NOT EXISTS event_observations (
    id TEXT PRIMARY KEY,
    canonical_event_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    source_trust TEXT NOT NULL DEFAULT '',
    observation_key TEXT UNIQUE NOT NULL,
    content_hash TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_observations_canonical ON event_observations(canonical_event_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS event_developments (
    id TEXT PRIMARY KEY,
    canonical_event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    meaningful INTEGER NOT NULL DEFAULT 1,
    source_observation_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(canonical_event_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_developments_canonical ON event_developments(canonical_event_id, discovered_at DESC);
CREATE TABLE IF NOT EXISTS event_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL DEFAULT '',
    cluster_id TEXT NOT NULL DEFAULT '',
    evaluated_at TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    score INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    tier TEXT NOT NULL,
    near_threshold INTEGER NOT NULL DEFAULT 0,
    distance_from_threshold INTEGER NOT NULL DEFAULT 0,
    later_became_important INTEGER NOT NULL DEFAULT 0,
    later_development_notified INTEGER NOT NULL DEFAULT 0,
    trace TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_decisions_recent ON event_decisions(evaluated_at DESC, near_threshold, allowed);
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


def _safe_json(value, fallback=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback if fallback is not None else {}


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def save_integration(conn: sqlite3.Connection, provider: str, label: str, **fields) -> None:
    existing = conn.execute("SELECT * FROM integrations WHERE provider=?", (provider,)).fetchone()
    values = {
        "enabled": int(fields.get("enabled", existing["enabled"] if existing else 0)),
        "connection_state": fields.get("connection_state", existing["connection_state"] if existing else "not_configured"),
        "authorization_state": fields.get("authorization_state", existing["authorization_state"] if existing else "not_configured"),
        "last_success_at": fields.get("last_success_at", existing["last_success_at"] if existing else None),
        "last_attempted_at": fields.get("last_attempted_at", existing["last_attempted_at"] if existing else None),
        "last_error": fields.get("last_error", existing["last_error"] if existing else ""),
        "metadata": json.dumps(fields.get("metadata", json.loads(existing["metadata"]) if existing else {}), separators=(",", ":")),
        "updated_at": utc_now(),
    }
    conn.execute(
        """INSERT INTO integrations(provider,label,enabled,connection_state,authorization_state,last_success_at,last_attempted_at,last_error,metadata,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(provider) DO UPDATE SET label=excluded.label,enabled=excluded.enabled,connection_state=excluded.connection_state,
             authorization_state=excluded.authorization_state,last_success_at=excluded.last_success_at,last_attempted_at=excluded.last_attempted_at,
             last_error=excluded.last_error,metadata=excluded.metadata,updated_at=excluded.updated_at""",
        (provider, label, values["enabled"], values["connection_state"], values["authorization_state"], values["last_success_at"], values["last_attempted_at"], values["last_error"], values["metadata"], values["updated_at"]),
    )


def _credential_cipher():
    key = os.environ.get("PULSE_TOKEN_ENCRYPTION_KEY", "")
    if not key or Fernet is None:
        raise RuntimeError("token encryption is not configured")
    return Fernet(key.encode("ascii"))


def save_credential(conn: sqlite3.Connection, provider: str, value: dict) -> None:
    ciphertext = _credential_cipher().encrypt(json.dumps(value, separators=(",", ":")).encode()).decode("ascii")
    conn.execute(
        "INSERT INTO integration_credentials(provider,ciphertext,updated_at) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at",
        (provider, ciphertext, utc_now()),
    )
    conn.commit()


def load_credential(conn: sqlite3.Connection, provider: str) -> dict | None:
    row = conn.execute("SELECT ciphertext FROM integration_credentials WHERE provider=?", (provider,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(_credential_cipher().decrypt(row["ciphertext"].encode()).decode())
    except (json.JSONDecodeError, ValueError, TypeError):
        raise RuntimeError("stored integration credential is invalid")


def set_context_signal(conn: sqlite3.Connection, kind: str, value: dict, source: str, confidence: float, expires_at: str | None = None) -> str:
    marker = hashlib.sha256((kind + ":" + source + ":" + json.dumps(value, sort_keys=True)).encode()).hexdigest()[:24]
    conn.execute(
        "INSERT INTO context_signals(id,kind,value_json,source,confidence,observed_at,expires_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json,confidence=excluded.confidence,observed_at=excluded.observed_at,expires_at=excluded.expires_at",
        (marker, kind, json.dumps(value, separators=(",", ":")), source, max(0.0, min(1.0, float(confidence))), utc_now(), expires_at),
    )
    return marker


def clear_context_signals(conn: sqlite3.Connection, kind: str | None = None, source: str | None = None) -> None:
    clauses = []
    values = []
    if kind:
        clauses.append("kind=?")
        values.append(kind)
    if source:
        clauses.append("source=?")
        values.append(source)
    if not clauses:
        conn.execute("DELETE FROM context_signals")
    else:
        conn.execute("DELETE FROM context_signals WHERE " + " AND ".join(clauses), values)


def create_pairing_challenge(conn: sqlite3.Connection, minutes: int = 10) -> tuple[str, str]:
    code = "-".join([secrets.token_hex(2).upper(), secrets.token_hex(2).upper()])
    challenge_id = secrets.token_urlsafe(16)
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    conn.execute("INSERT INTO pairing_challenges(id,code_hash,expires_at) VALUES(?,?,?)", (challenge_id, code_hash, (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()))
    conn.commit()
    return challenge_id, code


def redeem_pairing_challenge(conn: sqlite3.Connection, code: str, label: str) -> tuple[str, str] | None:
    code_hash = hashlib.sha256(code.strip().upper().encode()).hexdigest()
    row = conn.execute("SELECT * FROM pairing_challenges WHERE code_hash=? AND used_at IS NULL AND expires_at>=?", (code_hash, utc_now())).fetchone()
    if not row:
        return None
    token = secrets.token_urlsafe(32)
    device_id = secrets.token_urlsafe(12)
    conn.execute("UPDATE pairing_challenges SET used_at=? WHERE id=?", (utc_now(), row["id"]))
    conn.execute("INSERT INTO companion_devices(id,label,token_hash,created_at,metadata) VALUES(?,?,?,?,?)", (device_id, label[:120] or "Pulse companion", hashlib.sha256(token.encode()).hexdigest(), utc_now(), "{}"))
    conn.commit()
    return device_id, token


def companion_device(conn: sqlite3.Connection, token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute("SELECT * FROM companion_devices WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)).fetchone()
    if row:
        conn.execute("UPDATE companion_devices SET last_seen_at=? WHERE id=?", (utc_now(), row["id"]))
    return row


def list_companion_devices(conn: sqlite3.Connection) -> list[dict]:
    result = []
    for row in conn.execute("SELECT id,label,created_at,last_seen_at,revoked_at,metadata FROM companion_devices ORDER BY created_at DESC").fetchall():
        item = dict(row)
        item["metadata"] = _safe_json(item.get("metadata"), {})
        item["active"] = item.get("revoked_at") is None
        result.append(item)
    return result


def update_companion_metadata(conn: sqlite3.Connection, device_id: str, metadata: dict) -> None:
    conn.execute("UPDATE companion_devices SET metadata=? WHERE id=? AND revoked_at IS NULL", (json.dumps(metadata if isinstance(metadata, dict) else {}, separators=(",", ":")), device_id))


PACKAGE_STATUSES = {"unknown", "ordered", "in_transit", "delayed", "customs", "out_for_delivery", "delivered", "failed_delivery", "delivery_exception"}


def normalize_tracking_number(value: str) -> str:
    return "".join(char for char in str(value or "").upper() if char.isalnum())[:120]


def normalize_carrier(value: str) -> str:
    text = "".join(char for char in str(value or "").casefold() if char.isalnum())
    return {"ups": "UPS", "fedex": "FedEx", "dhl": "DHL", "usps": "USPS", "amazon": "Amazon", "servientrega": "Servientrega", "coordinadora": "Coordinadora"}.get(text, str(value or "").strip()[:80])


def upsert_package_record(conn: sqlite3.Connection, tracking_number: str, merchant: str = "", **fields) -> dict:
    tracking_number = normalize_tracking_number(tracking_number)
    merchant = str(merchant).strip()[:160]
    if not tracking_number:
        raise ValueError("tracking number is required")
    existing = conn.execute("SELECT * FROM packages WHERE tracking_number=? ORDER BY updated_at DESC LIMIT 1", (tracking_number,)).fetchone()
    package_id = existing["id"] if existing else "pkg-" + hashlib.sha256((tracking_number + ":" + merchant).encode()).hexdigest()[:24]
    status = str(fields.get("status", existing["status"] if existing else "unknown")).strip().casefold().replace(" ", "_")
    status = {"outfordelivery": "out_for_delivery", "failed": "failed_delivery", "exception": "delivery_exception"}.get(status, status)
    if status not in PACKAGE_STATUSES:
        status = "unknown"
    history = _safe_json(existing["status_history"], []) if existing else []
    if not isinstance(history, list):
        history = []
    if not history or history[-1].get("status") != status:
        history.append({"status": status, "at": utc_now()})
    changed = not existing or existing["status"] != status
    values = (normalize_carrier(fields.get("carrier", existing["carrier"] if existing else "")), existing["merchant"] if existing else merchant,
        str(fields.get("order_id", existing["order_id"] if existing else ""))[:120], fields.get("estimated_delivery", existing["estimated_delivery"] if existing else None),
        status, json.dumps(history[-20:], separators=(",", ":")), str(fields.get("source", existing["source"] if existing else ""))[:120], utc_now())
    if existing:
        conn.execute("UPDATE packages SET carrier=?,merchant=?,order_id=?,estimated_delivery=?,status=?,status_history=?,source=?,updated_at=? WHERE id=?", (*values, package_id))
    else:
        conn.execute("INSERT INTO packages(id,tracking_number,carrier,merchant,order_id,estimated_delivery,status,status_history,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (package_id, tracking_number, values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7]))
    return {"id": package_id, "tracking_number": tracking_number, "status": status, "changed": changed, "history": history[-20:]}


def upsert_package(conn: sqlite3.Connection, tracking_number: str, merchant: str = "", **fields) -> str:
    return upsert_package_record(conn, tracking_number, merchant, **fields)["id"]


PURCHASE_LIFECYCLES = {"interested", "planned", "ordered", "paid", "shipped", "delayed", "delivered", "cancelled", "returned", "refunded"}


def upsert_purchase_record(conn: sqlite3.Connection, external_key: str, **fields) -> dict:
    external_key = str(external_key).strip()[:240]
    if not external_key:
        raise ValueError("purchase key is required")
    order_id = str(fields.get("order_id", "")).strip()[:120]
    existing = conn.execute("SELECT * FROM purchases WHERE external_key=?", (external_key,)).fetchone()
    if not existing and order_id:
        existing = conn.execute("SELECT * FROM purchases WHERE order_id=? ORDER BY updated_at DESC LIMIT 1", (order_id,)).fetchone()
    if not existing and fields.get("merchant") and fields.get("title"):
        existing = conn.execute("SELECT * FROM purchases WHERE merchant=? AND title=? ORDER BY updated_at DESC LIMIT 1", (str(fields["merchant"])[:160], str(fields["title"])[:240])).fetchone()
    purchase_id = existing["id"] if existing else "purchase-" + hashlib.sha256(external_key.encode()).hexdigest()[:24]
    metadata = fields.get("metadata", _safe_json(existing["metadata"], {}) if existing else {})
    lifecycle = str(fields.get("lifecycle", existing["lifecycle"] if existing else "interested")).strip().casefold().replace(" ", "_")
    lifecycle = {"canceled": "cancelled", "return": "returned", "refund": "refunded"}.get(lifecycle, lifecycle)
    if lifecycle not in PURCHASE_LIFECYCLES:
        lifecycle = "interested"
    changed = not existing or existing["lifecycle"] != lifecycle
    values = (str(fields.get("merchant", existing["merchant"] if existing else ""))[:160],
        str(fields.get("title", existing["title"] if existing else ""))[:240], fields.get("amount", existing["amount"] if existing else None),
        str(fields.get("currency", existing["currency"] if existing else ""))[:12], lifecycle,
        order_id or (existing["order_id"] if existing else ""), fields.get("package_id", existing["package_id"] if existing else None),
        json.dumps(metadata if isinstance(metadata, dict) else {}, separators=(",", ":")), utc_now())
    if existing:
        conn.execute("UPDATE purchases SET merchant=?,title=?,amount=?,currency=?,lifecycle=?,order_id=?,package_id=?,metadata=?,updated_at=? WHERE id=?", (*values, purchase_id))
    else:
        conn.execute("INSERT INTO purchases(id,external_key,merchant,title,amount,currency,lifecycle,order_id,package_id,metadata,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (purchase_id, external_key, *values))
    return {"id": purchase_id, "external_key": external_key, "lifecycle": lifecycle, "changed": changed}


def upsert_purchase(conn: sqlite3.Connection, external_key: str, **fields) -> str:
    return upsert_purchase_record(conn, external_key, **fields)["id"]


def init_db(path: str | Path, initial_password_hash: str = "") -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        migrations = {
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
            "canonical_event_id": "TEXT NOT NULL DEFAULT ''",
            "cluster_id": "TEXT NOT NULL DEFAULT ''",
            "development_id": "TEXT NOT NULL DEFAULT ''",
            "normalized_title": "TEXT NOT NULL DEFAULT ''",
            "normalized_entities": "TEXT NOT NULL DEFAULT '[]'",
            "normalized_location": "TEXT NOT NULL DEFAULT ''",
            "provenance": "TEXT NOT NULL DEFAULT '{}'",
            "decision_trace": "TEXT NOT NULL DEFAULT '{}'",
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "notified_hash": "TEXT NOT NULL DEFAULT ''",
            "notification_pending": "INTEGER NOT NULL DEFAULT 0",
            "notification_count": "INTEGER NOT NULL DEFAULT 0",
            "last_notification_at": "TEXT",
            "notification_reason": "TEXT NOT NULL DEFAULT ''",
        }
        added = False
        for name, definition in migrations.items():
            if name not in event_columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
                added = True
        conn.execute("DROP INDEX IF EXISTS idx_events_pending")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_pending ON events(notification_pending, suppress_notification, score)")
        if added:
            # Existing unreviewed rows must remain history, not become a push storm
            # on the first stricter worker run.
            conn.execute("UPDATE events SET notification_pending=0")
        decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_decisions)").fetchall()}
        decision_migrations = {
            "distance_from_threshold": "INTEGER NOT NULL DEFAULT 0",
            "later_became_important": "INTEGER NOT NULL DEFAULT 0",
            "later_development_notified": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in decision_migrations.items():
            if name not in decision_columns:
                conn.execute(f"ALTER TABLE event_decisions ADD COLUMN {name} {definition}")
        companion_columns = {row[1] for row in conn.execute("PRAGMA table_info(companion_devices)").fetchall()}
        if "metadata" not in companion_columns:
            conn.execute("ALTER TABLE companion_devices ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        for row in conn.execute("SELECT * FROM events WHERE canonical_event_id='' OR content_hash='' OR (notified_at IS NOT NULL AND notification_count=0)").fetchall():
            item = event_from_row(row)
            fingerprint = event_fingerprint(item)
            notified = 1 if item.get("notified_at") else 0
            conn.execute(
                "UPDATE events SET content_hash=?, notified_hash=?, notification_count=?, last_notification_at=COALESCE(last_notification_at,?) WHERE id=?",
                (fingerprint, fingerprint if notified else "", notified, item.get("notified_at"), item["id"]),
            )
            _ensure_canonical_model(conn, {**item, "content_hash": fingerprint})
        # Populate the standalone cluster registry for databases upgraded from
        # the first canonical-model migration as well as new databases.
        for row in conn.execute("SELECT * FROM events WHERE canonical_event_id!='' AND cluster_id!=''").fetchall():
            _ensure_canonical_model(conn, event_from_row(row))
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
        "personal_priorities": {key: 0 for key in PERSONAL_PRIORITY_CATEGORIES},
        "temporary_priority": {},
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


def passive_source_weights(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT events.source_id,
                  SUM(CASE WHEN event_actions.action='delivered' THEN 1 ELSE 0 END) AS delivered,
                  SUM(CASE WHEN event_actions.action='opened' THEN 1 ELSE 0 END) AS opened
           FROM event_actions JOIN events ON events.id=event_actions.event_id
           WHERE event_actions.action IN ('delivered','opened')
           GROUP BY events.source_id"""
    ).fetchall()
    weights = {}
    for row in rows:
        delivered = int(row["delivered"] or 0)
        if delivered < 3:
            continue
        ratio = int(row["opened"] or 0) / delivered
        weights[row["source_id"]] = max(-6, min(6, round((ratio - 0.5) * 12)))
    return weights


def feedback_weights(conn: sqlite3.Connection, field: str) -> dict:
    if field not in {"topic", "source_id"}:
        return {}
    rows = conn.execute(f"SELECT events.{field} AS key,event_actions.action,event_actions.created_at FROM event_actions JOIN events ON events.id=event_actions.event_id WHERE event_actions.action IN ('useful','not_useful','too_late')").fetchall()
    values, totals = {}, {}
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - created).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 0.0
        decay = max(0.25, math.exp(-age_days / 180.0))
        weight = {"useful": 3, "not_useful": -3, "too_late": -2}[row["action"]]
        values[row["key"]] = values.get(row["key"], 0.0) + weight * decay
        totals[row["key"]] = totals.get(row["key"], 0) + 1
    weights = {key: max(-8, min(8, round(value))) for key, value in values.items() if totals.get(key, 0) >= 2}
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
    base["personal_priorities"] = {**base["personal_priorities"], **(stored.get("personal_priorities", {}) if isinstance(stored.get("personal_priorities", {}), dict) else {})}
    base["temporary_priority"] = stored.get("temporary_priority", {}) if isinstance(stored.get("temporary_priority", {}), dict) else {}
    for key in ("followed_entities", "less_like_entities", "less_like_topics"):
        base[key] = stored.get(key, {}) if isinstance(stored.get(key, {}), dict) else {}
    stored_weights = stored.get("learned_topic_weights", {}) if isinstance(stored.get("learned_topic_weights", {}), dict) else {}
    base["learned_topic_weights"] = {**passive_topic_weights(conn), **feedback_weights(conn, "topic"), **stored_weights}
    base["learned_source_weights"] = {**passive_source_weights(conn), **feedback_weights(conn, "source_id")}
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
    conn.execute("DELETE FROM event_actions WHERE action IN ('follow', 'less_like', 'useful', 'not_useful', 'too_late', 'opened', 'source_clicked', 'delivered')")
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
    for key in ("relevant", "suppress_notification", "notification_pending"):
        item[key] = bool(item[key])
    for key, fallback in (("metadata", {}), ("normalized_entities", []), ("provenance", {}), ("decision_trace", {})):
        try:
            item[key] = json.loads(item.get(key) or json.dumps(fallback))
        except (TypeError, json.JSONDecodeError):
            item[key] = fallback
    return item


def _canonical_identity(event: dict) -> tuple[str, str, str, list[str], str]:
    metadata = event.get("metadata") or {}
    cluster = event.get("cluster_id") or cluster_id_for(event)
    canonical = event.get("canonical_event_id") or "canonical-" + hashlib.sha256(
        str(event.get("canonical_key") or event.get("id") or cluster).encode("utf-8")
    ).hexdigest()[:24]
    normalized_title = normalize(clean_event_title(event))
    entities = [str(item) for item in (metadata.get("entities") or []) if item]
    location = str(metadata.get("location") or metadata.get("weather_label") or ("La Ceja, Antioquia" if metadata.get("earthquake_id") else ""))
    return canonical, cluster, normalized_title, sorted(set(entities)), location


def _ensure_canonical_model(conn: sqlite3.Connection, event: dict) -> dict:
    now = utc_now()
    metadata = dict(event.get("metadata") or {})
    canonical, cluster, normalized_title, entities, location = _canonical_identity(event)
    fingerprint = event.get("content_hash") or event_fingerprint(event)
    development = event.get("development_id") or "development-" + hashlib.sha256((canonical + fingerprint).encode("utf-8")).hexdigest()[:24]
    observation_key = hashlib.sha256((str(event.get("source_id")) + str(event.get("canonical_key")) + fingerprint + str(event.get("url", ""))).encode("utf-8")).hexdigest()
    observation_id = "observation-" + observation_key[:24]
    provenance = dict(event.get("provenance") or {})
    observation_ids = list(provenance.get("observation_ids") or [])
    if observation_id not in observation_ids:
        observation_ids.append(observation_id)
    provenance.update({
        "observation_ids": observation_ids[-50:],
        "sources": metadata.get("sources") or [{"url": event.get("url", ""), "title": event.get("title", ""), "trust": metadata.get("source_trust", "")}],
    })
    conn.execute(
        """INSERT INTO story_clusters(id,topic,normalized_title,normalized_entities,normalized_location,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET topic=excluded.topic,normalized_title=excluded.normalized_title,
             normalized_entities=excluded.normalized_entities,normalized_location=excluded.normalized_location,
             updated_at=excluded.updated_at""",
        (cluster, event.get("topic", "watcher"), normalized_title, json.dumps(entities, separators=(",", ":")), location, now, now),
    )
    conn.execute(
        """INSERT INTO canonical_events(id,topic,normalized_title,normalized_entities,normalized_location,cluster_id,current_event_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET topic=excluded.topic,normalized_title=excluded.normalized_title,normalized_entities=excluded.normalized_entities,normalized_location=excluded.normalized_location,cluster_id=excluded.cluster_id,current_event_id=excluded.current_event_id,updated_at=excluded.updated_at""",
        (canonical, event.get("topic", "watcher"), normalized_title, json.dumps(entities, separators=(",", ":")), location, cluster, event.get("id", ""), now, now),
    )
    conn.execute(
        """INSERT OR IGNORE INTO event_observations(id,canonical_event_id,source_id,source_kind,source_url,source_title,source_trust,observation_key,content_hash,published_at,observed_at,raw_payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (observation_id, canonical, event.get("source_id", ""), event.get("source_kind", ""), event.get("url", ""), event.get("title", ""), metadata.get("source_trust", ""), observation_key, fingerprint, event.get("published_at"), now, json.dumps({"title": event.get("title", ""), "summary": event.get("summary", ""), "metadata": metadata}, separators=(",", ":"))),
    )
    conn.execute(
        """INSERT OR IGNORE INTO event_developments(id,canonical_event_id,fingerprint,title,summary,discovered_at,meaningful,source_observation_id,metadata)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (development, canonical, fingerprint, event.get("title", ""), event.get("summary", ""), event.get("discovered_at") or now, int(event.get("development_meaningful", True)), observation_id, json.dumps(metadata, separators=(",", ":"))),
    )
    metadata["canonical_event_id"] = canonical
    metadata["cluster_id"] = cluster
    metadata["development_id"] = development
    event.update({
        "canonical_event_id": canonical,
        "cluster_id": cluster,
        "development_id": development,
        "normalized_title": normalized_title,
        "normalized_entities": entities,
        "normalized_location": location,
        "provenance": provenance,
        "metadata": metadata,
    })
    if event.get("id"):
        conn.execute(
            "UPDATE events SET canonical_event_id=?,cluster_id=?,development_id=?,normalized_title=?,normalized_entities=?,normalized_location=?,provenance=? WHERE id=?",
            (canonical, cluster, development, normalized_title, json.dumps(entities, separators=(",", ":")), location, json.dumps(provenance, separators=(",", ":")), event["id"]),
        )
    return event


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
           WHERE notification_pending=1 AND suppress_notification=0 AND relevant=1
           ORDER BY score DESC, discovered_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [event_from_row(row) for row in rows]


def _merge_metadata(old: dict, new: dict) -> dict:
    merged = {**(old or {}), **(new or {})}
    for key in ("sources", "entities", "entity_names"):
        values = []
        for item in ((old or {}).get(key) or []) + ((new or {}).get(key) or []):
            marker = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
            if marker not in {json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value) for value in values}:
                values.append(item)
        if values:
            merged[key] = values
    if len(merged.get("sources") or []) > 1:
        merged["verification"] = "cross_source"
    merged["source_count"] = len(merged.get("sources") or [])
    if old.get("safety_critical") or new.get("safety_critical"):
        merged["safety_critical"] = True
    return merged


def _find_similar_event(conn: sqlite3.Connection, event: dict) -> dict | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    rows = conn.execute(
        "SELECT * FROM events WHERE topic=? AND discovered_at>=? ORDER BY discovered_at DESC LIMIT 100",
        (event.get("topic"), cutoff),
    ).fetchall()
    for row in rows:
        existing = event_from_row(row)
        if existing and existing.get("canonical_key") != event.get("canonical_key") and events_similar(existing, event):
            return existing
    return None


def upsert_event(conn: sqlite3.Connection, event: dict) -> tuple[dict, bool]:
    now = utc_now()
    event = {**event, "discovered_at": event.get("discovered_at") or now, "last_seen_at": now}
    event["title"] = clean_event_title(event)
    event["summary"] = clean_event_summary(event)
    event["body"] = str(event.get("body") or "")[:700]
    existing = conn.execute("SELECT * FROM events WHERE canonical_key=?", (event["canonical_key"],)).fetchone()
    if existing is None:
        similar = _find_similar_event(conn, event)
        if similar:
            event["canonical_key"] = similar["canonical_key"]
            event["id"] = similar["id"]
            event["metadata"] = _merge_metadata(similar.get("metadata") or {}, event.get("metadata") or {})
            existing = conn.execute("SELECT * FROM events WHERE id=?", (similar["id"],)).fetchone()
    fingerprint = event_fingerprint(event)
    event["content_hash"] = fingerprint
    if existing:
        existing_item = event_from_row(existing)
        for key in ("canonical_event_id", "cluster_id"):
            if existing_item.get(key):
                event[key] = existing_item[key]
    if existing:
        event["development_meaningful"] = materially_changed(event_from_row(existing), event) or bool((event.get("metadata") or {}).get("development_meaningful"))
    development_meaningful = event.get("development_meaningful", (event.get("metadata") or {}).get("development_meaningful", False))
    event["metadata"] = {**(event.get("metadata") or {}), "development_meaningful": bool(development_meaningful)}
    event = _ensure_canonical_model(conn, event)
    if existing:
        old = event_from_row(existing)
        meaningful = materially_changed(old, event)
        if old.get("notification_count", 0) == 0:
            pending = int(bool(event["relevant"]) and not event.get("suppress_notification", False))
        elif meaningful and event["relevant"] and not event.get("suppress_notification", False):
            pending = 1
        elif not event["relevant"] or event.get("suppress_notification", False):
            pending = 0
        else:
            pending = int(old.get("notification_pending", False))
        conn.execute(
            """UPDATE events SET title=?,summary=?,body=?,url=?,published_at=?,last_seen_at=?,score=?,priority=?,relevant=?,suppress_notification=?,metadata=?,canonical_event_id=?,cluster_id=?,development_id=?,normalized_title=?,normalized_entities=?,normalized_location=?,provenance=?,content_hash=?,notification_pending=?,notification_reason=?
               WHERE canonical_key=?""",
            (
                event["title"], event["summary"], event["body"], event["url"], event.get("published_at"),
                now, event["score"], event["priority"], int(event["relevant"]), int(event.get("suppress_notification", False)), json.dumps(event.get("metadata") or {}, separators=(",", ":")), event["canonical_event_id"], event["cluster_id"], event["development_id"], event["normalized_title"], json.dumps(event["normalized_entities"], separators=(",", ":")), event["normalized_location"], json.dumps(event["provenance"], separators=(",", ":")), fingerprint, pending, "" if pending else old.get("notification_reason", ""), event["canonical_key"],
            ),
        )
        return event_from_row(conn.execute("SELECT * FROM events WHERE canonical_key=?", (event["canonical_key"],)).fetchone()), False
    conn.execute(
        """INSERT INTO events(id,source_id,source_kind,topic,title,summary,body,url,canonical_key,published_at,discovered_at,last_seen_at,score,priority,relevant,suppress_notification,created_from,metadata,canonical_event_id,cluster_id,development_id,normalized_title,normalized_entities,normalized_location,provenance,content_hash,notification_pending)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["id"], event["source_id"], event["source_kind"], event["topic"], event["title"],
            event.get("summary", ""), event.get("body", ""), event["url"], event["canonical_key"],
            event.get("published_at"), event["discovered_at"], now, event["score"], event["priority"],
            int(event["relevant"]), int(event.get("suppress_notification", False)), event.get("created_from", "source"), json.dumps(event.get("metadata") or {}, separators=(",", ":")), event["canonical_event_id"], event["cluster_id"], event["development_id"], event["normalized_title"], json.dumps(event["normalized_entities"], separators=(",", ":")), event["normalized_location"], json.dumps(event["provenance"], separators=(",", ":")), fingerprint, int(bool(event["relevant"]) and not event.get("suppress_notification", False)),
        ),
    )
    return event_from_row(conn.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()), True


def mark_notified(conn: sqlite3.Connection, event_id: str) -> None:
    now = utc_now()
    row = conn.execute("SELECT decision_trace FROM events WHERE id=?", (event_id,)).fetchone()
    trace = {}
    if row:
        try:
            trace = json.loads(row["decision_trace"] or "{}")
        except json.JSONDecodeError:
            trace = {}
    trace["pushed"] = True
    trace["last_push_at"] = now
    conn.execute(
        "UPDATE events SET notified_at=COALESCE(notified_at,?),last_notification_at=?,notification_pending=0,notification_count=notification_count+1,notified_hash=content_hash,notification_reason=?,decision_trace=? WHERE id=?",
        (now, now, "sent", json.dumps(trace, separators=(",", ":")), event_id),
    )
    identity = conn.execute("SELECT canonical_event_id,cluster_id,development_id FROM events WHERE id=?", (event_id,)).fetchone()
    if identity:
        rows = conn.execute(
            "SELECT id,trace FROM event_decisions WHERE allowed=0 AND evaluated_at<=? AND (canonical_event_id=? OR (cluster_id!='' AND cluster_id=?))",
            (now, identity["canonical_event_id"], identity["cluster_id"]),
        ).fetchall()
        for decision in rows:
            try:
                prior_trace = json.loads(decision["trace"] or "{}")
            except json.JSONDecodeError:
                prior_trace = {}
            later_development = bool(identity["development_id"] and prior_trace.get("development_id") and prior_trace.get("development_id") != identity["development_id"])
            prior_trace.update({
                "later_became_important": True,
                "later_development_notified": bool(prior_trace.get("later_development_notified") or later_development),
            })
            conn.execute(
                "UPDATE event_decisions SET later_became_important=1,later_development_notified=MAX(later_development_notified,?),trace=? WHERE id=?",
                (int(later_development), json.dumps(prior_trace, separators=(",", ":")), decision["id"]),
            )


def mark_notification_suppressed(conn: sqlite3.Connection, event_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE events SET notification_pending=0,notification_reason=? WHERE id=?",
        (reason[:120], event_id),
    )


def record_notification_decision(conn: sqlite3.Connection, event: dict, allowed: bool, reason: str, trace: dict) -> None:
    now = utc_now()
    threshold = int(trace.get("threshold", 0))
    score = int(trace.get("effective_score", event.get("score", 0)))
    distance = score - threshold
    near = int(distance >= -15)
    trace = {**trace, "distance_from_threshold": distance, "near_threshold": bool(near), "later_became_important": False, "later_development_notified": False}
    conn.execute(
        "UPDATE events SET decision_trace=?,notification_reason=? WHERE id=?",
        (json.dumps(trace, separators=(",", ":")), "" if allowed else reason[:120], event["id"]),
    )
    recent = conn.execute(
        "SELECT 1 FROM event_decisions WHERE event_id=? AND reason=? AND evaluated_at>=? LIMIT 1",
        (event["id"], reason, (datetime.now(timezone.utc) - timedelta(minutes=14)).replace(microsecond=0).isoformat()),
    ).fetchone()
    if not recent:
        conn.execute(
            "INSERT INTO event_decisions(event_id,canonical_event_id,cluster_id,evaluated_at,allowed,reason,score,threshold,tier,near_threshold,distance_from_threshold,later_became_important,later_development_notified,trace) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event["id"], event.get("canonical_event_id", ""), event.get("cluster_id", ""), now, int(allowed), reason[:160], score, threshold, str(trace.get("notification_tier", "low")), near, distance, 0, 0, json.dumps(trace, separators=(",", ":"))),
        )


def list_notification_decisions(conn: sqlite3.Connection, limit: int = 100, near_only: bool = False, suppressed_only: bool = False) -> list[dict]:
    clauses = []
    if near_only:
        clauses.append("d.near_threshold=1")
    if suppressed_only:
        clauses.append("d.allowed=0")
    clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT d.*,e.topic,e.title,e.normalized_title AS event_normalized_title,e.priority,e.notification_reason AS current_notification_reason FROM event_decisions d LEFT JOIN events e ON e.id=d.event_id{clause} ORDER BY d.evaluated_at DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["trace"] = json.loads(item.get("trace") or "{}")
        except json.JSONDecodeError:
            item["trace"] = {}
        item["allowed"] = bool(item["allowed"])
        item["near_threshold"] = bool(item["near_threshold"])
        item["later_became_important"] = bool(item.get("later_became_important"))
        item["later_development_notified"] = bool(item.get("later_development_notified"))
        result.append(item)
    return result


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


def create_morning_catchup(conn: sqlite3.Connection, config: dict, now: datetime | None = None) -> tuple[dict | None, list[dict]]:
    if not config.get("MORNING_CATCHUP_ENABLED", True):
        return None, []
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(config.get("TIMEZONE", "UTC")))
    hour, minute = [int(part) for part in str(config.get("QUIET_END", "07:00")).split(":", 1)]
    start = hour * 60 + minute
    current = local.hour * 60 + local.minute
    if current < start or current >= start + int(config.get("MORNING_CATCHUP_WINDOW_MINUTES", 60)):
        return None, []
    date_key = local.date().isoformat()
    if get_json_setting(conn, "morning_catchup_date", "") == date_key:
        return None, []
    rows = conn.execute(
        """SELECT * FROM events WHERE notification_pending=1 AND relevant=1 AND suppress_notification=0
           AND topic!='system' ORDER BY score DESC, discovered_at DESC LIMIT 8"""
    ).fetchall()
    events = [event_from_row(row) for row in rows]
    save_json_setting(conn, "morning_catchup_date", date_key)
    if len(events) < 2:
        return None, events
    titles = [clean_event_title(item) for item in events[:5]]
    digest_id = "morning-" + hashlib.sha256(date_key.encode("utf-8")).hexdigest()[:16]
    digest = {
        "id": digest_id,
        "source_id": "pulse-morning-catchup",
        "source_kind": "digest",
        "topic": "system",
        "title": "Your Pulse catch-up",
        "summary": f"{len(events)} saved signals were held during quiet hours: " + "; ".join(titles) + ".",
        "body": "Morning catch-up for events that were relevant but not urgent enough to interrupt quiet hours.",
        "url": config.get("APP_URL", "https://pulse.moralife.uk") + "/",
        "canonical_key": "pulse-morning:" + date_key,
        "published_at": now.replace(microsecond=0).isoformat(),
        "score": 90,
        "priority": "high",
        "relevant": True,
        "metadata": {"digest": True, "digest_event_ids": [item["id"] for item in events]},
    }
    for item in events:
        mark_notification_suppressed(conn, item["id"], "included in morning catch-up")
    stored, _ = upsert_event(conn, digest)
    return stored, events


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
