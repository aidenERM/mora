import hashlib
import json
from pathlib import Path
import shlex
from urllib.error import HTTPError
from urllib.parse import urlsplit

from cloudpc.client import CloudPC

TOOLS = {
    "browser_state": "Read current URL and title. arguments: {}",
    "browser_observe": "Read visible page text and actionable elements. arguments: {}",
    "browser_navigate": "Open HTTP(S) URL. arguments: {url}",
    "browser_click": "Click a selector, may need approval. arguments: {selector}",
    "browser_type": "Fill input; may need approval. arguments: {selector,text}",
    "browser_screenshot": "Inspect/save private screenshot. arguments: {}",
    "shell": "Run workspace shell command. arguments: {command,timeout?,cwd?}; risky commands require approval",
    "file_read": "Read workspace UTF-8 file. arguments: {path}",
    "file_write": "Write workspace UTF-8 file. arguments: {path,content,overwrite?}",
    "runtime_health": "Read runtime identity/health. arguments: {}",
}
ENDPOINTS = {"browser_state": "/v1/browser/state", "browser_observe": "/v1/browser/observe", "browser_navigate": "/v1/browser/navigate",
             "browser_click": "/v1/browser/click", "browser_type": "/v1/browser/type",
             "browser_screenshot": "/v1/browser/screenshot", "shell": "/v1/shell",
             "file_read": "/v1/files/read", "file_write": "/v1/files/write", "runtime_health": "/v1/health"}
READ_TOOLS = {"file_read", "browser_state", "browser_observe", "runtime_health", "browser_screenshot"}


def safe_shell(command):
    if not isinstance(command, str) or any(char in command for char in ";&|<>`$\n\r"):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts or any(".." in arg or arg.startswith("/") for arg in parts):
        return False
    # File reads use the path-confined file API. General shell/code, including Git
    # hooks and test scripts, needs approval because it can access browser state.
    return parts[0] in {"pwd", "ls"} and all(not arg.startswith("--") for arg in parts[1:])


def risk(action, arguments):
    if action not in TOOLS:
        raise ValueError("unknown tool")
    if action in {"browser_click", "browser_type"}:
        return "website interaction may change account data or send information"
    if action == "shell" and not safe_shell(arguments.get("command")):
        return "shell command requires explicit approval"
    return ""


class Tools:
    def __init__(self, artifacts, client=None):
        self.client = client or CloudPC()
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)

    def invoke(self, action, arguments, step_id):
        if action not in TOOLS or not isinstance(arguments, dict):
            raise ValueError("invalid tool request")
        if action.startswith("file_"):
            path = arguments.get("path", "")
            if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("workspace-relative path required")
            if any(part.startswith(".env") or part.lower() in {"credentials", "secrets", "token", "session.json"} for part in Path(path).parts):
                raise ValueError("secret file access forbidden")
        if action == "browser_navigate":
            parsed = urlsplit(arguments.get("url", ""))
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                raise ValueError("HTTP(S) URL without credentials required")
        endpoint = ENDPOINTS[action]
        get = action in {"browser_state", "browser_screenshot", "browser_observe", "runtime_health"}
        result = self.client.request(endpoint, None if get else arguments)
        if action == "browser_screenshot":
            if not isinstance(result, bytes) or not result.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("invalid screenshot")
            path = self.artifacts / f"step-{step_id}.png"
            path.write_bytes(result)
            path.chmod(0o600)
            result = {"artifact": path.name, "bytes": len(result), "sha256": hashlib.sha256(result).hexdigest(), "verified": True}
        elif action == "file_write":
            check = self.client.request(ENDPOINTS["file_read"], {"path": arguments["path"]})
            if check.get("content") != arguments.get("content"):
                raise ValueError("file write verification failed")
            result["verified"] = True
        elif action == "shell":
            result["ok"] = result["exit_code"] == 0 and not result.get("timed_out")
        return result

    def verify(self, checks):
        if not isinstance(checks, list) or not 1 <= len(checks) <= 5:
            raise ValueError("completion requires 1–5 explicit read-only checks")
        results = []
        for check in checks:
            action, args = check.get("tool"), check.get("arguments", {})
            if action not in READ_TOOLS and not (action == "shell" and safe_shell(args.get("command"))):
                raise ValueError("verification must be read-only")
            result = self.invoke(action, args, "verification")
            expected = check.get("expect")
            if not isinstance(expected, dict) or not expected:
                raise ValueError("verification expectation required")
            for key, value in expected.items():
                if result.get(key) != value:
                    raise ValueError("completion check did not match observed result")
            results.append({"tool": action, "verified": True, "expect": expected})
        return results
