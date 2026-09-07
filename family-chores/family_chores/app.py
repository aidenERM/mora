from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chores(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', suggested_time TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, chore_id INTEGER NOT NULL, weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6), week TEXT NOT NULL CHECK(week IN ('A','B')), notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, FOREIGN KEY(member_id) REFERENCES members(id), FOREIGN KEY(chore_id) REFERENCES chores(id));
CREATE TABLE IF NOT EXISTS exceptions(id INTEGER PRIMARY KEY, exception_date TEXT NOT NULL, member_id INTEGER, assignment_id INTEGER, exception_type TEXT NOT NULL CHECK(exception_type IN ('skip','swap','replace','vacation','note')), replacement_chore_id INTEGER, replacement_member_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feed_tokens(id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS event_history(uid TEXT PRIMARY KEY, member_id INTEGER NOT NULL, event_date TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0, payload_hash TEXT NOT NULL, payload TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, last_modified TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_assignments ON assignments(member_id, weekday, week, active);
CREATE INDEX IF NOT EXISTS idx_exceptions ON exceptions(exception_date);
"""

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def conn_for(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db(path):
    conn = conn_for(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('anchor_date',?)", (date.today().isoformat(),))
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('schedule_version','1')")
        conn.commit()
    finally:
        conn.close()

def bump(conn):
    conn.execute("UPDATE settings SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='schedule_version'")

def iso_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError("date must use YYYY-MM-DD")

def slug(value):
    result = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value).strip())
    return "-".join(x for x in result.split("-") if x)[:48]

def text(value, limit=500):
    return str(value or "").strip()[:limit]

def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def week_for(day, anchor):
    return "A" if ((day - anchor).days // 7) % 2 == 0 else "B"

def resolve(conn, member_id, day):
    week = week_for(day, iso_date(conn.execute("SELECT value FROM settings WHERE key='anchor_date'").fetchone()[0]))
    assignments = conn.execute("""SELECT a.*, c.name chore_name, c.description chore_description, c.suggested_time chore_time
        FROM assignments a JOIN chores c ON c.id=a.chore_id
        WHERE a.member_id=? AND a.week=? AND a.weekday=? AND a.active=1 AND c.active=1 ORDER BY a.id""", (member_id, week, day.weekday())).fetchall()
    exceptions = conn.execute("SELECT * FROM exceptions WHERE exception_date=? ORDER BY id", (day.isoformat(),)).fetchall()
    result = []
    for assignment in assignments:
        applicable = [e for e in exceptions if (e["member_id"] is None or e["member_id"] == member_id) and (e["assignment_id"] is None or e["assignment_id"] == assignment["id"])]
        if any(e["exception_type"] in ("skip", "vacation") for e in applicable):
            continue
        if any(e["exception_type"] == "swap" for e in applicable):
            continue
        name, description, suggested = assignment["chore_name"], assignment["chore_description"], assignment["chore_time"]
        notes = [assignment["notes"]] if assignment["notes"] else []
        for e in applicable:
            if e["exception_type"] == "replace" and e["replacement_chore_id"]:
                replacement = conn.execute("SELECT * FROM chores WHERE id=? AND active=1", (e["replacement_chore_id"],)).fetchone()
                if replacement:
                    name, description, suggested = replacement["name"], replacement["description"], replacement["suggested_time"]
            if e["note"]:
                notes.append(e["note"])
        result.append({"assignment_id": assignment["id"], "date": day.isoformat(), "week": week, "name": name, "description": description, "suggested_time": suggested, "notes": " ".join(notes)})
    incoming = conn.execute("SELECT * FROM exceptions WHERE exception_date=? AND exception_type='swap' AND replacement_member_id=?", (day.isoformat(), member_id)).fetchall()
    for e in incoming:
        if not e["assignment_id"]:
            continue
        assignment = conn.execute("""SELECT a.*, c.name chore_name, c.description chore_description, c.suggested_time chore_time
            FROM assignments a JOIN chores c ON c.id=a.chore_id WHERE a.id=? AND a.active=1""", (e["assignment_id"],)).fetchone()
        if assignment and assignment["member_id"] != member_id:
            result.append({"assignment_id": assignment["id"], "date": day.isoformat(), "week": week, "name": assignment["chore_name"], "description": assignment["chore_description"], "suggested_time": assignment["chore_time"], "notes": e["note"] or assignment["notes"]})
    return result

def ical_escape(value):
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n").replace("\r", "")

def event_for(item, timezone_name):
    try:
        hour, minute = [int(x) for x in (item["suggested_time"] or "09:00").split(":", 1)]
    except ValueError:
        hour, minute = 9, 0
    day = iso_date(item["date"])
    tz = ZoneInfo(timezone_name)
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    end = start + timedelta(minutes=30)
    description = "Suggested time: " + start.strftime("%H:%M")
    if item["description"]:
        description += "\n" + item["description"]
    if item["notes"]:
        description += "\nNote: " + item["notes"]
    return {"uid": "chore-%s-%s@home.moralife.uk" % (item["assignment_id"], item["date"]), "start": start.isoformat(), "end": end.isoformat(), "title": "A little help: " + item["name"], "description": description}

def stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def calendar_body(conn, member, config):
    today = datetime.now(ZoneInfo(config["TIMEZONE"])).date()
    expected = {}
    for offset in range(config["HORIZON"] + 1):
        for item in resolve(conn, member["id"], today + timedelta(days=offset)):
            event = event_for(item, config["TIMEZONE"])
            expected[event["uid"]] = event
    output = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//moralife.uk//Family Chores//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:" + ical_escape(member["name"] + " chores"), "X-WR-TIMEZONE:" + config["TIMEZONE"]]
    now = datetime.now(timezone.utc)
    with conn:
        for uid, event in expected.items():
            raw = json.dumps(event, sort_keys=True)
            hashed = hashlib.sha256(raw.encode()).hexdigest()
            old = conn.execute("SELECT * FROM event_history WHERE uid=?", (uid,)).fetchone()
            if old is None:
                sequence, modified = 0, now_utc()
                conn.execute("INSERT INTO event_history VALUES(?,?,?,?,?,?,?,?)", (uid, member["id"], event["start"][:10], sequence, hashed, raw, 0, modified))
            else:
                sequence, modified = old["sequence"], old["last_modified"]
                if old["payload_hash"] != hashed or old["cancelled"]:
                    sequence, modified = sequence + 1, now_utc()
                conn.execute("UPDATE event_history SET member_id=?, event_date=?, sequence=?, payload_hash=?, payload=?, cancelled=0, last_modified=? WHERE uid=?", (member["id"], event["start"][:10], sequence, hashed, raw, modified, uid))
            history = conn.execute("SELECT * FROM event_history WHERE uid=?", (uid,)).fetchone()
            output += ical_event(history, event, config["TIMEZONE"], now, False)
        old_events = conn.execute("SELECT * FROM event_history WHERE member_id=? AND event_date>=? AND event_date<=? AND cancelled=0", (member["id"], (today - timedelta(days=30)).isoformat(), (today + timedelta(days=config["HORIZON"])).isoformat())).fetchall()
        for old in old_events:
            if old["uid"] in expected:
                continue
            modified = now_utc()
            conn.execute("UPDATE event_history SET sequence=?, cancelled=1, last_modified=? WHERE uid=?", (old["sequence"] + 1, modified, old["uid"]))
            output += ical_event(conn.execute("SELECT * FROM event_history WHERE uid=?", (old["uid"],)).fetchone(), json.loads(old["payload"]), config["TIMEZONE"], now, True)
    output.append("END:VCALENDAR")
    return "\r\n".join(output) + "\r\n"

def ical_event(history, event, timezone_name, now, cancelled):
    start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
    modified = datetime.fromisoformat(history["last_modified"])
    return ["BEGIN:VEVENT", "UID:" + history["uid"], "DTSTAMP:" + stamp(now), "DTSTART;TZID=" + timezone_name + ":" + start.strftime("%Y%m%dT%H%M%S"), "DTEND;TZID=" + timezone_name + ":" + end.strftime("%Y%m%dT%H%M%S"), "SUMMARY:" + ical_escape(event["title"]), "DESCRIPTION:" + ical_escape(event["description"]), "SEQUENCE:" + str(history["sequence"]), "LAST-MODIFIED:" + stamp(modified), "STATUS:" + ("CANCELLED" if cancelled else "CONFIRMED"), "END:VEVENT"]

class Limiter:
    def __init__(self): self.lock, self.failures = threading.Lock(), {}
    def allowed(self, key):
        now = datetime.now(timezone.utc).timestamp()
        with self.lock:
            self.failures[key] = [x for x in self.failures.get(key, []) if now - x < 900]
            return len(self.failures[key]) < 5
    def failed(self, key):
        with self.lock: self.failures.setdefault(key, []).append(datetime.now(timezone.utc).timestamp())

def create_app(test_config=None):
    config = {"SECRET_KEY": os.environ.get("SECRET_KEY", ""), "FAMILY_PASSWORD": os.environ.get("FAMILY_PASSWORD", ""), "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", ""), "DATABASE_PATH": os.environ.get("DATABASE_PATH", str(ROOT / "data" / "chores.sqlite3")), "TIMEZONE": os.environ.get("TIMEZONE", "America/Bogota"), "HOME_NETWORKS": os.environ.get("HOME_NETWORKS", ""), "TRUST_PROXY": os.environ.get("TRUST_PROXY", "0") == "1", "COOKIE_SECURE": os.environ.get("COOKIE_SECURE", "1") == "1", "HORIZON": max(14, int(os.environ.get("CALENDAR_HORIZON_DAYS", "90")))}
    if test_config: config.update(test_config)
    if not test_config:
        missing = [key for key in ("SECRET_KEY", "FAMILY_PASSWORD", "ADMIN_PASSWORD") if not config[key]]
        if missing:
            raise RuntimeError("missing required environment variables: " + ", ".join(missing))
    init_db(config["DATABASE_PATH"])
    networks = tuple(ipaddress.ip_network(x.strip()) for x in config["HOME_NETWORKS"].split(",") if x.strip())
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.secret_key = config["SECRET_KEY"] or "test-secret"
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=config["COOKIE_SECURE"], SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    if config["TRUST_PROXY"]: app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    limiter = Limiter()
    @app.before_request
    def admin_origin_guard():
        if request.path.startswith("/api/admin/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                return jsonify(error="bad_origin"), 403
    def db():
        if "db" not in request.environ: request.environ["db"] = conn_for(config["DATABASE_PATH"])
        return request.environ["db"]
    @app.teardown_request
    def close(_error):
        connection = request.environ.pop("db", None)
        if connection: connection.close()
    def home_access():
        try: return bool(request.remote_addr and any(ipaddress.ip_address(request.remote_addr) in n for n in networks))
        except ValueError: return False
    def family_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not (home_access() or session.get("family_ok")): return jsonify(error="family_auth_required"), 401
            return view(*args, **kwargs)
        return wrapped
    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("admin_ok"): return jsonify(error="admin_auth_required"), 401
            return view(*args, **kwargs)
        return wrapped
    def rows(query, args=()): return [dict(x) for x in db().execute(query, args).fetchall()]
    @app.get("/")
    def index(): return send_from_directory(STATIC, "index.html")
    @app.get("/admin")
    def admin(): return send_from_directory(STATIC, "admin.html")
    @app.get("/health")
    def health():
        try: db().execute("SELECT 1"); return jsonify(status="ok")
        except sqlite3.Error: return jsonify(status="error"), 503
    @app.post("/api/login")
    def login():
        key = request.remote_addr or "unknown"
        if not limiter.allowed(key): return jsonify(error="too_many_attempts"), 429
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        if not hmac.compare_digest(password, config["FAMILY_PASSWORD"]): limiter.failed(key); return jsonify(error="invalid_password"), 401
        session.clear(); session.permanent = True; session["family_ok"] = True
        return jsonify(ok=True)
    @app.post("/api/logout")
    def logout(): session.clear(); return jsonify(ok=True)
    @app.get("/api/members")
    @family_required
    def members(): return jsonify(members=[{"slug": x["slug"], "name": x["name"]} for x in db().execute("SELECT slug,name FROM members WHERE active=1 ORDER BY name").fetchall()])
    @app.get("/api/dashboard/<member_slug>")
    @family_required
    def dashboard(member_slug):
        member = db().execute("SELECT * FROM members WHERE slug=? AND active=1", (member_slug,)).fetchone()
        if not member: return jsonify(error="member_not_found"), 404
        today = datetime.now(ZoneInfo(config["TIMEZONE"])).date()
        days = [{"date": (today + timedelta(days=i)).isoformat(), "label": (today + timedelta(days=i)).strftime("%A"), "items": resolve(db(), member["id"], today + timedelta(days=i))} for i in range(7)]
        return jsonify(member={"slug": member["slug"], "name": member["name"]}, today=days[0], tomorrow=days[1], week=days)
    @app.post("/api/calendar-token/<member_slug>")
    @family_required
    def new_token(member_slug):
        member = db().execute("SELECT * FROM members WHERE slug=? AND active=1", (member_slug,)).fetchone()
        if not member: return jsonify(error="member_not_found"), 404
        token = secrets.token_urlsafe(48)
        with db(): db().execute("INSERT INTO feed_tokens(member_id,token_hash,created_at) VALUES(?,?,?)", (member["id"], digest_token(token), now_utc()))
        return jsonify(url=request.host_url.rstrip("/") + "/calendar/" + token + ".ics", webcal_url="webcal://" + request.host + "/calendar/" + token + ".ics")
    @app.get("/calendar/<token>.ics")
    def calendar(token):
        member = db().execute("SELECT m.* FROM feed_tokens t JOIN members m ON m.id=t.member_id WHERE t.token_hash=? AND t.revoked_at IS NULL AND m.active=1", (digest_token(token),)).fetchone()
        if not member: return jsonify(error="calendar_not_found"), 404
        response = app.response_class(calendar_body(db(), member, config), mimetype="text/calendar")
        response.headers["Cache-Control"] = "private, no-store"
        return response
    @app.post("/api/admin/login")
    def admin_login():
        key = "admin:" + (request.remote_addr or "unknown")
        if not limiter.allowed(key): return jsonify(error="too_many_attempts"), 429
        password = str((request.get_json(silent=True) or {}).get("password", ""))
        if not hmac.compare_digest(password, config["ADMIN_PASSWORD"]): limiter.failed(key); return jsonify(error="invalid_password"), 401
        session.clear(); session.permanent = True; session["admin_ok"] = True
        return jsonify(ok=True)
    @app.get("/api/admin/state")
    @admin_required
    def admin_state():
        return jsonify(anchor_date=db().execute("SELECT value FROM settings WHERE key='anchor_date'").fetchone()[0], members=rows("SELECT * FROM members ORDER BY name"), chores=rows("SELECT * FROM chores ORDER BY name"), assignments=rows("SELECT a.*,m.name member_name,c.name chore_name FROM assignments a JOIN members m ON m.id=a.member_id JOIN chores c ON c.id=a.chore_id ORDER BY a.week,a.weekday,m.name"), exceptions=rows("SELECT e.*,m.name member_name FROM exceptions e LEFT JOIN members m ON m.id=e.member_id ORDER BY e.exception_date DESC,e.id DESC"))
    @app.post("/api/admin/settings")
    @admin_required
    def admin_settings():
        anchor = iso_date((request.get_json(silent=True) or {}).get("anchor_date"))
        with db(): db().execute("UPDATE settings SET value=? WHERE key='anchor_date'", (anchor.isoformat(),)); bump(db())
        return jsonify(ok=True)
    @app.post("/api/admin/members")
    @admin_required
    def admin_member():
        payload = request.get_json(silent=True) or {}; name = text(payload.get("name"), 80)
        if not name: return jsonify(error="name_required"), 400
        try:
            with db():
                if payload.get("id"): db().execute("UPDATE members SET name=?,slug=?,active=1 WHERE id=?", (name, slug(payload.get("slug") or name), int(payload["id"])))
                else: db().execute("INSERT INTO members(slug,name,created_at) VALUES(?,?,?)", (slug(payload.get("slug") or name), name, now_utc()))
                bump(db())
        except sqlite3.IntegrityError: return jsonify(error="member_slug_already_exists"), 409
        return jsonify(ok=True)
    @app.delete("/api/admin/members/<int:item_id>")
    @admin_required
    def remove_member(item_id):
        with db(): db().execute("UPDATE members SET active=0 WHERE id=?", (item_id,)); bump(db())
        return jsonify(ok=True)
    @app.post("/api/admin/chores")
    @admin_required
    def admin_chore():
        payload = request.get_json(silent=True) or {}; name = text(payload.get("name"), 120)
        if not name: return jsonify(error="name_required"), 400
        values = (name, text(payload.get("description")), text(payload.get("suggested_time"), 10))
        with db():
            if payload.get("id"): db().execute("UPDATE chores SET name=?,description=?,suggested_time=?,active=1 WHERE id=?", (*values, int(payload["id"])))
            else: db().execute("INSERT INTO chores(name,description,suggested_time,created_at) VALUES(?,?,?,?)", (*values, now_utc()))
            bump(db())
        return jsonify(ok=True)
    @app.delete("/api/admin/chores/<int:item_id>")
    @admin_required
    def remove_chore(item_id):
        with db(): db().execute("UPDATE chores SET active=0 WHERE id=?", (item_id,)); db().execute("UPDATE assignments SET active=0 WHERE chore_id=?", (item_id,)); bump(db())
        return jsonify(ok=True)
    @app.post("/api/admin/assignments")
    @admin_required
    def admin_assignment():
        payload = request.get_json(silent=True) or {}; week = text(payload.get("week")).upper(); weekday = int(payload.get("weekday", -1))
        if week not in ("A", "B") or weekday not in range(7): return jsonify(error="invalid_week_or_day"), 400
        values = (int(payload["member_id"]), int(payload["chore_id"]), weekday, week, text(payload.get("notes")), now_utc())
        with db():
            if payload.get("id"): db().execute("UPDATE assignments SET member_id=?,chore_id=?,weekday=?,week=?,notes=?,active=1,updated_at=? WHERE id=?", (*values, int(payload["id"])))
            else: db().execute("INSERT INTO assignments(member_id,chore_id,weekday,week,notes,updated_at) VALUES(?,?,?,?,?,?)", values)
            bump(db())
        return jsonify(ok=True)
    @app.delete("/api/admin/assignments/<int:item_id>")
    @admin_required
    def remove_assignment(item_id):
        with db(): db().execute("UPDATE assignments SET active=0 WHERE id=?", (item_id,)); bump(db())
        return jsonify(ok=True)
    @app.post("/api/admin/exceptions")
    @admin_required
    def admin_exception():
        payload = request.get_json(silent=True) or {}; kind = text(payload.get("exception_type"))
        if kind not in ("skip", "swap", "replace", "vacation", "note"): return jsonify(error="invalid_exception_type"), 400
        values = (iso_date(payload.get("exception_date")).isoformat(), int(payload["member_id"]) if payload.get("member_id") else None, int(payload["assignment_id"]) if payload.get("assignment_id") else None, kind, int(payload["replacement_chore_id"]) if payload.get("replacement_chore_id") else None, int(payload["replacement_member_id"]) if payload.get("replacement_member_id") else None, text(payload.get("note")), now_utc())
        with db(): db().execute("INSERT INTO exceptions(exception_date,member_id,assignment_id,exception_type,replacement_chore_id,replacement_member_id,note,created_at) VALUES(?,?,?,?,?,?,?,?)", values); bump(db())
        return jsonify(ok=True)
    @app.delete("/api/admin/exceptions/<int:item_id>")
    @admin_required
    def remove_exception(item_id):
        with db(): db().execute("DELETE FROM exceptions WHERE id=?", (item_id,)); bump(db())
        return jsonify(ok=True)
    @app.post("/api/admin/members/<int:item_id>/rotate-tokens")
    @admin_required
    def rotate_tokens(item_id):
        with db(): db().execute("UPDATE feed_tokens SET revoked_at=? WHERE member_id=? AND revoked_at IS NULL", (now_utc(), item_id))
        return jsonify(ok=True)
    return app
