"""Single private Linux runtime. Only a trusted Pulse caller may control it."""
import hmac
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, Error as BrowserError

MAX_BODY = 1024 * 1024


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.workspace = self.root / "workspace"
        for directory in (self.root, self.workspace, self.root / "profile", self.root / "home"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        identity = self.root / "identity.json"
        if not identity.exists():
            import uuid
            atomic_json(identity, {"runtime_id": str(uuid.uuid4()), "created_at": time.time()})
        self.identity = json.loads(identity.read_text())
        self.started_at = time.time()
        # Disable browser password storage. Authentication cookies stay private on disk.
        preferences = self.root / "profile" / "Default" / "Preferences"
        preferences.parent.mkdir(exist_ok=True, mode=0o700)
        pref = json.loads(preferences.read_text()) if preferences.exists() else {}
        pref["credentials_enable_service"] = False
        pref.setdefault("profile", {})["password_manager_enabled"] = False
        atomic_json(preferences, pref)
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.root / "profile"), headless=True, chromium_sandbox=True,
            viewport={"width": 1280, "height": 800}, accept_downloads=False,
        )
        self.context.set_default_timeout(15000)
        self.context.set_default_navigation_timeout(30000)
        session = self.root / "session.json"
        saved = json.loads(session.read_text()) if session.exists() else {}
        if saved.get("cookies"):
            self.context.add_cookies(saved["cookies"])
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        url = saved.get("url", "")
        if url.startswith(("https://", "http://")):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except BrowserError:
                pass  # An offline site must not prevent reconnecting to the runtime.

    def checkpoint(self):
        atomic_json(self.root / "session.json", {
            "url": self.page.url, "cookies": self.context.cookies(),
        })

    def close(self):
        try:
            self.checkpoint()
        finally:
            self.context.close()
            self.playwright.stop()

    def path(self, value):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError("relative workspace path required")
        path = (self.workspace / value).resolve()
        if not path.is_relative_to(self.workspace) or path == self.workspace:
            raise ValueError("path must stay inside workspace")
        return path

    def shell(self, body):
        command = text(body, "command", 16384)
        timeout = body.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise ValueError("timeout must be between 1 and 60 seconds")
        cwd = self.path(body["cwd"]) if body.get("cwd") else self.workspace
        if not cwd.is_dir():
            raise ValueError("cwd must be a workspace directory")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
               "HOME": str(self.root / "home")}
        # Bound output on disk, even for a noisy command. Kill the entire process group.
        wrapper = 'ulimit -f 16384; exec /bin/bash -c "$1"'
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                ["/bin/bash", "-c", wrapper, "pulse-shell", command],
                cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            output.seek(0)
            raw = output.read(65537)
        return {"exit_code": process.returncode, "timed_out": timed_out,
                "output": raw[:65536].decode("utf-8", errors="replace"),
                "truncated": len(raw) > 65536}

    def dispatch(self, route, body):
        if route == "/v1/shell":
            return self.shell(body)
        if route == "/v1/files/read":
            path = self.path(body.get("path"))
            with path.open("rb") as file:
                content = file.read(MAX_BODY + 1)
            if len(content) > MAX_BODY:
                raise ValueError("file exceeds 1 MiB")
            encoding = body.get("encoding", "utf-8")
            if encoding not in {"utf-8", "base64"}:
                raise ValueError("unsupported encoding")
            return {"path": body["path"], "content": base64.b64encode(content).decode() if encoding == "base64" else content.decode("utf-8"), "encoding": encoding}
        if route == "/v1/files/write":
            path = self.path(body.get("path"))
            content = body.get("content")
            if not isinstance(content, str) or len(content.encode()) > MAX_BODY:
                raise ValueError("content must be UTF-8 text up to 1 MiB")
            encoding = body.get("encoding", "utf-8")
            if encoding not in {"utf-8", "base64"}:
                raise ValueError("unsupported encoding")
            raw = base64.b64decode(content, validate=True) if encoding == "base64" else content.encode()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("wb" if body.get("overwrite") is True else "xb") as file:
                file.write(raw)
            path.chmod(0o600)
            return {"path": body["path"], "written": len(raw)}
        if route == "/v1/browser/navigate":
            url = text(body, "url", 8192)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("HTTP(S) URL without embedded credentials required")
            self.page.goto(url, wait_until="domcontentloaded")
        elif route == "/v1/browser/click":
            self.page.locator(text(body, "selector", 2048)).click()
        elif route == "/v1/browser/type":
            selector = text(body, "selector", 2048)
            value = body.get("text")
            if not isinstance(value, str) or len(value) > 65536:
                raise ValueError("text must be a string up to 64 KiB")
            self.page.locator(selector).fill(value)
        else:
            raise KeyError("unknown endpoint")
        self.checkpoint()
        return {"url": self.page.url, "title": self.page.title()}


