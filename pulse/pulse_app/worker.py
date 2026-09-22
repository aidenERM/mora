from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone

from .config import load_config
from .discovery import collect_discovery_candidates
from .integrations import PROVIDERS, mark_failure, sync_provider
from .push import send_payload
from .rules import apply_preference_adjustments, evaluate_notification, notification_copy
from .sources import collect_candidates, collect_watch_candidates
from .storage import connect, create_morning_catchup, due_reminders, game_event_candidates, get_preferences, get_source_state, init_db, list_active_plans, mark_notification_suppressed, mark_notified, mark_reminded, pending_events, prune_history, record_event_action, record_notification_decision, runtime_config, save_source_state, update_plan_candidate, upsert_event

LOGGER = logging.getLogger("pulse.worker")


def _integration_failure_candidate(config: dict, provider: str, error: str) -> dict:
    label = PROVIDERS.get(provider, provider)
    error_code = str(error or "sync_failed")[:80]
    event_key = f"integration-error:{provider}:{error_code}"
    return {
        "id": "integration-error-" + provider,
        "source_id": "integration:" + provider,
        "source_kind": "integration",
        "topic": "security",
        "title": f"{label} connection needs attention",
        "summary": f"Pulse could not sync {label}.",
        "body": "The connection failed during the scheduled sync. Reconnect it from Pulse links.",
        "url": config.get("APP_URL", "https://pulse.moralife.uk") + "/integrations",
        "canonical_key": event_key,
        "published_at": None,
        "score": 94,
        "priority": "high",
        "relevant": True,
        "suppress_notification": False,
        "metadata": {
            "source_trust": "primary",
            "verification": "provider_sync",
            "integration_failure": True,
            "integration_provider": provider,
            "integration_error_code": error_code,
            "sources": [{"url": config.get("APP_URL", "https://pulse.moralife.uk") + "/integrations", "title": label, "trust": "primary"}],
        },
    }


def _integration_failure_transition(conn, config: dict, provider: str, error: str) -> dict | None:
    error_code = str(error or "sync_failed")[:80]
    state_key = "integration-failure:" + provider
    state = get_source_state(conn, state_key)
    if state.get("active") and state.get("error_code") == error_code:
        return None
    save_source_state(conn, state_key, {"active": True, "error_code": error_code})
    return _integration_failure_candidate(config, provider, error)


def _clear_integration_failure(conn, provider: str) -> None:
    state_key = "integration-failure:" + provider
    state = get_source_state(conn, state_key)
    if state.get("active"):
        save_source_state(conn, state_key, {"active": False, "last_error_code": state.get("error_code", "")})


def _payload(config: dict, event: dict, reminder: bool = False) -> dict:
    title, body = notification_copy(event)
    title = ("Reminder · " if reminder else "") + title
    trace = event.get("decision_trace") or {}
    tier = trace.get("notification_tier") or ("urgent" if event.get("priority") == "critical" else "high" if event.get("priority") == "high" else "normal")
    identity = event.get("canonical_event_id") or event.get("cluster_id") or event["id"]
    return {
        "id": event["id"],
        "title": title[:120],
        "body": body[:220],
        "topic": event["topic"],
        "priority": event["priority"],
        "tier": tier,
        "url": "/event/" + event["id"],
        "tag": "pulse-event-" + identity,
    }


def _send_event(conn, config: dict, event: dict, reminder: bool = False) -> dict:
    result = send_payload(conn, config, _payload(config, event, reminder))
    if not reminder:
        mark_notified(conn, event["id"])
    if result.get("sent", 0) and not reminder:
        record_event_action(conn, event["id"], "delivered", str(result.get("sent", 0)))
    conn.commit()
    return result


