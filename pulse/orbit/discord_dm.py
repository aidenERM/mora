"""Owner-only official Discord bot interface. No guild chat ingestion."""
import asyncio
import io
import json
import os
from pathlib import Path
import re
import time

import discord
from discord.ext import tasks

from cloudpc.client import CloudPC
from .memory import Memory
from .security import clean
from .store import Store


def intent(text):
    text = text.strip()
    lower = text.lower()
    if lower in {"hi", "hey", "hello", "yo"}:
        return "hello", ""
    if lower.startswith("remember "):
        return "remember", text[9:].strip()
    if lower.startswith("forget "):
        return "forget", text[7:].strip()
    if lower in {"stop", "cancel", "stop that", "cancel that"}:
        return "cancel", ""
    if any(phrase in lower for phrase in ("what happened", "what are you doing", "task status")):
        return "status", ""
    match = re.match(r"(?i)^(?:use|switch to) (aws|sol|codex|auto|fast|strong|openai)(?:\s+(?:for this|for that))?[,:]?\s*(.*)$", text)
    if match:
        return "model", (match.group(1).lower(), match.group(2))
    return "goal", text


class Approval(discord.ui.View):
    def __init__(self, bot, step):
        super().__init__(timeout=None)
        for accepted, label, style in ((True, "Approve", discord.ButtonStyle.success), (False, "Decline", discord.ButtonStyle.secondary)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"orbit:{int(accepted)}:{step}")
            async def callback(interaction, accepted=accepted):
                if interaction.user.id != bot.owner_id or interaction.guild is not None:
                    await interaction.response.send_message("This action belongs to the owner.", ephemeral=True)
                    return
                try:
                    bot.store.approve(step, accepted)
                    await interaction.response.edit_message(content="Approved. I’ll continue." if accepted else "Declined. Task cancelled.", view=None)
                except ValueError:
                    await interaction.response.send_message("That approval expired or was already resolved.")
            button.callback = callback
            self.add_item(button)


