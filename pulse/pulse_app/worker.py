from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .config import load_config
from .push import send_payload
from .rules import should_notify
from .sources import collect_candidates
from .storage import connect, due_reminders, get_preferences, init_db, mark_notified, mark_reminded, pending_events, upsert_event

LOGGER = logging.getLogger("pulse.worker")


def _payload(config: dict, event: dict, reminder: bool = False) -> dict:
    title = ("Reminder · " if reminder else "") + event["title"]
    body = event.get("summary") or event.get("body") or "Open Pulse for details."
    return {
        "id": event["id"],
        "title": title[:120],
        "body": body[:220],
        "topic": event["topic"],
        "priority": event["priority"],
        "url": "/event/" + event["id"],
        "tag": "pulse-event-" + event["id"],
    }


def _send_event(conn, config: dict, event: dict, reminder: bool = False) -> dict:
    result = send_payload(conn, config, _payload(config, event, reminder))
    mark_notified(conn, event["id"])
    conn.commit()
    return result


def run_once(config: dict | None = None) -> dict:
    config = config or load_config()
    init_db(config["DATABASE_PATH"])
    conn = connect(config["DATABASE_PATH"])
    inserted = 0
    notifications = 0
    errors = []
    try:
        candidates = collect_candidates(conn, config)
        for candidate in candidates:
            event, created = upsert_event(conn, candidate)
            inserted += int(created)
            if created:
                conn.commit()
        preferences = get_preferences(conn, config)
        for event in pending_events(conn):
            allowed, reason = should_notify(event, preferences)
            if not allowed:
                continue
            try:
                result = _send_event(conn, config, event)
                notifications += int(result.get("sent", 0))
                errors.extend(result.get("errors", []))
                LOGGER.info("event %s sent=%s removed=%s", event["id"], result.get("sent", 0), result.get("removed", 0))
            except Exception as exc:
                errors.append(str(exc))
                LOGGER.exception("notification failed for %s", event["id"])
        for event in due_reminders(conn):
            try:
                result = _send_event(conn, config, event, reminder=True)
                mark_reminded(conn, event["id"])
                conn.commit()
                notifications += int(result.get("sent", 0))
                errors.extend(result.get("errors", []))
            except Exception as exc:
                errors.append(str(exc))
                LOGGER.exception("reminder failed for %s", event["id"])
        return {"inserted": inserted, "notifications": notifications, "errors": errors}
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    LOGGER.info("Pulse worker started; interval=%s minutes", config["POLL_MINUTES"])
    while True:
        try:
            result = run_once(config)
            LOGGER.info("check complete: %s", result)
        except Exception:
            LOGGER.exception("worker check failed")
        time.sleep(config["POLL_MINUTES"] * 60)


if __name__ == "__main__":
    main()
