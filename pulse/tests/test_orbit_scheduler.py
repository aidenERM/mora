from datetime import datetime, timezone
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from orbit.memory import Memory
from orbit.policy import Policy
from orbit.reporting import report_finding, schedule_changed
from orbit.scheduler import Scheduler, enqueue_pulse_event, parse_timing
from orbit.store import Store
from orbit.tools import Tools


def test_relative_dates_use_bogota_and_recurrence_limits():
    now = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("America/Bogota"))
    tomorrow = parse_timing("look into this tomorrow at 5pm", now)
    wake = datetime.fromtimestamp(tomorrow["wake_at"], ZoneInfo("America/Bogota"))
    assert wake.day == 3 and wake.hour == 17
    assert parse_timing("check a file in two hours", now)["wake_at"] == now.timestamp() + 7200
    assert parse_timing("check file every 2 hours", now)["interval"] == 7200
    with pytest.raises(ValueError):
        parse_timing("check every 2 minutes", now)


def test_waiting_tasks_wake_once_without_budget_reset(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("later", wake_at=100)
    store.start_step(task["id"], "wait", {})
    claimed = store.claim()
    assert claimed["id"] == task["id"] and claimed["steps"] == 1
    assert store.claim() is None


def test_recurrence_is_deduplicated_and_can_disable(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    scheduler = Scheduler(store)
    schedule = scheduler.create("check file", 900, next_run=100)
    first = scheduler.tick(100)
    assert len(first) == 1 and first[0]["metadata"]["schedule_id"] == schedule
    assert scheduler.tick(100) == []
    scheduler.disable(schedule)
    assert scheduler.tick(2000) == []


def test_proactive_gate_uses_effective_score_and_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_PROACTIVE_ENABLED", "1")
    store = Store(tmp_path / "orbit.sqlite3")
    event = {"id": "event", "title": "meaningful development", "score": 99, "canonical_event_id": "story", "development_id": "one"}
    assert enqueue_pulse_event(event, {"notification_tier": "high", "effective_score": 70}, store) is None
    trace = {"notification_tier": "high", "effective_score": 90}
    first = enqueue_pulse_event(event, trace, store)
    assert first["metadata"]["read_only"]
    assert enqueue_pulse_event(event, trace, store) is None
    assert enqueue_pulse_event({**event, "development_id": "two"}, trace, store)
    assert enqueue_pulse_event({**event, "development_id": "three"}, trace, store) is None


def test_proactive_is_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_PROACTIVE_ENABLED", "0")
    assert enqueue_pulse_event({"id": "event"}, {}, Store(tmp_path / "orbit.sqlite3")) is None


def test_operator_policy_cannot_auto_publish_or_delete():
    policy = Policy({"automatic_shell_commands": ["python -m pytest", "git push", "rm data.txt"]})
    assert not policy.evaluate("shell", {"command": "python -m pytest"})
    assert policy.evaluate("shell", {"command": "git push"})
    assert policy.evaluate("shell", {"command": "rm data.txt"})
    assert policy.evaluate("browser_navigate", {"url": "https://example.com/account?action=delete"})
    with pytest.raises(ValueError):
        Policy({"blocked_tools": ["imaginary"]})


def test_browser_approval_binds_visible_element_and_blocks_passwords(tmp_path):
    client = Mock()
    client.request.return_value = {"url": "https://example.com", "element": {"type": "submit", "label": "Publish", "html_hash": "hash"}}
    tools = Tools(tmp_path, client)
    bound = tools.bind_approval("browser_click", {"selector": "button"})
    assert bound["_binding"]["element"]["label"] == "Publish"
    with pytest.raises(ValueError):
        tools.invoke("browser_click", {"selector": "button"}, 1)
    client.request.return_value["element"]["type"] = "password"
    with pytest.raises(ValueError):
        tools.bind_approval("browser_type", {"selector": "input", "text": "not a real password"})


def test_schedule_dedup_uses_verified_evidence_not_wording(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    for summary in ("first phrasing", "different phrasing"):
        task = store.create(summary, metadata={"schedule_id": "same"})
        step = store.start_step(task["id"], "finish", {})
        store.finish_step(step, {"checks": [{"tool": "file_read", "expect": {"content": "same value"}}]}, "verified")
        changed = schedule_changed(store, task)
        assert changed == (summary == "first phrasing")


def test_proactive_repeats_rejected_and_quiet_hours_reused(tmp_path):
    from pulse_app.config import load_config
    from pulse_app.storage import init_db, connect, upsert_event, save_preferences
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    db = connect(database)
    now = datetime.now(timezone.utc)
    old = "Apple announced new iPhone plans for launch."
    event, _ = upsert_event(db, {"id": "apple-test", "source_id": "apple", "source_kind": "rss", "topic": "apple", "title": old,
        "summary": old, "body": old, "canonical_key": "apple:test", "url": "https://www.apple.com/newsroom/test",
        "published_at": now.isoformat(), "score": 95, "priority": "high", "relevant": True,
        "metadata": {"source_trust": "primary", "sources": [{"url": "https://www.apple.com/newsroom/test", "trust": "primary"}]}})
    config = load_config({"DATABASE_PATH": str(database), "QUIET_START": "00:00", "QUIET_END": "23:59"})
    db.commit()
    db.close()
    store = Store(tmp_path / "orbit.sqlite3")
    memory = Memory(store)
    task = store.create("investigate", source="pulse", metadata={"event_id": event["id"]})
    store.state("report_requested:" + task["id"], True)
    assert not report_finding(store, memory, task, {"result": old}, config, now)
    text = "Shipment delivery postponed Friday warehouse transport disruption manufacturing shortage delays regional availability customers."
    step = store.start_step(task["id"], "browser_observe", {})
    store.finish_step(step, {"url": event["url"], "text": text})
    assert not report_finding(store, memory, task, {"result": old}, config, now)
    assert not report_finding(store, memory, task, {"result": text}, config, now)
    trace = store.state("delivery_decision:" + task["id"])
    assert trace["trace"]["quiet_hours"]["active"]
    assert memory.claim_notice() is None