class OrbitDiscord(discord.Client):
    def __init__(self, store=None, owner_id=None):
        intents = discord.Intents.none()
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.store = store or Store()
        self.memory = Memory(self.store)
        self.owner_id = int(owner_id or os.environ["ORBIT_DISCORD_USER_ID"])
        self.runtime = CloudPC()

    async def setup_hook(self):
        self.memory.recover_notices()
        with self.store.connect() as db:
            pending = db.execute("SELECT step_id FROM approvals WHERE status='pending' AND expires>?", (time.time(),)).fetchall()
        for row in pending:
            self.add_view(Approval(self, row[0]))
        self.deliver.start()

    async def on_ready(self):
        self.store.state("discord", {"connected": True, "ready_at": time.time()})
        self.memory.notice("setup", "connected", "Orbit is connected. Send me a goal here. I’m using the existing AWS connection; OpenAI is not configured yet.")

    async def on_disconnect(self):
        self.store.state("discord", {"connected": False, "updated_at": time.time()})

    async def on_error(self, event, *args, **kwargs):
        self.store.state("discord_error", {"event": event, "updated_at": time.time()})

    async def on_message(self, message):
        if message.guild is not None or message.author.bot or message.author.id != self.owner_id:
            return
        text = clean(message.content.strip())
        attachments = []
        for index, attachment in enumerate(message.attachments[:3]):
            if attachment.size > 700000:
                await message.channel.send("That file is too large for this version. Limit: 700 KB.")
                continue
            data = await attachment.read()
            name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(attachment.filename).name)[:100]
            try:
                content = data.decode("utf-8")
                contains_credentials = clean(content) != content
            except UnicodeDecodeError:
                contains_credentials = False
            if contains_credentials or name.startswith(".env") or "credentials" in name.lower():
                await message.channel.send("That credential attachment was not saved.")
                continue
            path = f"inbox/{message.id}-{index}-{name}"
            import base64
            await asyncio.to_thread(self.runtime.request, "/v1/files/write", {"path": path, "content": base64.b64encode(data).decode(), "encoding": "base64"})
            attachments.append({"path": path, "size": len(data), "type": attachment.content_type})
        if not text and attachments:
            text = "Inspect the shared files and explain what is useful. Do not modify external systems."
        if not text:
            return
        self.memory.conversation("user", text, "discord:" + str(message.id))
        kind, value = intent(text)
        if kind == "hello":
            await message.channel.send("hey. send me what you want done.")
        elif kind == "remember":
            try:
                memory_id = self.memory.remember(value, source="discord:" + str(message.id), context="explicit user request")
                await message.channel.send("Remembered. Reference: " + memory_id[:8])
            except ValueError:
                await message.channel.send("That text could not be saved as memory.")
        elif kind == "forget":
            with self.store.connect() as db:
                matches = db.execute("SELECT id FROM memories WHERE id LIKE ? OR text=?", (value + "%", value)).fetchall()
            if len(matches) == 1:
                self.memory.forget(matches[0][0])
                await message.channel.send("Forgotten.")
            else:
                await message.channel.send("Give me the memory reference so I remove the right item.")
        elif kind == "status":
            rows = self.store.list(5)
            await message.channel.send("\n".join(f"{row['status']}: {row['goal'][:100]}" for row in rows) or "No tasks yet.")
        elif kind == "cancel":
            rows = [row for row in self.store.list() if row["status"] in {"queued", "running", "waiting", "waiting_approval"}]
            if rows:
                self.store.cancel(rows[0]["id"])
            await message.channel.send("Stopped." if rows else "Nothing is running.")
        elif kind == "model":
            model, goal = value
            self.store.state("default_model", model)
            if goal:
                await self.submit(message, goal, attachments, model)
            else:
                await message.channel.send("Selected " + model + ". Unconfigured models will report a blocker.")
        else:
            await self.submit(message, value, attachments)

    async def submit(self, message, goal, attachments, model=None):
        previous = self.store.list(1)
        task = self.store.create(goal, model or self.store.state("default_model") or "auto", source="discord", source_key="discord:" + str(message.id),
            metadata={"user_id": str(self.owner_id), "attachments": attachments,
                      "conversation": self.memory.recent(), "previous_task": previous[0] if previous else None})
        await message.channel.send("on it. " + task["id"][:8])

    @tasks.loop(seconds=5)
    async def deliver(self):
        notice = self.memory.claim_notice()
        if not notice:
            return
        try:
            owner = await self.fetch_user(self.owner_id)
            kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
            text = clean(notice["text"])
            if notice["kind"].startswith("approval-"):
                step_id = int(notice["kind"].split("-", 1)[1])
                with self.store.connect() as db:
                    step = db.execute("SELECT action,arguments FROM steps WHERE id=?", (step_id,)).fetchone()
                proposal = json.dumps({"action": step["action"], "arguments": json.loads(step["arguments"])}, indent=2)
                kwargs["view"] = Approval(self, step_id)
                kwargs["file"] = discord.File(io.BytesIO(proposal.encode()), filename="proposed-action.json")
            elif notice["artifact"]:
                root = (Path(self.store.path).parent / "artifacts").resolve()
                path = (root / notice["artifact"]).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("invalid artifact")
                kwargs["file"] = discord.File(path)
            sent = await owner.send(text[:1800] or "Task updated.", **kwargs)
            self.memory.notice_done(notice["id"], "sent", str(sent.id))
            self.memory.conversation("assistant", text)
            self.store.state("discord_delivery", {"ok": True, "sent_at": time.time()})
        except Exception as exc:
            self.memory.notice_done(notice["id"], "failed")
            self.store.state("discord_delivery", {"ok": False, "error": type(exc).__name__, "code": getattr(exc, "code", None)})

    @deliver.before_loop
    async def wait_for_connection(self):
        await self.wait_until_ready()


def main():
    os.umask(0o077)
    token = os.environ.get("ORBIT_DISCORD_BOT_TOKEN")
    if not token or not os.environ.get("ORBIT_DISCORD_USER_ID"):
        raise RuntimeError("Discord bot token and owner ID required")
    try:
        OrbitDiscord().run(token, log_handler=None)
    except Exception as exc:
        print("discord_start_failure:" + type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
