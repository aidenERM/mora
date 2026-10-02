"""Deterministic wakeups. No model calls while a task sleeps."""
from datetime import datetime, timedelta
import json
import os
import re
import time
import uuid
from zoneinfo import ZoneInfo
from .store import MODELS


def parse_timing(text, now=None):
    now = now or datetime.now(ZoneInfo("America/Bogota"))
    # Questions about tomorrow's conditions should be answered now, not deferred.
    if re.match(r"(?i)^(what|when|where|why|how|is|are|does|will)\b", text.strip()):
        return None
    recurring = re.search(r"(?i)\bevery\s+(?:(\d+)\s+)?(minutes?|hours?|days?|day|daily)\b", text)
    if recurring:
        number = int(recurring.group(1) or 1)
        unit = recurring.group(2).lower()
        seconds = number * (60 if unit.startswith("minute") else 3600 if unit.startswith("hour") else 86400)
        if not 900 <= seconds <= 31 * 86400:
            raise ValueError("recurring intervals must be between 15 minutes and 31 days")
        return {"interval": seconds, "wake_at": now.timestamp() + seconds, "goal": (text[:recurring.start()] + text[recurring.end():]).strip(" ,.")}
    tomorrow = re.search(r"(?i)\btomorrow(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?\b", text)
    if tomorrow:
        hour, minute = int(tomorrow.group(1) or 9), int(tomorrow.group(2) or 0)
        meridian = (tomorrow.group(3) or "").lower()
        if meridian:
            if not 1 <= hour <= 12:
                raise ValueError("invalid 12-hour time")
            hour = hour % 12 + (12 if meridian == "pm" else 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("invalid time")
        wake = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {"wake_at": wake.timestamp(), "interval": None, "goal": text}
    later = re.search(r"(?i)\bin\s+(\d+|one|two|three)\s+(minutes?|hours?|days?)\b", text)
    if later:
        number = {"one": 1, "two": 2, "three": 3}.get(later.group(1).lower())
        number = number if number is not None else int(later.group(1))
        seconds = number * (60 if later.group(2).lower().startswith("minute") else 3600 if later.group(2).lower().startswith("hour") else 86400)
        if not 60 <= seconds <= 31 * 86400:
            raise ValueError("wake interval out of range")
        return {"wake_at": now.timestamp() + seconds, "interval": None, "goal": text}
    return None


class Scheduler:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, goal TEXT, model TEXT, interval REAL,
                    next_run REAL, enabled INTEGER DEFAULT 1, metadata TEXT, last_task TEXT
                );
                CREATE INDEX IF NOT EXISTS schedules_due ON schedules(enabled,next_run);
            ''')

    def create(self, goal, interval, next_run=None, model="auto", metadata=None):
        if model not in MODELS:
            raise ValueError("unknown model selection")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 900 <= interval <= 31 * 86400:
            raise ValueError("invalid recurrence interval")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
            raise ValueError("invalid schedule goal")
        schedule_id = uuid.uuid4().hex
        from .security import clean
        with self.store.connect() as db:
            db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,1,?,NULL)", (schedule_id, clean(goal), model, interval, next_run or time.time() + interval, json.dumps(clean(metadata or {}))))
        return schedule_id

    def list(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,goal,interval,next_run,enabled FROM schedules ORDER BY next_run LIMIT 30")]

    def disable(self, schedule_id):
        with self.store.connect() as db:
            db.execute("UPDATE schedules SET enabled=0 WHERE id=?", (schedule_id,))

    def tick(self, now=None):
        now = now or time.time()
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=? LIMIT 5", (now,)).fetchall()
        created = []
        for row in rows:
            # Recover an interrupted tick by reusing the same occurrence key.
            metadata = json.loads(row["metadata"])
            metadata["schedule_id"] = row["id"]
            task = self.store.create(row["goal"], row["model"], max_steps=8, source="schedule",
                source_key=f"schedule:{row['id']}:{row['next_run']}", metadata=metadata)
            with self.store.connect() as db:
                db.execute("UPDATE schedules SET next_run=?,last_task=? WHERE id=? AND next_run=?", (now + row["interval"], task["id"], row["id"], row["next_run"]))
            created.append(task)
        return created


def enqueue_pulse_event(event, trace, store=None):
    """Called only after Pulse's existing relevance/quiet-hours/cooldown decision."""
    if os.environ.get("ORBIT_PROACTIVE_ENABLED", "0") != "1":
        return None
    if trace.get("notification_tier") not in {"high", "urgent"} or int(trace.get("effective_score", event.get("score", 0))) < 85:
        return None
    from .store import Store
    store = store or Store()
    day = datetime.now(ZoneInfo("America/Bogota")).date().isoformat()
    identity = str(event.get("canonical_event_id") or event["id"])
    development = str(event.get("development_id") or event.get("content_hash") or identity)
    source_key = "pulse:" + identity + ":" + development
    with store.connect() as db:
        if db.execute("SELECT 1 FROM tasks WHERE source_key=?", (source_key,)).fetchone():
            return None
        count = db.execute("SELECT COUNT(*) FROM tasks WHERE source='pulse' AND metadata LIKE ?", ('%"day": "' + day + '"%',)).fetchone()[0]
        if count >= 2:
            return None
    return store.create("Investigate this Pulse event using read-only tools. Identify any practical new action or change. Do not repeat the original alert: " + event.get("title", "")[:200],
        model=store.state("default_model") or "auto", max_steps=6, source="pulse", source_key=source_key, metadata={"read_only": True, "day": day,
            "relevance": {"score": trace.get("effective_score"), "reason": event.get("body", "")[:400], "local": trace.get("local_relevance")},
            "event_id": event["id"], "summary": event.get("summary", "")[:800], "url": event.get("url", ""), "notify": False})
