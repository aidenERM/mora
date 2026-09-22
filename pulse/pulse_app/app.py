from __future__ import annotations

import json
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from .config import PERSONAL_PRIORITY_CATEGORIES, STATIC, TOPIC_LABELS, load_config
from .discovery import discovery_enabled, discovery_provider
from .push import configured as push_configured, send_payload
from .rules import annotate_candidate, apply_preference_adjustments, cluster_id_for, evaluate_notification, score_item, significant_tokens
from .sources import github_webhook_candidate
from .storage import (
    connect,
    clear_learning,
    create_manual_event,
    delete_subscription,
    event_action_summary,
    get_event,
    get_preferences,
    get_source_state,
    init_db,
    get_password_hash,
    load_credential,
    learning_metrics,
    list_events,
    mark_clicked,
    mark_notification_suppressed,
    mark_notified,
    list_notification_decisions,
    record_event_action,
    record_notification_decision,
    runtime_config,
    save_password_hash,
    save_json_setting,
    save_source_state,
    save_preferences,
    save_credential,
    save_subscription,
    set_reminder,
    clear_reminder,
    clear_context_signals,
    list_people,
    set_person_importance,
    list_packages,
    list_purchases,
    set_purchase_watch,
    list_game_events,
    list_plan_candidates,
    get_plan_candidate,
    update_plan_candidate,
    create_action_proposal,
    update_action_proposal,
    get_action_proposal,
    latest_action_for_plan,
    queue_action,
    next_queued_action,
    claim_action,
    complete_action,
    fail_action,
    create_capture,
    upsert_game_event,
    upsert_event,
)
from .worker import _payload
from .bedrock import classify_or_fallback, validate_config
from .actions import extract_plan_candidate, save_extracted_plan, validate_iphone_action
from .integrations import (
    PROVIDERS,
    active_context,
    create_oauth_state,
    discord_authorization_url,
    disconnect,
    exchange_oauth_code,
    google_authorization_url,
    mark_failure,
    mark_success,
    provider_status,
    sync_provider,
    normalize_companion_payload,
    normalize_location_payload,
    create_google_calendar_event,
)
from .storage import companion_device, create_pairing_challenge, list_companion_devices, redeem_pairing_challenge, save_integration, set_context_signal, update_companion_metadata

LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json_body() -> dict:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _store_context_payload(conn, payload: dict, source: str) -> None:
    # Keep physical location separate from sleep/focus state. Clear the
    # previous transient values from this source so an old Focus=true or
    # location cannot win simply because it remains in the table.
    for kind in ("mode", "physical_context", "state", "sleep", "focus", "location"):
        clear_context_signals(conn, kind=kind, source=source)
    expires_at = payload["expires_at"]
    set_context_signal(conn, "physical_context", {"mode": payload["physical_context"]}, source, payload["confidence"], expires_at)
    # Keep the legacy mode record for existing consumers while new clients use
    # physical_context and state explicitly.
    set_context_signal(conn, "mode", {"mode": payload["mode"]}, source, payload["confidence"], expires_at)
    set_context_signal(conn, "state", {"state": payload["state"]}, source, payload["confidence"], expires_at)
    for kind, value in payload["signals"].items():
        set_context_signal(conn, kind, value, source, payload["confidence"], expires_at)


def _valid_time(value: str) -> bool:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return hour in range(24) and minute in range(60)
    except (AttributeError, TypeError, ValueError):
        return False


