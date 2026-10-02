import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from orbit.engine import Engine
from orbit.store import Store
from orbit.tools import Tools, risk, safe_shell
from orbit.providers import Router


def decision(action, arguments=None, checks=None):
    return {"action": action, "arguments": json.dumps(arguments or {}), "summary": "verified test", "verification": json.dumps(checks or []), "wake_at": ""}, {"provider": "test"}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orbit.sqlite3")


def test_independent_verification_required(store, tmp_path):
    task = store.create("check file")
    store.claim()
    router = Mock()
    router.decide.side_effect = [decision("finish"), decision("finish")]
    tools = Tools(tmp_path, Mock())
    result = Engine(store, router, tools).execute(task["id"])
    assert result["status"] == "failed"
    assert all(row["status"] == "failed" for row in store.history(task["id"]))


def test_file_write_is_read_back_and_verified(store, tmp_path):
    task = store.create("write test.txt")
    store.claim()
    client = Mock()
    client.request.side_effect = [{"written": 4}, {"content": "test"}, {"content": "test"}]
    router = Mock()
    router.decide.side_effect = [decision("file_write", {"path": "test.txt", "content": "test"}),
        decision("finish", checks=[{"tool": "file_read", "arguments": {"path": "test.txt"}, "expect": {"content": "test"}}])]
    result = Engine(store, router, Tools(tmp_path, client)).execute(task["id"])
    assert result["status"] == "completed"
    assert store.history(task["id"])[0]["observation"]["verified"]
    assert client.request.call_count == 3


def test_wrong_readback_fails_and_bounds_retries(store, tmp_path):
    task = store.create("write")
    store.claim()
    client = Mock()
    client.request.side_effect = [{}, {"content": "wrong"}, {}, {"content": "wrong"}]
    router = Mock()
    router.decide.return_value = decision("file_write", {"path": "test.txt", "content": "right"})
    assert Engine(store, router, Tools(tmp_path, client)).execute(task["id"])["status"] == "failed"
    assert router.decide.call_count == 2


def test_budget_is_persistent(store, tmp_path):
    task = store.create("inspect", max_steps=2)
    store.claim()
    router, tools = Mock(), Mock()
    router.decide.return_value = decision("runtime_health")
    tools.invoke.return_value = {"ok": True}
    result = Engine(store, router, tools).execute(task["id"])
    assert result["status"] == "failed" and result["error"] == "step_budget_exhausted"
    assert Store(store.path).task(task["id"])["steps"] == 2


def test_cancellation_during_model_request_prevents_tool(store, tmp_path):
    task = store.create("write")
    store.claim()
    def cancelled(*_):
        store.cancel(task["id"])
        return decision("file_write", {"path": "test.txt", "content": "test"})
    router, tools = Mock(), Mock()
    router.decide.side_effect = cancelled
    assert Engine(store, router, tools).execute(task["id"])["status"] == "cancelled"
    tools.invoke.assert_not_called()


def test_interruption_recovers_idle_but_does_not_replay_unknown_tool(store):
    task = store.create("inspect")
    store.claim()
    store.recover()
    assert store.task(task["id"])["status"] == "queued"
    store.claim()
    store.start_step(task["id"], "browser_click", {"selector": "button"})
    store.recover()
    assert store.task(task["id"])["status"] == "blocked"
    store.resume(task["id"])
    assert store.task(task["id"])["status"] == "queued"
    assert store.history(task["id"])[0]["status"] == "interrupted"


def test_approval_is_explicit_single_use_and_denial_cancels(store):
    task = store.create("click")
    store.claim()
    step = store.start_step(task["id"], "browser_click", {"selector": "button"})
    store.require_approval(step, task["id"], "external action")
    assert store.task(task["id"])["status"] == "waiting_approval"
    store.approve(step, False)
    assert store.task(task["id"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        store.approve(step, True)


def test_expired_approval_cannot_run(store):
    task = store.create("click")
    store.claim()
    step = store.start_step(task["id"], "browser_click", {})
    store.require_approval(step, task["id"], "external")
    with store.connect() as db:
        db.execute("UPDATE approvals SET expires=0")
    with pytest.raises(ValueError):
        store.approve(step, True)


@pytest.mark.parametrize("command", ["ls; curl evil", "cat ../profile/session.json", "cat /etc/pulse.env", "git push", "rm -rf .", "rg --pre=evil text", "echo $(env)"])
def test_dangerous_shell_needs_approval(command):
    assert not safe_shell(command)
    assert risk("shell", {"command": command})


def test_source_task_is_idempotent(store):
    first = store.create("first", source_key="pulse:test")
    second = store.create("duplicate", source_key="pulse:test")
    assert first["id"] == second["id"]


def test_provider_fallback_keeps_identical_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ORBIT_OPENAI_MODEL", "test-model")
    router = Router({"BEDROCK_MODEL_ID": "test-aws-model"})
    router.openai = Mock(side_effect=ValueError("primary failed"))
    fallback = Mock(return_value=({"action": "finish"}, {"model": "test-aws-model"}))
    monkeypatch.setattr("orbit.providers.invoke_json", fallback)
    context = {"goal": "inspect", "observations": [{"success": True}]}
    _, metadata = router.decide({"model": "auto", "goal": "inspect"}, context)
    assert fallback.call_args.args[2] is context
    assert metadata["fallback_from"] == "openai"


def test_screenshot_is_real_png_and_private(tmp_path):
    client = Mock()
    client.request.return_value = b"\x89PNG\r\n\x1a\nsynthetic"
    result = Tools(tmp_path, client).invoke("browser_screenshot", {}, 1)
    assert result["verified"] and (tmp_path / "step-1.png").exists()
    client.request.return_value = b"not a screenshot"
    with pytest.raises(ValueError):
        Tools(tmp_path, client).invoke("browser_screenshot", {}, 2)