def text(body, key, maximum):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be nonempty text up to {maximum} characters")
    return value


def handler(runtime, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log URLs, typed text, commands, or Authorization headers.

        def reply(self, status, value, kind="application/json"):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            expected = "Bearer " + token
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                self.reply(401, {"error": "unauthorized"})
                return False
            if self.headers.get("Origin"):
                self.reply(403, {"error": "browser cross-origin requests forbidden"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            try:
                if self.path == "/v1/health":
                    self.reply(200, {"ok": runtime.context.browser.is_connected(), **runtime.identity,
                                     "started_at": runtime.started_at, "workspace": str(runtime.workspace)})
                elif self.path == "/v1/browser/state":
                    self.reply(200, {"url": runtime.page.url, "title": runtime.page.title()})
                elif self.path == "/v1/browser/observe":
                    visible = runtime.page.evaluate('''() => ({
                        text: (document.body?.innerText || '').slice(0, 8000),
                        elements: [...document.querySelectorAll('a,button,input,textarea,select,[role="button"]')]
                            .filter(e => e.getClientRects().length).slice(0, 60).map((e,i) => {
                                e.setAttribute('data-orbit-ref', String(i));
                                return {selector: '[data-orbit-ref="'+i+'"]', tag: e.tagName.toLowerCase(),
                                    label: (e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') || '').slice(0,120),
                                    type: e.getAttribute('type') || '', href: e.getAttribute('href') || ''};
                            })
                    })''')
                    self.reply(200, {"url": runtime.page.url, "title": runtime.page.title(), **visible})
                elif self.path == "/v1/browser/screenshot":
                    self.reply(200, runtime.page.screenshot(type="png"), "image/png")
                else:
                    self.reply(404, {"error": "unknown endpoint"})
            except BrowserError:
                self.reply(502, {"error": "browser operation failed"})

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    self.reply(413, {"error": "request exceeds limit or is empty"})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                self.reply(200, runtime.dispatch(self.path, body))
            except KeyError:
                self.reply(404, {"error": "unknown endpoint"})
            except (ValueError, OSError) as exc:
                self.reply(400, {"error": type(exc).__name__})
            except BrowserError:
                self.reply(502, {"error": "browser operation failed"})

        def setup(self):
            super().setup()
            self.connection.settimeout(35)
    return Handler


def main():
    os.umask(0o077)
    token_file = Path(os.environ.get("PULSE_CLOUDPC_TOKEN_FILE", "/etc/pulse-cloudpc/token"))
    token = token_file.read_text().strip()
    if len(token) < 32:
        raise RuntimeError("a private token of at least 32 characters is required")
    runtime = Runtime(os.environ.get("PULSE_CLOUDPC_STATE_DIR", "/var/lib/pulse-cloudpc"))
    server = HTTPServer(("127.0.0.1", int(os.environ.get("PULSE_CLOUDPC_PORT", "8792"))), handler(runtime, token))
    server.timeout = 1
    stopping = False

    def stop(*args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            server.handle_request()
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()
