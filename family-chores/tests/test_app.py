import json
from datetime import date, timedelta

import pytest

from family_chores.app import create_app, conn_for, init_db, resolve, week_for


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
    assert client.post("/api/login", json={"password": "family"}).status_code == 200
    assert client.get("/api/members").status_code == 200
    client.post("/api/logout")
    assert client.post("/api/admin/login", json={"password": "family"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "admin"}).status_code == 200


def test_calendar_uid_and_revocation(app):
    _, client, path = app
    today = date.today()
    seed(path, today.isoformat())
    with client.session_transaction() as session: session["family_ok"] = True
    token = client.post("/api/calendar-token/alex").get_json()["url"].split("/")[-1][:-4]
    first = client.get("/calendar/" + token + ".ics")
    second = client.get("/calendar/" + token + ".ics")
    assert first.status_code == second.status_code == 200
    uid = ("UID:chore-1-" + today.isoformat() + "@home.moralife.uk").encode()
    assert first.get_data().count(uid) == 1
    conn = conn_for(path); conn.execute("UPDATE feed_tokens SET revoked_at='now' WHERE token_hash=?", (__import__("hashlib").sha256(token.encode()).hexdigest(),)); conn.commit(); conn.close()
    assert client.get("/calendar/" + token + ".ics").status_code == 404
