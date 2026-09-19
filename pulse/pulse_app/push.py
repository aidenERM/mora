from __future__ import annotations

import json
import logging

from pywebpush import WebPushException, webpush

from .storage import remove_subscriptions

LOGGER = logging.getLogger(__name__)


def configured(config: dict) -> bool:
    return all(config.get(key) for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"))


def send_payload(conn, config: dict, payload: dict, subscriptions: list[dict] | None = None) -> dict:
    if not configured(config):
        return {"sent": 0, "removed": 0, "configured": False, "errors": ["VAPID is not configured"]}
    subscriptions = subscriptions if subscriptions is not None else _subscriptions(conn)
    removed: list[str] = []
    sent = 0
    errors: list[str] = []
    urgency = "high" if payload.get("priority") in {"critical", "high"} else "normal"
    for item in subscriptions:
        try:
            webpush(
                subscription_info=json.loads(item["payload"]),
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=config["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": config["VAPID_SUBJECT"]},
                ttl=300,
                headers={"Urgency": urgency},
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                removed.append(item["id"])
            else:
                errors.append(f"{item['id']}: {status or type(exc).__name__}")
                LOGGER.warning("web push failed for %s: %s", item["id"], exc)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"{item['id']}: invalid subscription ({exc})")
    remove_subscriptions(conn, removed)
    return {"sent": sent, "removed": len(removed), "configured": True, "errors": errors}


def _subscriptions(conn) -> list[dict]:
    rows = conn.execute("SELECT id,payload FROM subscriptions ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]