def _active_plan_candidates(conn, now: datetime) -> list[dict]:
    candidates = []
    for plan in list_active_plans(conn):
        try:
            end_at = datetime.fromisoformat(str(plan["end_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            end_at = now
        if end_at <= now:
            update_plan_candidate(conn, plan["id"], "completed")
            continue
        rows = conn.execute("SELECT * FROM events WHERE discovered_at>? AND relevant=1 AND topic IN ('weather','package','purchase','travel','service_status','security') ORDER BY discovered_at DESC LIMIT 30", (plan["updated_at"],)).fetchall()
        for row in rows:
            event = dict(row)
            identity = event.get("canonical_event_id") or event.get("cluster_id") or event["id"]
            try:
                event_metadata = json.loads(event.get("metadata") or "{}") if isinstance(event.get("metadata"), str) else (event.get("metadata") or {})
            except json.JSONDecodeError:
                event_metadata = {}
            candidates.append({
                "id": "plan-followup-" + identity[:32],
                "source_id": "plan:" + plan["id"],
                "source_kind": "plan_followup",
                "topic": event["topic"],
                "title": f"{plan['title']}: {event['title']}",
                "summary": f"This may affect your active plan. {event.get('summary') or event.get('body') or ''}"[:700],
                "body": "A tracked plan changed because a related signal appeared.",
                "url": event.get("url") or "https://pulse.moralife.uk/",
                "canonical_key": f"plan-followup:{plan['id']}:{identity}",
                "published_at": event.get("published_at") or event.get("discovered_at"),
                "score": min(99, max(78, int(event.get("score") or 0))),
                "priority": event.get("priority") or "high",
                "relevant": True,
                "suppress_notification": False,
                "metadata": {"plan_followup": True, "plan_id": plan["id"], "related_event_id": event["id"], "sources": event_metadata.get("sources", [])},
            })
    return candidates


def run_once(config: dict | None = None) -> dict:
    config = config or load_config()
    init_db(config["DATABASE_PATH"])
    conn = connect(config["DATABASE_PATH"])
    inserted = 0
    notifications = 0
    errors = []
    try:
        save_source_state(conn, "worker:heartbeat", {"started_at": datetime.now(timezone.utc).isoformat(), "last_completed_at": get_source_state(conn, "worker:heartbeat").get("last_completed_at") or "", "ok": False})
        conn.commit()
        runtime = runtime_config(conn, config)
        integration_candidates = []
        for provider in ("google", "discord", "apple-calendar", "apple-contacts", "apple-mail"):
            credential_provider = "icloud" if provider.startswith("apple-") else provider
            if conn.execute("SELECT 1 FROM integration_credentials WHERE provider=?", (credential_provider,)).fetchone():
                try:
                    sync_provider(conn, config, provider)
                    _clear_integration_failure(conn, provider)
                except Exception as exc:
                    error_code = type(exc).__name__
                    mark_failure(conn, provider, error_code)
                    failure = _integration_failure_transition(conn, config, provider, error_code)
                    if failure:
                        integration_candidates.append(failure)
                    errors.append(provider + ":" + error_code)
                    conn.commit()
        followup_candidates = _active_plan_candidates(conn, datetime.now(timezone.utc))
        direct_candidates = collect_candidates(conn, runtime)
        try:
            watch_candidates = collect_watch_candidates(conn, runtime)
        except Exception as exc:
            LOGGER.exception("watch check failed: %s", exc)
            watch_candidates = []
        candidates = [*integration_candidates, *followup_candidates, *direct_candidates, *watch_candidates, *game_event_candidates(conn)]
        try:
            candidates.extend(collect_discovery_candidates(conn, runtime, direct_candidates))
        except Exception as exc:
            LOGGER.exception("discovery check failed: %s", exc)
        conn.commit()
        preferences = get_preferences(conn, runtime)
        for candidate in candidates:
            apply_preference_adjustments(candidate, preferences)
            event, created = upsert_event(conn, candidate)
            inserted += int(created)
            if created or event.get("notification_pending") or event.get("suppress_notification"):
                allowed, reason, trace = evaluate_notification(event, preferences, datetime.now(timezone.utc), runtime, conn)
                record_notification_decision(conn, event, allowed, reason, trace)
            if created:
                conn.commit()
        morning_digest, _held_events = create_morning_catchup(conn, runtime, datetime.now(timezone.utc))
        if morning_digest:
            inserted += 1
            conn.commit()
        sent_this_run = 0
        now = datetime.now(timezone.utc)
        for event in pending_events(conn, 100):
            allowed, reason, trace = evaluate_notification(event, preferences, now, runtime, conn)
            record_notification_decision(conn, event, allowed, reason, trace)
            if not allowed:
                if reason not in {"quiet hours", "topic or event cooldown"}:
                    mark_notification_suppressed(conn, event["id"], reason)
                    conn.commit()
                continue
            if sent_this_run >= int(runtime.get("MAX_NOTIFICATIONS_PER_RUN", 2)):
                break
            try:
                result = _send_event(conn, config, event)
                notifications += int(result.get("sent", 0))
                sent_this_run += 1
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
        prune_history(conn, runtime, now)
        save_source_state(conn, "worker:heartbeat", {"started_at": get_source_state(conn, "worker:heartbeat").get("started_at", now.isoformat()), "last_completed_at": datetime.now(timezone.utc).isoformat(), "ok": not errors, "errors": errors[:10]})
        conn.commit()
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