def create_app(test_config: dict | None = None) -> Flask:
    config = load_config(test_config)
    init_db(config["DATABASE_PATH"], config["PASSWORD_HASH"])
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.secret_key = config["SECRET_KEY"] or config["SESSION_SECRET"]
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config["COOKIE_SECURE"],
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def db():
        if "pulse_db" not in request.environ:
            request.environ["pulse_db"] = connect(config["DATABASE_PATH"])
        return request.environ["pulse_db"]

    @app.teardown_request
    def close_db(_error):
        conn = request.environ.pop("pulse_db", None)
        if conn:
            conn.close()

    def auth_enabled() -> bool:
        return bool(config["PASSWORD_HASH"]) and not (config["ENV"] != "production" and config.get("DEV_NO_AUTH"))

    def active_password_hash() -> str:
        return get_password_hash(db(), config["PASSWORD_HASH"])

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if auth_enabled() and not session.get("pulse_authenticated"):
                return jsonify(error="authentication_required"), 401
            if not auth_enabled() and config["ENV"] == "production":
                return jsonify(error="authentication_not_configured"), 503
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def origin_guard():
        if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != config["APP_URL"].rstrip("/"):
                return jsonify(error="bad_origin"), 403

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify(error="not_found"), 404
        return send_from_directory(STATIC, "index.html")

    @app.errorhandler(500)
    def internal_error(error):
        LOGGER.exception("request failed: %s", error)
        if request.path.startswith("/api/"):
            return jsonify(error="internal_error"), 500
        return "Pulse is temporarily unavailable", 500

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, service="pulse", version="0.2.0", push_configured=push_configured(config), now=_now().isoformat())

    @app.get("/api/config")
    def public_config():
        return jsonify(
            app_url=config["APP_URL"],
            vapid_public_key=config["VAPID_PUBLIC_KEY"],
            push_configured=push_configured(config),
            auth_configured=auth_enabled(),
            discovery={"enabled": discovery_enabled(config), "provider": discovery_provider(config)[1], "interval_minutes": config["DISCOVERY_INTERVAL_MINUTES"]},
            bedrock_model_id=config.get("BEDROCK_MODEL_ID"),
            topics=[{"id": key, "label": TOPIC_LABELS.get(key, key)} for key in config["TOPIC_THRESHOLDS"]],
            ios={"minimum_version": "16.4", "requires_home_screen": True},
        )

    @app.get("/api/auth/status")
    def auth_status():
        return jsonify(authenticated=bool(session.get("pulse_authenticated")) or not auth_enabled(), configured=auth_enabled())

    @app.post("/api/auth/login")
    def login():
        if not auth_enabled():
            session["pulse_authenticated"] = True
            return jsonify(ok=True)
        password = str(_json_body().get("password", ""))
        if not password or not check_password_hash(active_password_hash(), password):
            return jsonify(error="invalid_credentials"), 401
        session.clear()
        session.permanent = True
        session["pulse_authenticated"] = True
        return jsonify(ok=True)

    @app.post("/api/auth/password")
    @require_auth
    def change_password():
        body = _json_body()
        current = body.get("current_password")
        new = body.get("new_password")
        confirmation = body.get("confirm_password")
        if not all(isinstance(value, str) for value in (current, new, confirmation)):
            return jsonify(error="invalid_password"), 400
        if len(new) < 8:
            return jsonify(error="password_too_short"), 400
        if len(new) > 256:
            return jsonify(error="password_too_long"), 400
        if new != confirmation:
            return jsonify(error="password_confirmation_mismatch"), 400
        current_hash = active_password_hash()
        if not current_hash or not check_password_hash(current_hash, current):
            return jsonify(error="invalid_current_password"), 400
        if check_password_hash(current_hash, new):
            return jsonify(error="password_unchanged"), 400
        save_password_hash(db(), generate_password_hash(new))
        return jsonify(ok=True)

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/discovery")
    @require_auth
    def discovery_settings():
        active = runtime_config(db(), config)
        return jsonify(
            enabled=discovery_enabled(config),
            provider=discovery_provider(config)[1],
            interval_minutes=config["DISCOVERY_INTERVAL_MINUTES"],
            profiles=active["SEARCH_PROFILES"],
            entities=active["TRACKED_ENTITIES"],
        )

    @app.put("/api/discovery")
    @require_auth
    def discovery_settings_update():
        body = _json_body()
        active = runtime_config(db(), config)
        for key, setting_key in (("profiles", "search_profiles"), ("entities", "tracked_entities")):
            if key not in body:
                continue
            value = body[key]
            if not isinstance(value, list) or len(value) > 30:
                return jsonify(error=f"invalid_{key}"), 400
            cleaned = []
            for item in value:
                if not isinstance(item, dict):
                    return jsonify(error=f"invalid_{key}"), 400
                item_id = str(item.get("id", "")).strip()[:80]
                name = str(item.get("label" if key == "profiles" else "name", "")).strip()[:120]
                topic = str(item.get("topic", "watcher")).strip()
                if not item_id or not name or topic not in TOPIC_LABELS:
                    return jsonify(error=f"invalid_{key}"), 400
                if key == "profiles":
                    queries = item.get("queries")
                    keywords = item.get("keywords", [])
                    if not isinstance(queries, list) or not 1 <= len(queries) <= 20 or not isinstance(keywords, list):
                        return jsonify(error="invalid_profiles"), 400
                    queries = [str(query).strip()[:300] for query in queries if str(query).strip()]
                    if not queries:
                        return jsonify(error="invalid_profiles"), 400
                    cleaned.append({
                        "id": item_id,
                        "label": name,
                        "topic": topic,
                        "queries": queries[:20],
                        "keywords": [str(word).strip()[:80] for word in keywords if str(word).strip()][:40],
                        "active": bool(item.get("active", True)),
                    })
                else:
                    aliases = item.get("aliases") or [name]
                    if not isinstance(aliases, list):
                        return jsonify(error="invalid_entities"), 400
                    try:
                        boost = max(0, min(40, int(item.get("boost", 0))))
                    except (TypeError, ValueError):
                        return jsonify(error="invalid_entities"), 400
                    cleaned.append({
                        "id": item_id,
                        "name": name,
                        "aliases": [str(alias).strip()[:120] for alias in aliases if str(alias).strip()][:20],
                        "topic": topic,
                        "boost": boost,
                    })
            if not cleaned:
                return jsonify(error=f"invalid_{key}"), 400
            save_json_setting(db(), setting_key, cleaned)
            active["SEARCH_PROFILES" if key == "profiles" else "TRACKED_ENTITIES"] = cleaned
        return jsonify(profiles=active["SEARCH_PROFILES"], entities=active["TRACKED_ENTITIES"])

    @app.get("/api/integrations")
    @require_auth
    def integrations_status():
        return jsonify(integrations=provider_status(db(), config), context=active_context(db()), devices=list_companion_devices(db()))

    @app.post("/api/integrations/<provider>/disconnect")
    @require_auth
    def integration_disconnect(provider: str):
        if provider not in PROVIDERS:
            return jsonify(error="unknown_integration"), 404
        disconnect(db(), provider)
        db().commit()
        return jsonify(ok=True, integrations=provider_status(db(), config))

    @app.post("/api/integrations/<provider>/sync")
    @require_auth
    def integration_sync(provider: str):
        if provider not in PROVIDERS:
            return jsonify(error="unknown_integration"), 404
        try:
            result = sync_provider(db(), config, provider)
            return jsonify(ok=True, result=result, integrations=provider_status(db(), config))
        except Exception as exc:
            mark_failure(db(), provider, type(exc).__name__)
            db().commit()
            return jsonify(error="integration_sync_failed", detail="provider sync failed", integrations=provider_status(db(), config)), 502

    @app.post("/api/integrations/<provider>/test")
    @require_auth
    def integration_test(provider: str):
        if provider not in PROVIDERS:
            return jsonify(error="unknown_integration"), 404
        if provider == "aws-bedrock":
            validation = validate_config(config)
            if not validation["valid"]:
                return jsonify(ok=False, metadata={"success": False, "configuration": validation, "safe_to_continue": True}, integrations=provider_status(db(), config)), 503
            value, metadata = classify_or_fallback(config, "Return a minimal JSON health response.", {"service": "Pulse", "check": "connectivity"}, {"status": "string"})
            if not value:
                mark_failure(db(), provider, metadata.get("error_code") or metadata.get("error", "request_failed"))
                db().commit()
                return jsonify(ok=False, metadata=metadata, integrations=provider_status(db(), config)), 502
            mark_success(db(), provider, {"model": metadata.get("model"), "latency_ms": metadata.get("latency_ms")})
            db().commit()
            return jsonify(ok=True, metadata=metadata, integrations=provider_status(db(), config))
        if provider in {"google", "gmail", "calendar", "discord", "apple-calendar", "apple-contacts", "apple-mail"}:
            credential_provider = "google" if provider in {"google", "gmail", "calendar"} else "icloud" if provider.startswith("apple-") else provider
            has_credential = bool(db().execute("SELECT 1 FROM integration_credentials WHERE provider=?", (credential_provider,)).fetchone())
            if provider.startswith("apple-") and has_credential:
                try:
                    result = sync_provider(db(), config, provider)
                    db().commit()
                    return jsonify(ok=True, result=result, integrations=provider_status(db(), config))
                except Exception as exc:
                    mark_failure(db(), provider, type(exc).__name__)
                    db().commit()
                    return jsonify(ok=False, error="apple_sync_failed", integrations=provider_status(db(), config)), 502
            return jsonify(ok=has_credential, configured=has_credential, integrations=provider_status(db(), config))
        return jsonify(ok=True, configured=True, integrations=provider_status(db(), config))

    @app.get("/api/integrations/google/connect")
    @require_auth
    def google_connect():
        if not config.get("GOOGLE_CLIENT_ID") or not config.get("GOOGLE_CLIENT_SECRET"):
            return jsonify(error="google_oauth_not_configured"), 503
        state = create_oauth_state(db(), "google")
        return redirect(google_authorization_url(config, state))

    @app.get("/api/integrations/discord/connect")
    @require_auth
    def discord_connect():
        if not config.get("DISCORD_CLIENT_ID") or not config.get("DISCORD_CLIENT_SECRET"):
            return jsonify(error="discord_oauth_not_configured"), 503
        state = create_oauth_state(db(), "discord")
        return redirect(discord_authorization_url(config, state))

    @app.get("/api/integrations/apple/setup")
    @require_auth
    def apple_setup_status():
        credential = load_credential(db(), "icloud") or {}
        return jsonify(configured=bool(credential.get("apple_id") and credential.get("app_password")))

    @app.post("/api/integrations/apple/setup")
    @require_auth
    def apple_setup_save():
        body = _json_body()
        apple_id = str(body.get("apple_id", "")).strip()[:240]
        app_password = str(body.get("app_password", "")).strip()[:240]
        if "@" not in apple_id or len(app_password) < 8:
            return jsonify(error="apple_id_and_app_specific_password_required"), 400
        save_credential(db(), "icloud", {"apple_id": apple_id, "app_password": app_password})
        for provider in ("apple-calendar", "apple-contacts", "apple-mail"):
            save_integration(db(), provider, PROVIDERS[provider], enabled=False, connection_state="needs_setup", authorization_state="authorized", last_error="")
        db().commit()
        return jsonify(ok=True, configured=True, integrations=provider_status(db(), config))

    @app.post("/api/integrations/apple/disconnect")
    @require_auth
    def apple_disconnect():
        db().execute("DELETE FROM integration_credentials WHERE provider='icloud'")
        for provider in ("apple-calendar", "apple-contacts", "apple-mail"):
            disconnect(db(), provider)
        db().commit()
        return jsonify(ok=True, integrations=provider_status(db(), config))

    @app.get("/api/integrations/<provider>/callback")
    def integration_callback(provider: str):
        if provider not in {"google", "discord"}:
            return jsonify(error="unknown_integration"), 404
        code, state = request.args.get("code", ""), request.args.get("state", "")
        if not code or not state:
            return jsonify(error="invalid_or_expired_oauth_state"), 400
        try:
            exchange_oauth_code(db(), config, provider, code, state)
        except Exception:
            return jsonify(error="oauth_exchange_failed"), 502
        return redirect("/integrations?connected=" + provider)

    @app.get("/api/context")
    @require_auth
    def context_status():
        return jsonify(context=active_context(db()))

    def shortcut_token():
        stored = load_credential(db(), "shortcut") or {}
        return str(stored.get("token") or config.get("SHORTCUT_TOKEN") or "")

    @app.get("/api/shortcut/setup")
    @require_auth
    def shortcut_setup():
        token = shortcut_token()
        if not token:
            return jsonify(error="shortcut_not_configured"), 503
        return jsonify(endpoint=config["APP_URL"] + "/api/shortcut/context", capture_endpoint=config["APP_URL"] + "/api/shortcut/capture", runner_source_endpoint=config["APP_URL"] + "/api/shortcut/runner-source", token=token)

    @app.post("/api/shortcut/token/rotate")
    @require_auth
    def shortcut_rotate():
        token = secrets.token_urlsafe(32)
        save_credential(db(), "shortcut", {"token": token})
        return jsonify(ok=True, endpoint=config["APP_URL"] + "/api/shortcut/context", capture_endpoint=config["APP_URL"] + "/api/shortcut/capture", runner_source_endpoint=config["APP_URL"] + "/api/shortcut/runner-source", token=token)

    @app.get("/api/shortcut/runner-source")
    @require_auth
    def shortcut_runner_source():
        source_path = Path(__file__).resolve().parent.parent / "shortcuts" / "pulse-action-runner.cherri"
        return make_response(source_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8", "Content-Disposition": "attachment; filename=pulse-action-runner.cherri"})

    @app.post("/api/location")
    @require_auth
    def browser_location():
        try:
            location = normalize_location_payload(_json_body(), config["LOCATION_MAX_AGE_MINUTES"])
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        set_context_signal(db(), "location", location["value"], "browser-location", location["confidence"], location["expires_at"])
        db().commit()
        return jsonify(ok=True, location={"accuracy_m": location["value"]["accuracy_m"], "coarse": True, "expires_at": location["expires_at"]}, context=active_context(db()))

    @app.delete("/api/location")
    @require_auth
    def clear_browser_location():
        clear_context_signals(db(), kind="location", source="browser-location")
        db().commit()
        return jsonify(ok=True, context=active_context(db()))

    @app.post("/api/companion/pair/start")
    @require_auth
    def companion_pair_start():
        challenge_id, code = create_pairing_challenge(db())
        return jsonify(challenge_id=challenge_id, code=code, expires_in_minutes=10)

    @app.post("/api/companion/pair/redeem")
    def companion_pair_redeem():
        body = _json_body()
        result = redeem_pairing_challenge(db(), str(body.get("code", "")), str(body.get("label", "Pulse iPhone")))
        if not result:
            return jsonify(error="invalid_or_expired_pairing_code"), 400
        device_id, token = result
        return jsonify(device_id=device_id, token=token)

    @app.post("/api/companion/context")
    def companion_context():
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        device = companion_device(db(), token) if token else None
        if not device:
            return jsonify(error="companion_auth_required"), 401
        try:
            payload = normalize_companion_payload(_json_body())
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        _store_context_payload(db(), payload, "apple-companion")
        previous = json.loads(device["metadata"] or "{}") if device["metadata"] else {}
        previous.update({"permissions": payload["permissions"], "last_payload_at": _now().isoformat(), "last_mode": payload["mode"]})
        update_companion_metadata(db(), device["id"], previous)
        save_integration(db(), "apple", "Apple companion", enabled=True, connection_state="connected", authorization_state="authorized", last_success_at=_now().isoformat(), last_attempted_at=_now().isoformat(), last_error="", metadata={"last_context_mode": payload["mode"], "permissions": payload["permissions"]})
        db().commit()
        return jsonify(ok=True, context=active_context(db()))

    @app.post("/api/priorities")
    @require_auth
    def priorities_update():
        body = _json_body()
        priorities = body.get("personal_priorities", {})
        temporary = body.get("temporary_priority", {})
        if not isinstance(priorities, dict) or not isinstance(temporary, dict):
            return jsonify(error="invalid_priorities"), 400
        cleaned = {}
        for topic, value in priorities.items():
            if topic not in TOPIC_LABELS and topic not in PERSONAL_PRIORITY_CATEGORIES:
                continue
            try:
                cleaned[topic] = max(-20, min(20, int(value)))
            except (TypeError, ValueError):
                return jsonify(error="invalid_priority"), 400
        cleaned_temporary = {}
        for topic, value in temporary.items():
            if topic not in TOPIC_LABELS or not isinstance(value, dict):
                continue
            try:
                boost = max(-20, min(20, int(value.get("boost", 0))))
                expires_at = datetime.fromisoformat(str(value.get("expires_at", "")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return jsonify(error="invalid_temporary_priority"), 400
            if expires_at > _now():
                cleaned_temporary[topic] = {"boost": boost, "expires_at": expires_at.replace(microsecond=0).isoformat()}
        prefs = get_preferences(db(), config)
        prefs["personal_priorities"], prefs["temporary_priority"] = cleaned, cleaned_temporary
        save_preferences(db(), prefs)
        return jsonify(priorities={"personal_priorities": cleaned, "temporary_priority": cleaned_temporary})

    @app.get("/api/learning")
    @require_auth
    def learning():
        preferences = get_preferences(db(), config)
        return jsonify(
            summary=event_action_summary(db()),
            metrics=learning_metrics(db()),
            followed_entities=preferences.get("followed_entities", {}),
            followed_stories=preferences.get("followed_stories", {}),
            less_like_entities=preferences.get("less_like_entities", {}),
            less_like_topics=preferences.get("less_like_topics", {}),
            learned_topic_weights=preferences.get("learned_topic_weights", {}),
            learned_source_weights=preferences.get("learned_source_weights", {}),
        )

    @app.delete("/api/learning")
    @require_auth
    def learning_reset():
        clear_learning(db())
        return jsonify(ok=True)

    @app.get("/api/events")
    @require_auth
    def events():
        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError:
            limit = 30
        return jsonify(events=list_events(db(), limit, request.args.get("topic")))

    @app.get("/api/plans")
    @require_auth
    def plans():
        status = str(request.args.get("status", "")).strip() or None
        return jsonify(plans=list_plan_candidates(db(), status, request.args.get("limit", "50")))

    def receive_capture(source: str):
        body = _json_body()
        title = str(body.get("title") or body.get("subject") or "").strip()[:240]
        text = str(body.get("text") or body.get("body") or body.get("content") or "").strip()[:12000]
        url = str(body.get("url") or "").strip()[:1000]
        if not title and not text and not url:
            return jsonify(error="capture_content_required"), 400
        plan = extract_plan_candidate({"id": body.get("id", ""), "subject": title, "snippet": text, "url": url}, source, config)
        if plan:
            plan = save_extracted_plan(db(), plan)
        capture = create_capture(db(), {"source": source, "title": title, "body": text, "url": url, "metadata": {"shared": True}, "plan_id": plan["id"] if plan else ""})
        db().commit()
        return jsonify(ok=True, capture=capture, plan=plan), 201

    @app.post("/api/capture")
    @require_auth
    def capture():
        return receive_capture(str(_json_body().get("source") or "share_sheet"))

    @app.post("/api/plans/<plan_id>/approve")
    @require_auth
    def plan_approve(plan_id: str):
        plan = get_plan_candidate(db(), plan_id)
        if not plan:
            return jsonify(error="not_found"), 404
        if plan["status"] not in {"proposed", "approved"}:
            return jsonify(error="plan_not_actionable"), 409
        body = _json_body()
        target = str(body.get("target") or "google").strip()
        if target in {"apple_calendar", "apple_reminder"}:
            action_type = "create_calendar_event" if target == "apple_calendar" else "create_reminder"
            source_payload = plan.get("action_payload") or {}
            payload = {
                "title": source_payload.get("summary") or plan.get("title"),
                "start": plan.get("start_at"),
                "end": plan.get("end_at"),
                "location": source_payload.get("location") or plan.get("location"),
                "notes": source_payload.get("description") or plan.get("summary"),
                "due": plan.get("start_at"),
                "list": body.get("list", ""),
                "priority": body.get("priority", 0),
            }
            if action_type == "create_reminder":
                payload = {key: payload[key] for key in ("title", "due", "list", "notes", "priority")}
            else:
                payload = {key: payload[key] for key in ("title", "start", "end", "location", "notes", "calendar_name") if key in payload}
            try:
                normalized = validate_iphone_action(action_type, payload)
            except (TypeError, ValueError) as exc:
                return jsonify(error=str(exc)), 400
            normalized["pulse_action_id"] = "pending"
            action = create_action_proposal(db(), plan, risk="confirm", action_type=action_type, payload=normalized, status="approved", target=target, idempotency_key=f"plan:{plan_id}:{target}")
            normalized["pulse_action_id"] = action["id"]
            db().execute("UPDATE action_proposals SET payload=? WHERE id=?", (json.dumps(normalized, separators=(",", ":")), action["id"]))
            action = queue_action(db(), action["id"])
            update_plan_candidate(db(), plan_id, "approved")
            db().commit()
            return jsonify(ok=True, queued=True, plan=get_plan_candidate(db(), plan_id), action=action), 202
        if target != "google":
            return jsonify(error="unsupported_action_target"), 400
        action = create_action_proposal(db(), plan, risk="confirm")
        try:
            if plan["action_type"] != "calendar_event":
                raise ValueError("unsupported_action")
            result = create_google_calendar_event(db(), config, plan)
            action = update_action_proposal(db(), action["id"], "completed", result)
            update_plan_candidate(db(), plan_id, "executed")
            db().commit()
            return jsonify(ok=True, plan=get_plan_candidate(db(), plan_id), action=action)
        except Exception as exc:
            action = update_action_proposal(db(), action["id"], "failed", error=type(exc).__name__)
            update_plan_candidate(db(), plan_id, "approved")
            db().commit()
            return jsonify(error="action_failed", detail="calendar action failed; reconnect Google if needed", plan=get_plan_candidate(db(), plan_id), action=action), 502

    @app.post("/api/plans/<plan_id>/reject")
    @require_auth
    def plan_reject(plan_id: str):
        plan = update_plan_candidate(db(), plan_id, "rejected")
        if not plan:
            return jsonify(error="not_found"), 404
        db().commit()
        return jsonify(ok=True, plan=plan)

    @app.post("/api/shortcut/test-action")
    @require_auth
    def shortcut_test_action():
        action_type = str(_json_body().get("action_type") or "").strip()
        start = (_now() + timedelta(hours=1)).isoformat()
        end = (_now() + timedelta(hours=2)).isoformat()
        if action_type == "create_calendar_event":
            payload = {"title": "Pulse test event", "start": start, "end": end, "location": "", "notes": "Safe test created by Pulse.", "calendar_name": ""}
        elif action_type == "create_reminder":
            payload = {"title": "Pulse test reminder", "due": start, "list": "", "notes": "Safe test created by Pulse.", "priority": 0}
        else:
            return jsonify(error="unsupported_action_type"), 400
        try:
            payload = validate_iphone_action(action_type, payload)
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        plan = {"id": "test-plan-" + secrets.token_urlsafe(10), "action_type": action_type, "action_payload": payload}
        action = create_action_proposal(db(), plan, risk="confirm", action_type=action_type, payload=payload, status="approved", target="apple", idempotency_key="test:" + secrets.token_urlsafe(18))
        payload["pulse_action_id"] = action["id"]
        db().execute("UPDATE action_proposals SET payload=? WHERE id=?", (json.dumps(payload, separators=(",", ":")), action["id"]))
        action = queue_action(db(), action["id"])
        db().commit()
        return jsonify(ok=True, action=action), 202

    @app.get("/api/packages")
    @require_auth
    def packages():
        return jsonify(packages=list_packages(db()))

    @app.get("/api/purchases")
    @require_auth
    def purchases():
        return jsonify(purchases=list_purchases(db()))

    @app.patch("/api/purchases/<purchase_id>/watch")
    @require_auth
    def purchase_watch(purchase_id: str):
        try:
            item = set_purchase_watch(db(), purchase_id, str(_json_body().get("priority", "normal")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not item:
            return jsonify(error="not_found"), 404
        db().commit()
        return jsonify(purchase=item)

    @app.get("/api/game-events")
    @require_auth
    def game_events():
        return jsonify(events=list_game_events(db()))

    @app.post("/api/game-events")
    @require_auth
    def game_event_create():
        body = _json_body()
        try:
            item = upsert_game_event(db(), body.get("game", ""), body.get("title", ""), body.get("starts_at", ""), body.get("kind", "event"), body.get("url", ""), body.get("id", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        db().commit()
        return jsonify(event=item), 201

    @app.delete("/api/game-events/<event_id>")
    @require_auth
    def game_event_delete(event_id: str):
        cursor = db().execute("UPDATE game_events SET active=0,updated_at=? WHERE id=?", (_now().isoformat(), event_id))
        if not cursor.rowcount:
            return jsonify(error="not_found"), 404
        db().commit()
        return jsonify(ok=True)

    @app.get("/api/events/<event_id>")
    @require_auth
    def event_detail(event_id: str):
        event = get_event(db(), event_id)
        if not event:
            return jsonify(error="not_found"), 404
        return jsonify(event=event)

    @app.post("/api/events/<event_id>/opened")
    @require_auth
    def event_opened(event_id: str):
        if not get_event(db(), event_id):
            return jsonify(error="not_found"), 404
        mark_clicked(db(), event_id)
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/events/<event_id>/source-clicked")
    @require_auth
    def event_source_clicked(event_id: str):
        event = get_event(db(), event_id)
        if not event:
            return jsonify(error="not_found"), 404
        record_event_action(db(), event_id, "source_clicked", event.get("url", ""))
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/events/<event_id>/draft-reply")
    @require_auth
    def event_draft_reply(event_id: str):
        event = get_event(db(), event_id)
        if not event:
            return jsonify(error="not_found"), 404
        metadata = event.get("metadata") or {}
        if not (metadata.get("sender") and (metadata.get("gmail_message_id") or metadata.get("mail_message_id"))):
            return jsonify(error="not_a_message_event"), 400
        tone = str(_json_body().get("tone", "brief and friendly")).strip()[:80] or "brief and friendly"
        payload = {
            "sender": str(metadata.get("sender", ""))[:160],
            "subject": str(event.get("title", ""))[:240],
            "summary": str(event.get("summary", ""))[:700],
            "topic": str(event.get("topic", ""))[:80],
            "tone": tone,
        }
        value, ai_metadata = classify_or_fallback(
            config,
            "Draft a short reply for Aiden to review. Never claim it was sent. Use only the supplied subject and summary. Return a practical draft, not analysis.",
            payload,
            {"draft": "string", "needs_review": "boolean"},
        )
        if value and str(value.get("draft", "")).strip():
            return jsonify(draft=str(value["draft"]).strip()[:2400], needs_review=True, generated_by=ai_metadata.get("model", "luna"), metadata={"safe_to_continue": True})
        fallback = {
            "package": "Hi, thanks for the update. Could you confirm the latest delivery details?",
            "security": "Thanks for letting me know. I’ll review this and follow up if I need anything else.",
            "school": "Hi, thanks for the update. I’ll review the details and get back to you shortly.",
            "travel": "Thanks for the update. I’ll review the details and confirm what I need to do next.",
        }.get(event.get("topic"), "Hi, thanks for the message. I’ll review this and get back to you shortly.")
        return jsonify(draft=fallback, needs_review=True, generated_by="deterministic fallback", metadata={"safe_to_continue": True, "ai_failed": True})

    @app.post("/api/events/<event_id>/feedback")
    @require_auth
    def event_feedback(event_id: str):
        event = get_event(db(), event_id)
        if not event:
            return jsonify(error="not_found"), 404
        body = _json_body()
        action = body.get("action")
        metadata = event.get("metadata") or {}
        entity_ids = set(metadata.get("entities") or [])
        entity_id = str(body.get("entity_id", "")).strip()
        if action == "follow":
            if entity_id not in entity_ids:
                return jsonify(error="unknown_entity"), 400
            prefs = get_preferences(db(), config)
            prefs["followed_entities"][entity_id] = _now().isoformat()
            save_preferences(db(), prefs)
            record_event_action(db(), event_id, "follow", entity_id)
        elif action == "follow_story":
            story_id = event.get("canonical_event_id") or event.get("cluster_id")
            if not story_id:
                return jsonify(error="story_unavailable"), 400
            prefs = get_preferences(db(), config)
            prefs.setdefault("followed_stories", {})[story_id] = _now().isoformat()
            save_preferences(db(), prefs)
            record_event_action(db(), event_id, "follow_story", story_id)
        elif action == "less_like":
            prefs = get_preferences(db(), config)
            if entity_id:
                if entity_id not in entity_ids:
                    return jsonify(error="unknown_entity"), 400
                prefs["less_like_entities"][entity_id] = _now().isoformat()
                record_event_action(db(), event_id, "less_like", entity_id)
            else:
                prefs["less_like_topics"][event["topic"]] = _now().isoformat()
                record_event_action(db(), event_id, "less_like", event["topic"])
            save_preferences(db(), prefs)
        elif action in {"useful", "not_useful", "too_late"}:
            record_event_action(db(), event_id, action)
        else:
            return jsonify(error="invalid_feedback"), 400
        db().commit()
        return jsonify(ok=True, preferences=get_preferences(db(), config))

    @app.get("/api/people")
    @require_auth
    def people_list():
        return jsonify(people=list_people(db()))

    @app.patch("/api/people/<person_id>")
    @require_auth
    def people_importance(person_id: str):
        try:
            item = set_person_importance(db(), person_id, str(_json_body().get("importance", "normal")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not item:
            return jsonify(error="not_found"), 404
        db().commit()
        return jsonify(person=item)

    @app.post("/api/events/<event_id>/remind")
    @require_auth
    def event_remind(event_id: str):
        minutes = _json_body().get("minutes", 60)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return jsonify(error="invalid_minutes"), 400
        if minutes not in {15, 30, 60, 120, 1440}:
            return jsonify(error="invalid_minutes"), 400
        event = set_reminder(db(), event_id, minutes)
        if not event:
            return jsonify(error="not_found"), 404
        record_event_action(db(), event_id, "remind", str(minutes))
        db().commit()
        return jsonify(event=event)

    @app.get("/api/audit")
    @require_auth
    def notification_audit():
        try:
            limit = max(1, min(200, int(request.args.get("limit", "100"))))
        except ValueError:
            limit = 100
        near_only = request.args.get("near_threshold", "0").lower() in {"1", "true", "yes"}
        suppressed_only = request.args.get("suppressed_only", "0").lower() in {"1", "true", "yes"}
        return jsonify(decisions=list_notification_decisions(db(), limit, near_only, suppressed_only))

    @app.post("/api/debug/simulate")
    @require_auth
    def notification_simulator():
        body = _json_body()
        conn = db()
        if body.get("event_id"):
            event = get_event(conn, str(body["event_id"]))
            if not event:
                return jsonify(error="not_found"), 404
        else:
            topic = str(body.get("topic", "watcher")).strip()
            title = str(body.get("title", "")).strip()[:240]
            summary = str(body.get("summary", "")).strip()[:700]
            if topic not in TOPIC_LABELS or not title:
                return jsonify(error="topic_and_title_required"), 400
            runtime = runtime_config(conn, config)
            keywords = body.get("keywords") if isinstance(body.get("keywords"), list) else []
            if not keywords:
                for profile in runtime.get("SEARCH_PROFILES", []):
                    if profile.get("topic") == topic:
                        keywords.extend(profile.get("keywords") or [])
            keywords = [str(item) for item in keywords if str(item).strip()] or list(significant_tokens(title))[:8]
            score, relevant, reason = score_item(topic, title, summary, keywords)
            event = {
                "id": "simulation",
                "source_id": "simulator",
                "source_kind": "simulator",
                "topic": topic,
                "title": title,
                "summary": summary,
                "body": reason,
                "url": str(body.get("url", "https://pulse.moralife.uk/")),
                "canonical_key": "simulation",
                "published_at": _now().isoformat(),
                "discovered_at": _now().isoformat(),
                "score": score,
                "priority": "critical" if score >= 90 else "high" if score >= 80 else "normal" if score >= 65 else "low",
                "relevant": relevant,
                "metadata": {"source_trust": str(body.get("source_trust", "reliable_secondary")), "sources": []},
            }
            annotate_candidate(event, runtime.get("TRACKED_ENTITIES", []))
            event["cluster_id"] = cluster_id_for(event)
            apply_preference_adjustments(event, get_preferences(conn, config))
        preferences = get_preferences(conn, config)
        allowed, reason, trace = evaluate_notification(event, preferences, _now(), config, conn)
        trace["simulation"] = True
        trace["would_send"] = allowed
        return jsonify(event=event, allowed=allowed, reason=reason, trace=trace, would_send=allowed, push_sent=False)

    @app.delete("/api/events/<event_id>/remind")
    @require_auth
    def event_remind_clear(event_id: str):
        if not get_event(db(), event_id):
            return jsonify(error="not_found"), 404
        clear_reminder(db(), event_id)
        record_event_action(db(), event_id, "remind_clear")
        db().commit()
        return jsonify(ok=True)

    @app.get("/api/preferences")
    @require_auth
    def preferences():
        return jsonify(preferences=get_preferences(db(), config))

    @app.put("/api/preferences")
    @require_auth
    def preferences_update():
        body = _json_body()
        prefs = get_preferences(db(), config)
        for key in ("quiet_start", "quiet_end"):
            if key in body:
                if not _valid_time(str(body[key])):
                    return jsonify(error=f"invalid_{key}"), 400
                prefs[key] = str(body[key])
        if "topic_thresholds" in body:
            if not isinstance(body["topic_thresholds"], dict):
                return jsonify(error="invalid_topic_thresholds"), 400
            for topic, value in body["topic_thresholds"].items():
                if topic not in TOPIC_LABELS:
                    continue
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return jsonify(error="invalid_topic_threshold"), 400
                if value < 0 or value > 100:
                    return jsonify(error="invalid_topic_threshold"), 400
                prefs["topic_thresholds"][topic] = value
        save_preferences(db(), prefs)
        return jsonify(preferences=prefs)

    @app.post("/api/topics/<topic>/mute")
    @require_auth
    def topic_mute(topic: str):
        if topic not in TOPIC_LABELS:
            return jsonify(error="unknown_topic"), 404
        body = _json_body()
        days = body.get("days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            return jsonify(error="invalid_days"), 400
        prefs = get_preferences(db(), config)
        if days == 0:
            prefs["muted_topics"].pop(topic, None)
        elif days in {1, 7, 30}:
            prefs["muted_topics"][topic] = (_now() + timedelta(days=days)).isoformat()
        else:
            return jsonify(error="invalid_days"), 400
        record_event_action(db(), "topic:" + topic, "mute" if days else "unmute", str(days))
        save_preferences(db(), prefs)
        return jsonify(preferences=prefs)

    @app.post("/api/subscriptions")
    @require_auth
    def subscribe():
        body = _json_body()
        endpoint = body.get("endpoint")
        keys = body.get("keys")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
            return jsonify(error="invalid_endpoint"), 400
        if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
            return jsonify(error="invalid_keys"), 400
        sub_id = save_subscription(db(), body, request.headers.get("User-Agent", ""))
        return jsonify(id=sub_id, ok=True)

    @app.delete("/api/subscriptions/<sub_id>")
    @require_auth
    def unsubscribe(sub_id: str):
        delete_subscription(db(), sub_id)
        return jsonify(ok=True)

    @app.post("/api/push/test")
    @require_auth
    def test_push():
        event = create_manual_event(db(), config["APP_URL"], "Pulse test received", "Tap this notification to open the exact event page.")
        result = send_payload(db(), config, _payload(config, event))
        mark_notified(db(), event["id"])
        db().commit()
        return jsonify(event=event, delivery=result)

    @app.post("/api/shortcut/check")
    def shortcut_check():
        token = shortcut_token()
        if not token or request.headers.get("X-Pulse-Shortcut-Token") != token:
            return jsonify(error="shortcut_not_configured"), 403
        conn = db()
        prefs = get_preferences(conn, config)
        pending = list_events(conn, 10)
        return jsonify(
            generated_at=_now().isoformat(),
            context=_json_body(),
            events=[item for item in pending if item["priority"] in {"critical", "high"}],
            preferences={"quiet_start": prefs["quiet_start"], "quiet_end": prefs["quiet_end"]},
        )

    @app.post("/api/shortcut/context")
    def shortcut_context():
        token = shortcut_token()
        if not token or request.headers.get("X-Pulse-Shortcut-Token") != token:
            return jsonify(error="shortcut_not_configured"), 403
        try:
            payload = normalize_companion_payload(_json_body())
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        _store_context_payload(db(), payload, "shortcut")
        db().commit()
        return jsonify(ok=True, context=active_context(db()))

    def shortcut_action_auth() -> bool:
        token = shortcut_token()
        return bool(token and hmac.compare_digest(request.headers.get("X-Pulse-Shortcut-Token", ""), token))

    @app.get("/api/shortcut/actions/next")
    def shortcut_action_next():
        if not shortcut_action_auth():
            return jsonify(error="shortcut_not_configured"), 403
        action = next_queued_action(db())
        db().commit()
        return jsonify(action=action)

    @app.post("/api/shortcut/actions/<action_id>/claim")
    def shortcut_action_claim(action_id: str):
        if not shortcut_action_auth():
            return jsonify(error="shortcut_not_configured"), 403
        action = claim_action(db(), action_id, request.headers.get("X-Pulse-Action-Claim", ""))
        db().commit()
        if not action:
            return jsonify(error="action_not_claimable"), 409
        return jsonify(action=action)

    @app.post("/api/shortcut/actions/<action_id>/complete")
    def shortcut_action_complete(action_id: str):
        if not shortcut_action_auth():
            return jsonify(error="shortcut_not_configured"), 403
        current = get_action_proposal(db(), action_id)
        if not current:
            return jsonify(error="not_found"), 404
        if current["status"] == "completed":
            return jsonify(action=current)
        if current["status"] != "claimed":
            return jsonify(error="action_not_claimed"), 409
        if not hmac.compare_digest(current.get("claim_token", ""), request.headers.get("X-Pulse-Action-Claim", "")):
            return jsonify(error="action_claim_mismatch"), 409
        result = _json_body().get("result", {})
        if not isinstance(result, dict) or len(json.dumps(result, separators=(",", ":"))) > 4000:
            return jsonify(error="invalid_action_result"), 400
        action = complete_action(db(), action_id, result)
        db().commit()
        return jsonify(action=action)

    @app.post("/api/shortcut/actions/<action_id>/fail")
    def shortcut_action_fail(action_id: str):
        if not shortcut_action_auth():
            return jsonify(error="shortcut_not_configured"), 403
        current = get_action_proposal(db(), action_id)
        if not current:
            return jsonify(error="not_found"), 404
        if current["status"] == "failed":
            return jsonify(action=current)
        if current["status"] not in {"queued", "claimed"}:
            return jsonify(error="action_not_failible"), 409
        if current["status"] == "claimed" and not hmac.compare_digest(current.get("claim_token", ""), request.headers.get("X-Pulse-Action-Claim", "")):
            return jsonify(error="action_claim_mismatch"), 409
        error = str(_json_body().get("error") or "shortcut_action_failed").strip()[:240]
        action = fail_action(db(), action_id, error)
        db().commit()
        return jsonify(action=action)

    @app.post("/api/shortcut/capture")
    def shortcut_capture():
        token = shortcut_token()
        if not token or request.headers.get("X-Pulse-Shortcut-Token") != token:
            return jsonify(error="shortcut_not_configured"), 403
        return receive_capture(str(_json_body().get("source") or "ios_share_sheet"))

    @app.post("/api/webhooks/github")
    def github_webhook():
        secret = config.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            return jsonify(error="github_webhook_not_configured"), 503
        raw = request.get_data(cache=True)
        supplied = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return jsonify(error="invalid_signature"), 401
        delivery = request.headers.get("X-GitHub-Delivery", "").strip()
        event_type = request.headers.get("X-GitHub-Event", "").strip()
        if not delivery or not event_type:
            return jsonify(error="missing_github_headers"), 400
        delivery_key = "github:webhook:delivery:" + delivery
        if get_source_state(db(), delivery_key):
            return jsonify(ok=True, duplicate=True)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="invalid_json"), 400
        candidate = github_webhook_candidate(payload, event_type, config)
        save_source_state(db(), delivery_key, {"event": event_type, "received_at": _now().isoformat()})
        if not candidate:
            db().commit()
            return jsonify(ok=True, ignored=True)
        preferences = get_preferences(db(), config)
        apply_preference_adjustments(candidate, preferences)
        candidate["created_from"] = "github_webhook"
        event, created = upsert_event(db(), candidate)
        db().commit()
        delivered = False
        if created:
            allowed, reason, trace = evaluate_notification(event, preferences, _now(), config, db())
            record_notification_decision(db(), event, allowed, reason, trace)
            if allowed:
                result = send_payload(db(), config, _payload(config, event))
                mark_notified(db(), event["id"])
                if result.get("sent", 0):
                    record_event_action(db(), event["id"], "delivered", str(result["sent"]))
                    delivered = True
            elif reason not in {"quiet hours", "topic or event cooldown"}:
                mark_notification_suppressed(db(), event["id"], reason)
            db().commit()
        return jsonify(ok=True, created=created, delivered=delivered, event_id=event["id"])

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        response = make_response(send_from_directory(STATIC, "sw.js", mimetype="application/javascript"))
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/event/<event_id>")
    def event_route(event_id: str):
        return send_from_directory(STATIC, "index.html")

    return app
