"""Operator-owned policy. Models cannot edit their own permissions."""
import json
import os
from pathlib import Path
import re
import shlex
from urllib.parse import unquote, urlsplit

from .tools import TOOLS, risk


class Policy:
    def __init__(self, rules=None):
        if rules is None:
            path = Path(os.environ.get("ORBIT_POLICY_FILE", "/etc/orbit-policy.json"))
            rules = {}
            if path.exists():
                if path.is_symlink() or (os.name == "posix" and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022)):
                    raise ValueError("policy must be root-owned and not writable by other users")
                rules = json.loads(path.read_text())
        if not isinstance(rules, dict):
            raise ValueError("invalid policy")
        self.blocked = set(rules.get("blocked_tools", []))
        if not self.blocked.issubset(TOOLS):
            raise ValueError("unknown policy tool")
        self.trusted_commands = rules.get("automatic_shell_commands", [])
        if not isinstance(self.trusted_commands, list) or any(not isinstance(item, str) for item in self.trusted_commands):
            raise ValueError("invalid automatic command policy")

    def classify(self, action, arguments, goal=""):
        reason = self.evaluate(action, arguments, goal)
        command = arguments.get("command", "")
        conditional = action == "shell" and (re.match(r"^git (status|diff|log|show|commit)\b", command) or re.match(r"^python(?:3)? -m pytest\b", command))
        level = "conditional" if conditional else "approval_required" if reason else "automatic"
        return {"level": level, "requires_approval": bool(reason), "reason": reason}

    def evaluate(self, action, arguments, goal=""):
        if action in self.blocked:
            raise ValueError("tool disabled by operator policy")
        operation = str(arguments.get("command", "")) + " " + str(arguments.get("url", "")) + " " + str((arguments.get("_binding") or {}).get("element", {}).get("label", ""))
        if re.search(r"(?i)\b(do not|don't|never)\s+(deploy|publish|release)\b", goal) and re.search(r"(?i)\b(deploy|publish|release|push|production)\b", operation):
            raise ValueError("deployment forbidden by this goal")
        if re.search(r"(?i)\b(do not|don't|never)\s+send\b", goal) and re.search(r"(?i)\b(send|message|email)\b", operation):
            raise ValueError("sending forbidden by this goal")
        if action == "browser_navigate":
            url = urlsplit(arguments.get("url", ""))
            if url.hostname in {"localhost", "127.0.0.1", "::1"}:
                return "internal website requires approval"
            if re.search(r"\b(delete|remove|logout|unsubscribe|checkout|purchase|publish)\b", unquote(url.path + " " + url.query).lower()):
                return "URL may modify account or external state"
        if action == "shell" and arguments.get("command") in self.trusted_commands:
            command = arguments["command"]
            if any(char in command for char in ";&|<>`$\n\r"):
                return "shell composition requires approval"
            if re.search(r"\b(push|deploy|publish|release|delete|sudo|rm|rmdir|curl|wget|ssh)\b", command):
                return "publishing, destructive, or external shell action requires approval"
            return ""
        return risk(action, arguments)
