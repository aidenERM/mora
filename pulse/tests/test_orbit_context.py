import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from orbit.context import read_context
from orbit.providers import ConfigurationError, Router
from orbit.tools import Tools
from pulse_app.config import load_config
from pulse_app.storage import connect, init_db, set_context_signal


def test_cached_context_filters_expiry_without_mutating_pulse(tmp_path):
    database = tmp_path / "pulse.sqlite3"
    init_db(database)
    db = connect(database)
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    set_context_signal(db, "physical_context", {"mode": "outside"}, "shortcut", 1, expired)
    set_context_signal(db, "state", {"state": "sleep"}, "shortcut", 1, None)
    db.commit()
    config = load_config({"DATABASE_PATH": str(database)})
    result = read_context(config=config)
    assert [row["kind"] for row in result["context"]] == ["state"]
    assert db.execute("SELECT COUNT(*) FROM context_signals").fetchone()[0] == 2
    db.close()


def test_image_artifact_validation_and_no_binary_in_state(tmp_path):
    client = Mock()
    raw = b"\x89PNG\r\n\x1a\nprivate-image-fixture"
    client.request.return_value = {"content": base64.b64encode(raw).decode()}
    tools = Tools(tmp_path, client)
    observation = tools.invoke("image_inspect", {"path": "inbox/image.png"}, 1)
    assert "bytes" not in observation or isinstance(observation["bytes"], int)
    assert tools.vision(observation)["bytes"] == raw
    with pytest.raises(ValueError):
        tools.invoke("image_inspect", {"artifact": "../../private.png"}, 2)


def test_unconfigured_vision_never_fakes_image_understanding(monkeypatch):
    for name in ("OPENAI_API_KEY", "ORBIT_OPENAI_API_KEY", "ORBIT_OPENAI_MODEL", "ORBIT_VISION_MODEL", "ORBIT_AWS_VISION_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError):
        Router({}).decide({"model": "auto", "goal": "read image"}, {}, {"format": "png", "bytes": b"image"})


def test_negative_user_scope_blocks_deploy_and_send():
    from orbit.policy import Policy
    policy = Policy()
    with pytest.raises(ValueError):
        policy.evaluate("shell", {"command": "git push"}, "fix this but do not deploy")
    with pytest.raises(ValueError):
        policy.evaluate("browser_click", {"_binding": {"element": {"label": "Send email"}}}, "draft a reply but don't send")


def test_file_patch_preserves_unrelated_text_and_checks_concurrent_edits(tmp_path):
    client = Mock()
    client.request.side_effect = [{"content": "prefix old suffix", "sha256": "previous-hash"}, {"written": 17}, {"content": "prefix new suffix"}]
    result = Tools(tmp_path, client).invoke("file_patch", {"path": "source.py", "old": "old", "new": "new"}, 1)
    assert result["verified"]
    assert client.request.call_args_list[1].args[1]["expected_sha256"] == "previous-hash"
    client.request.side_effect = [{"content": "old old"}]
    with pytest.raises(ValueError):
        Tools(tmp_path, client).invoke("file_patch", {"path": "source.py", "old": "old", "new": "new"}, 2)


def test_binary_metadata_can_independently_verify_without_shell(tmp_path):
    import hashlib
    client = Mock()
    raw = b"non-sensitive binary fixture"
    client.request.return_value = {"content": base64.b64encode(raw).decode()}
    tools = Tools(tmp_path, client)
    expected = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert tools.verify([{"tool": "file_info", "arguments": {"path": "inbox/file.png"}, "expect": expected}])[0]["verified"]
    with pytest.raises(ValueError):
        tools.invoke("file_info", {"path": "../profile/session.json"}, 1)
