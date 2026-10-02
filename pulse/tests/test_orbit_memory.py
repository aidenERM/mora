import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orbit.memory import Memory
from orbit.security import clean
from orbit.store import Store


def test_sourced_memory_dedup_retrieval_and_forget(tmp_path):
    memory = Memory(Store(tmp_path / "orbit.sqlite3"))
    first = memory.remember("Pulse is my project", "project", "user:1", "explicit request")
    assert memory.remember("Pulse is my project", "project", "user:2") == first
    memory.remember("Unrelated telescope details", "project", "user:3")
    result = memory.retrieve("work on Pulse")
    assert len(result) == 1 and result[0]["source"] == "user:2"
    assert memory.forget(first)
    assert memory.retrieve("work on Pulse") == []


def test_memory_expiry_and_conversation_retention(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    memory = Memory(store)
    memory.remember("expired Pulse fact", "episode", "test", days=-1)
    assert memory.retrieve("Pulse") == []
    for i in range(45):
        memory.conversation("user", str(i))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 40
    assert len(memory.recent()) == 4


def test_credentials_not_persisted_or_memorized(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DISCORD_BOT_TOKEN", "synthetic-private-credential")
    store = Store(tmp_path / "orbit.sqlite3")
    memory = Memory(store)
    task = store.create("value synthetic-private-credential")
    assert "synthetic-private-credential" not in store.task(task["id"])["goal"]
    with pytest.raises(ValueError):
        memory.remember("remember synthetic-private-credential")
    assert "hiddenpassword" not in clean("password: hiddenpassword")


def test_notice_is_deduplicated_and_interrupted_send_not_replayed(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    memory = Memory(store)
    memory.notice("task", "completed", "done")
    memory.notice("task", "completed", "done again")
    assert memory.claim_notice()["text"] == "done"
    memory.recover_notices()
    assert memory.claim_notice() is None


def test_periodic_retention_removes_raw_context_but_preserves_active_goals(tmp_path):
    store = Store(tmp_path / "orbit.sqlite3")
    memory = Memory(store)
    active = store.create("keep active goal", metadata={"conversation": [{"text": "old raw message"}]})
    completed = store.create("finished goal")
    step = store.start_step(completed["id"], "browser_observe", {})
    store.finish_step(step, {"text": "old page"})
    store.change(completed["id"], "completed", result="useful result")
    memory.conversation("user", "old DM")
    with store.connect() as db:
        db.execute("UPDATE tasks SET created=0")
        db.execute("UPDATE steps SET created=0")
        db.execute("UPDATE conversations SET created=0")
    memory.maintain()
    assert store.task(active["id"])["goal"] == "keep active goal"
    assert "conversation" not in store.task(active["id"])["metadata"]
    assert store.task(completed["id"])["result"] == "useful result"
    assert store.history(completed["id"]) == []
    assert memory.recent() == []


def test_discord_ignores_other_users_and_guilds(tmp_path):
    from orbit.discord_dm import OrbitDiscord
    bot = OrbitDiscord(Store(tmp_path / "orbit.sqlite3"), owner_id=123)
    channel = SimpleNamespace(send=AsyncMock())
    for author, guild in ((124, None), (123, object())):
        message = SimpleNamespace(author=SimpleNamespace(id=author, bot=False), guild=guild, channel=channel)
        asyncio.run(bot.on_message(message))
    channel.send.assert_not_called()
    assert bot.store.list() == []


def test_discord_natural_goal_and_explicit_memory(tmp_path):
    from orbit.discord_dm import OrbitDiscord
    bot = OrbitDiscord(Store(tmp_path / "orbit.sqlite3"), owner_id=123)
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(id=1, author=SimpleNamespace(id=123, bot=False), guild=None,
        channel=channel, content="remember Pulse is my project", attachments=[])
    asyncio.run(bot.on_message(message))
    assert bot.memory.retrieve("Pulse")[0]["text"] == "Pulse is my project"
    message.id = 2
    message.content = "check my project but do not deploy"
    asyncio.run(bot.on_message(message))
    assert bot.store.list()[0]["goal"] == message.content
    assert bot.store.task(bot.store.list()[0]["id"])["metadata"]["conversation"]


def test_owner_only_approval_button(tmp_path):
    from orbit.discord_dm import Approval, OrbitDiscord
    bot = OrbitDiscord(Store(tmp_path / "orbit.sqlite3"), owner_id=123)
    task = bot.store.create("click")
    bot.store.claim()
    step = bot.store.start_step(task["id"], "browser_click", {"selector": "button"})
    bot.store.require_approval(step, task["id"], "external")
    async def run():
        view = Approval(bot, step)
        interaction = SimpleNamespace(user=SimpleNamespace(id=124), guild=None,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        await view.children[0].callback(interaction)
        assert bot.store.task(task["id"])["status"] == "waiting_approval"
        interaction.user.id = 123
        await view.children[0].callback(interaction)
        assert bot.store.task(task["id"])["status"] == "queued"
    asyncio.run(run())
