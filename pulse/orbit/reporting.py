"""Useful proactive findings still pass through Pulse's delivery decision."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from urllib.parse import urlsplit

from pulse_app.config import load_config
from pulse_app.rules import evaluate_notification, significant_tokens
from pulse_app.storage import get_event, get_preferences


def report_finding(store, memory, task, result, config=None, now=None):
    if not store.state("report_requested:" + task["id"]):
        store.state("delivery_decision:" + task["id"], {"allowed": False, "reason": "no new useful finding requested"})
        return False
    config = config or load_config()
    now = now or datetime.now(timezone.utc)
    with sqlite3.connect("file:" + config["DATABASE_PATH"] + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        original = get_event(db, task["metadata"]["event_id"])
        if not original:
            store.state("delivery_decision:" + task["id"], {"allowed": False, "reason": "original event unavailable"})
            return False
        observations = [row["observation"] for row in store.history(task["id"], 40) if row["action"] == "browser_observe" and row["status"] == "observed"]
        host = urlsplit(original.get("url", "")).hostname
        relevant = [row for row in observations if host and urlsplit(row.get("url", "")).hostname == host]
        if not relevant:
            store.state("delivery_decision:" + task["id"], {"allowed": False, "reason": "no validated source observation"})
            return False
        summary = result["result"]
        current = set(significant_tokens(summary))
        previous = set(significant_tokens(original["title"] + " " + original.get("summary", "") + " " + original.get("body", "")))
        observed = set(significant_tokens(" ".join(row.get("text", "") for row in relevant)))
        if len(current - previous) < 6 or len(current & observed) < max(4, len(current) // 2):
            store.state("delivery_decision:" + task["id"], {"allowed": False, "reason": "no material source-backed development"})
            return False
        finding = {**original, "summary": summary, "body": summary, "notification_count": 0,
                   "last_notification_at": None, "notified_at": None,
                   "content_hash": hashlib.sha256(summary.encode()).hexdigest(),
                   "metadata": {**original.get("metadata", {}), "development_meaningful": True}}
        allowed, reason, trace = evaluate_notification(finding, get_preferences(db, config), now, config, db)
        # AI-derived reports do not inherit an urgent bypass from the original
        # alert. Pulse still owns validated safety-critical notifications.
        if trace.get("quiet_hours", {}).get("active"):
            allowed, reason = False, "quiet hours for derived Orbit finding"
        store.state("delivery_decision:" + task["id"], {"allowed": allowed, "reason": reason, "trace": trace})
        if not allowed:
            return False
    memory.notice(task["id"], "finding", summary + "\n" + original.get("url", ""))
    return True


def schedule_changed(store, task):
    schedule_id = task["metadata"].get("schedule_id")
    if not schedule_id:
        return True
    steps = store.history(task["id"], 40)
    verified = [row["observation"].get("checks") for row in steps if row["status"] == "verified"]
    evidence = json.dumps(verified, sort_keys=True)
    key = "schedule_evidence:" + schedule_id
    previous = store.state(key)
    store.state(key, evidence)
    return previous != evidence
