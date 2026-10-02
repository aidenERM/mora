import json
from unittest.mock import Mock

import pytest

from orbit.engine import Engine
from orbit.providers import ConfigurationError, FIELDS, ProviderError, Router, invoke_json
from orbit.store import Store


def test_auto_routes_by_word_not_substring(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    for role in ("OPENAI_MODEL", "MODEL_FAST", "MODEL_CODEX", "MODEL_STRONG"):
        monkeypatch.setenv("ORBIT_" + role, role)
    router = Router({"BEDROCK_MODEL_ID": "aws"})
    assert router.choose({"model": "auto", "goal": "latest Apple news"}) == ("openai", "MODEL_FAST")
    assert router.choose({"model": "auto", "goal": "fix repository tests"}) == ("openai", "MODEL_CODEX")
    assert router.choose({"model": "auto", "goal": "complex architecture"}) == ("openai", "MODEL_STRONG")


def test_unconfigured_provider_blocks_without_retry_and_model_switch_resumes(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ORBIT_OPENAI_API_KEY", raising=False)
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("inspect", model="sol")
    store.claim()
    result = Engine(store, Router({}), Mock()).execute(task["id"])
    assert result["status"] == "blocked" and result["error"] == "provider_not_configured"
    assert result["steps"] == 0
    store.select_model(task["id"], "aws")
    updated = store.task(task["id"])
    assert updated["id"] == task["id"] and updated["goal"] == task["goal"]
    assert updated["status"] == "queued" and updated["model"] == "aws"


def test_decision_budget_survives_restart(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("inspect")
    store.reserve_decision(task["id"], 1)
    with pytest.raises(ValueError):
        Store(store.path).reserve_decision(task["id"], 1)


def test_bedrock_prose_prefix_parses_only_complete_unambiguous_decision(monkeypatch):
    value = {field: "" for field in FIELDS}
    value.update(action="runtime_health", arguments="{}")
    client = Mock()
    client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "I will inspect.\n" + json.dumps(value)}]}}}
    monkeypatch.setattr("orbit.providers._client", lambda _: client)
    result, _ = invoke_json({"BEDROCK_MODEL_ID": "test"}, "test", {}, FIELDS)
    assert result == value
    client.converse.return_value["output"]["message"]["content"][0]["text"] = json.dumps(value) * 2
    with pytest.raises(ProviderError):
        invoke_json({"BEDROCK_MODEL_ID": "test"}, "test", {}, FIELDS)
    client.converse.return_value["stopReason"] = "max_tokens"
    with pytest.raises(ProviderError):
        invoke_json({"BEDROCK_MODEL_ID": "test"}, "test", {}, FIELDS)


def test_graceful_stop_does_not_execute_tool(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("inspect")
    store.claim()
    router, tools = Mock(), Mock()
    assert Engine(store, router, tools).execute(task["id"], lambda: True)["status"] == "queued"
    router.decide.assert_not_called()
    tools.invoke.assert_not_called()


def test_approved_action_expires_before_worker_claim(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("click")
    store.claim()
    step = store.start_step(task["id"], "browser_click", {})
    store.require_approval(step, task["id"], "external")
    store.approve(step, True)
    with store.connect() as db:
        db.execute("UPDATE approvals SET expires=0")
    assert store.claim() is None
    assert store.task(task["id"])["status"] == "blocked"


def test_primary_outage_circuit_skips_repeated_failed_requests(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ORBIT_OPENAI_MODEL", "test-model")
    router = Router({"BEDROCK_MODEL_ID": "aws"})
    router.openai = Mock(side_effect=ValueError("controlled outage"))
    monkeypatch.setattr("orbit.providers.invoke_json", Mock(return_value=({"action": "runtime_health"}, {})))
    task = {"model": "auto", "goal": "inspect"}
    router.decide(task, {})
    _, metadata = router.decide(task, {})
    assert router.openai.call_count == 1 and metadata["circuit_open"]


def test_used_approval_cannot_expire_and_block_later_work(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    task = store.create("continue after approved action")
    store.claim()
    step = store.start_step(task["id"], "browser_click", {})
    store.require_approval(step, task["id"], "external")
    store.approve(step, True)
    store.finish_step(step, {"observed": True})
    with store.connect() as db:
        db.execute("UPDATE approvals SET expires=0")
    assert store.claim()["id"] == task["id"]


def test_aws_vision_observation_feeds_normal_planner(monkeypatch):
    monkeypatch.setenv("ORBIT_AWS_VISION_MODEL", "verified-vision-model")
    client = Mock()
    client.converse.return_value = {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "Main heading: Example Domain"}]}}, "usage": {"inputTokens": 100}}
    monkeypatch.setattr("orbit.providers._client", lambda _: client)
    planner = Mock(return_value=({"action": "finish"}, {"provider": "aws"}))
    monkeypatch.setattr("orbit.providers.invoke_json", planner)
    _, meta = Router({"BEDROCK_MODEL_ID": "planner"}).decide({"model": "aws", "goal": "inspect"}, {}, {"format": "png", "bytes": b"fixture"})
    assert planner.call_args.args[2]["image_observation"] == "Main heading: Example Domain"
    assert meta["vision_model"] == "verified-vision-model"
