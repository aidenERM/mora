from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, make_response, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from .config import STATIC, TOPIC_LABELS, load_config
from .push import configured as push_configured, send_payload
from .storage import (
    connect,
    create_manual_event,
    delete_subscription,
    get_event,
    get_preferences,
    init_db,
    list_events,
    mark_clicked,
    save_preferences,
    save_subscription,
    set_reminder,
    clear_reminder,
)
from .worker import _payload

LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json_body() -> dict:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _valid_time(value: str) -> bool:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return hour in range(24) and minute in range(60)
    except (AttributeError, TypeError, ValueError):
        return False


def create_app(test_config: dict | None = None) -> Flask:
    config = load_config(test_config)
    init_db(config["DATABASE_PATH"])
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
        return jsonify(ok=True, service="pulse", version="0.1.0", push_configured=push_configured(config), now=_now().isoformat())

    @app.get("/api/config")
    def public_config():
        return jsonify(
            app_url=config["APP_URL"],
            vapid_public_key=config["VAPID_PUBLIC_KEY"],
            push_configured=push_configured(config),
            auth_configured=auth_enabled(),
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
        if not password or not check_password_hash(config["PASSWORD_HASH"], password):
            return jsonify(error="invalid_credentials"), 401
        session.clear()
        session.permanent = True
        session["pulse_authenticated"] = True
        return jsonify(ok=True)

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/events")
    @require_auth
    def events():
        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError:
            limit = 30
        return jsonify(events=list_events(db(), limit, request.args.get("topic")))

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
        return jsonify(event=event)

    @app.delete("/api/events/<event_id>/remind")
    @require_auth
    def event_remind_clear(event_id: str):
        if not get_event(db(), event_id):
            return jsonify(error="not_found"), 404
        clear_reminder(db(), event_id)
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
        db().execute("UPDATE events SET notified_at=? WHERE id=?", (_now().isoformat(), event["id"]))
        db().commit()
        return jsonify(event=event, delivery=result)

    @app.post("/api/shortcut/check")
    def shortcut_check():
        if not config["SHORTCUT_TOKEN"] or request.headers.get("X-Pulse-Shortcut-Token") != config["SHORTCUT_TOKEN"]:
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
