import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from family_chores.app import create_app, conn_for, day_rule, event_for, resolve, week_for


@pytest.fixture()
def app(tmp_path):
    path = str(tmp_path / "chores.sqlite3")
    app = create_app({"TESTING": True, "DATABASE_PATH": path, "SECRET_KEY": "test", "FAMILY_PASSWORD": "family", "ADMIN_PASSWORD": "admin", "HOME_NETWORKS": "127.0.0.1/32", "COOKIE_SECURE": False, "TIMEZONE": "UTC", "HORIZON": 14})
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["admin_ok"] = True
        yield app, client, path


def seed(path, anchor="2026-01-05"):
    conn = conn_for(path)
    conn.execute("UPDATE settings SET value=? WHERE key='anchor_date'", (anchor,))
    conn.execute("INSERT INTO members(slug,name,created_at) VALUES('alex','Alex','now')")
    conn.execute("INSERT INTO members(slug,name,created_at) VALUES('sam','Sam','now')")
    conn.execute("INSERT INTO chores(name,description,suggested_time,created_at) VALUES('dishes','','18:00','now')")
    conn.execute("INSERT INTO chores(name,description,suggested_time,created_at) VALUES('bins','','19:00','now')")
    conn.execute("INSERT INTO assignments(member_id,chore_id,weekday,week,notes,updated_at) VALUES(1,1,0,'A','','now')")
    conn.execute("INSERT INTO assignments(member_id,chore_id,weekday,week,notes,updated_at) VALUES(1,2,0,'B','','now')")
    conn.commit(); conn.close()


def test_two_week_cycle():
    anchor = date(2026, 1, 5)
    assert week_for(anchor, anchor) == "A"
    assert week_for(anchor + timedelta(days=6), anchor) == "A"
    assert week_for(anchor + timedelta(days=7), anchor) == "B"
    assert week_for(anchor + timedelta(days=14), anchor) == "A"


def test_dashboard_and_exceptions(app):
    _, client, path = app
    seed(path)
    with client.session_transaction() as session: session.clear(); session["family_ok"] = True
    assert client.get("/api/dashboard/alex").status_code == 200
    conn = conn_for(path)
    monday = date(2026, 1, 5)
    assert resolve(conn, 1, monday)[0]["name"] == "dishes"
    conn.execute("INSERT INTO exceptions(exception_date,member_id,assignment_id,exception_type,note,created_at) VALUES('2026-01-05',1,1,'skip','rest day','now')")
    conn.commit()
    assert resolve(conn, 1, monday) == []
    conn.execute("DELETE FROM exceptions WHERE exception_type='skip'")
    conn.execute("INSERT INTO exceptions(exception_date,member_id,assignment_id,exception_type,replacement_chore_id,note,created_at) VALUES('2026-01-05',1,1,'replace',2,'swap it','now')")
    conn.commit()
    assert resolve(conn, 1, monday)[0]["name"] == "bins"
    conn.execute("INSERT INTO exceptions(exception_date,member_id,assignment_id,exception_type,replacement_member_id,note,created_at) VALUES('2026-01-05',1,1,'swap',2,'Sam can help','now')")
    conn.commit()
    assert resolve(conn, 1, monday) == []
    assert resolve(conn, 2, monday)[0]["name"] == "dishes"
    conn.close()


def test_external_password_and_admin_are_separate(app):
    _, client, _ = app
    client.environ_base["REMOTE_ADDR"] = "198.51.100.9"
    assert client.get("/api/members").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "family"}).status_code == 200
    assert client.get("/api/members").status_code == 200
    client.post("/api/logout")
    assert client.post("/api/admin/login", json={"password": "family"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "admin"}).status_code == 200


def test_home_network_bypass(app):
    _, client, path = app
    seed(path)
    client.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    assert client.get("/api/members").status_code == 200


def test_calendar_uid_and_revocation(app):
    _, client, path = app
    today = datetime.now(ZoneInfo("UTC")).date()
    seed(path, today.isoformat())
    with client.session_transaction() as session: session["family_ok"] = True
    token = client.post("/api/calendar-token/alex").get_json()["url"].split("/")[-1][:-4]
    first = client.get("/calendar/" + token + ".ics")
    second = client.get("/calendar/" + token + ".ics")
    assert first.status_code == second.status_code == 200
    uids = [line for line in first.get_data(as_text=True).splitlines() if line.startswith("UID:")]
    assert uids and len(uids) == len(set(uids))
    assert first.get_data(as_text=True).count(uids[0]) == 1
    assert first.get_data() == second.get_data()
    conn = conn_for(path); conn.execute("UPDATE feed_tokens SET revoked_at='now' WHERE token_hash=?", (__import__("hashlib").sha256(token.encode()).hexdigest(),)); conn.commit(); conn.close()
    assert client.get("/calendar/" + token + ".ics").status_code == 404


def test_expired_session(app):
    application, client, _ = app
    client.environ_base["REMOTE_ADDR"] = "198.51.100.9"
    client.post("/api/login", json={"password": "family"})
    application.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=-1)
    assert client.get("/api/members").status_code == 401


