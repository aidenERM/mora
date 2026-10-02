"""Root-only Discord provisioning. Read credentials from SSH stdin, never arguments."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import requests


def main():
    if os.geteuid() != 0:
        raise ValueError("root required")
    payload = json.loads(sys.stdin.read(4096))
    token = payload["bot_token"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise ValueError("invalid bot token shape")
    headers = {"Authorization": "Bot " + token, "User-Agent": "PulseOrbit/0.1"}
    user = requests.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=15)
    user.raise_for_status()
    application = requests.get("https://discord.com/api/v10/oauth2/applications/@me", headers=headers, timeout=15)
    application.raise_for_status()
    bot, app = user.json(), application.json()
    if not bot.get("bot") or str(app.get("id")) != str(payload["application_id"]) or str(bot.get("id")) != str(app.get("id")):
        raise ValueError("bot/application identity mismatch")
    owner = str((app.get("team") or {}).get("owner_user_id") or (app.get("owner") or {}).get("id") or "")
    if not owner.isdigit() or owner == bot["id"]:
        raise ValueError("human application owner required")
    target = Path("/etc/orbit.env")
    if target.is_symlink():
        raise ValueError("symlink configuration forbidden")
    updates = {"ORBIT_DISCORD_BOT_TOKEN": token, "ORBIT_DISCORD_USER_ID": owner}
    lines = target.read_text().splitlines() if target.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0] not in updates]
    lines += [key + "=" + value for key, value in updates.items()]
    fd, temporary = tempfile.mkstemp(prefix="orbit-env-", dir="/etc")
    with os.fdopen(fd, "w") as file:
        file.write("\n".join(lines) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    print(json.dumps({"bot_verified": True, "application_matched": True, "human_owner_resolved": True, "secret_file": str(target), "permissions": "0600"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