def test_duplicate_calendar_generation_has_unique_uids(app):
    _, client, path = app
    seed(path, date.today().isoformat())
    with client.session_transaction() as session: session["family_ok"] = True
    token_one = client.post("/api/calendar-token/alex").get_json()["url"].split("/")[-1][:-4]
    token_two = client.post("/api/calendar-token/alex").get_json()["url"].split("/")[-1][:-4]
    for token in (token_one, token_two):
        body = client.get("/calendar/" + token + ".ics").get_data(as_text=True)
        uids = [line for line in body.splitlines() if line.startswith("UID:")]
        assert len(uids) == len(set(uids))


def test_vacation_spans_multiple_days_and_assignment_can_move(app):
    _, client, path = app
    seed(path)
    conn = conn_for(path)
    conn.execute("INSERT INTO assignments(member_id,chore_id,weekday,week,notes,updated_at) VALUES(1,1,1,'A','','now')")
    conn.execute("INSERT INTO exceptions(exception_date,end_date,member_id,assignment_id,exception_type,note,created_at) VALUES('2026-01-05','2026-01-06',1,NULL,'vacation','away','now')")
    conn.commit()
    assert resolve(conn, 1, date(2026, 1, 5)) == []
    assert resolve(conn, 1, date(2026, 1, 6)) == []
    conn.execute("UPDATE assignments SET member_id=2 WHERE id=1")
    conn.commit()
    assert resolve(conn, 1, date(2026, 1, 5)) == []
    assert resolve(conn, 2, date(2026, 1, 5))[0]["name"] == "dishes"
    conn.close()


def test_timezone_boundary_and_malformed_admin_input(app):
    _, client, _ = app
    item = {"assignment_id": 9, "date": "2026-01-05", "name": "late tidy", "description": "", "suggested_time": "23:45", "notes": ""}
    event = event_for(item, "America/Bogota")
    assert event["start"].startswith("2026-01-05T23:45")
    assert event["end"].startswith("2026-01-06T00:15")
    assert client.post("/api/admin/settings", json={"anchor_date": "not-a-date"}).status_code == 400
    assert client.post("/api/admin/assignments", json={"week": "A", "weekday": 0}).status_code == 400


def test_spanish_calendar_and_task_status(app):
    _, client, path = app
    today = date.today() - timedelta(days=date.today().weekday())
    seed(path, today.isoformat())
    with client.session_transaction() as session: session["family_ok"] = True
    token = client.post("/api/calendar-token/alex").get_json()["url"].split("/")[-1][:-4]
    body = client.get("/calendar/" + token + ".ics").get_data(as_text=True)
    assert "SUMMARY:dishes" in body
    assert "Hora sugerida" in body
    assert client.post("/api/status", json={"assignment_id": 1, "member_slug": "alex", "date": today.isoformat(), "status": "done"}).status_code == 200


def test_import_preview_and_apply_is_idempotent(app):
    _, client, path = app
    payload = {"members": [{"name": "Ana M"}], "chores": [{"name": "Desayuno", "suggested_time": "10:00"}], "assignments": [{"member": "Ana M", "chore": "Desayuno", "weekday": 0, "week": "A"}]}
    assert client.post("/api/admin/import-preview", json=payload).status_code == 200
    assert client.post("/api/admin/import-apply", json=payload).status_code == 200
    assert client.post("/api/admin/import-apply", json=payload).status_code == 200
    conn = conn_for(path)
    assert conn.execute("SELECT COUNT(*) FROM members WHERE slug='ana-m'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
    conn.close()


def test_specific_holiday_uses_holiday_rule_not_weekday_row(app):
    _, client, path = app
    seed(path)
    conn = conn_for(path)
    conn.execute("INSERT INTO holidays(holiday_date,label,created_at) VALUES('2026-01-05','Festivo local','now')")
    conn.execute("INSERT INTO holiday_assignments(member_id,chore_id,notes,updated_at) VALUES(2,2,'regla festiva','now')")
    conn.commit()
    assert resolve(conn, 1, date(2026, 1, 5)) == []
    assert resolve(conn, 2, date(2026, 1, 5))[0]["name"] == "bins"
    assert resolve(conn, 1, date(2026, 1, 19))[0]["name"] == "dishes"
    conn.close()


def test_weekend_note_is_rule_without_invented_assignment(app):
    _, _, path = app
    seed(path)
    conn = conn_for(path)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('weekend_morning_note','Revisar la casa por la mañana')")
    conn.commit()
    assert day_rule(conn, date(2026, 1, 10)) == "Revisar la casa por la mañana"
    assert resolve(conn, 1, date(2026, 1, 10)) == []
    conn.close()


def test_holiday_admin_is_date_specific_and_import_idempotent(app):
    _, client, path = app
    assert client.post("/api/admin/holidays", json={"holiday_date": "2026-12-25", "label": "Navidad"}).status_code == 200
    assert client.post("/api/admin/holidays", json={"holiday_date": "2026-12-25", "label": "Navidad"}).status_code == 200
    assert client.post("/api/admin/holidays", json={"holiday_date": "bad", "label": "Festivo"}).status_code == 400
    conn = conn_for(path)
    assert conn.execute("SELECT COUNT(*) FROM holidays WHERE holiday_date='2026-12-25'").fetchone()[0] == 1
    conn.close()

