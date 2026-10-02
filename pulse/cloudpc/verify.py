"""Non-sensitive integration probe. --restart-service requires root on the VPS."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from client import CloudPC


HTML = b'''<!doctype html><title>probe</title><input id="value"><button id="save">save</button>
<script>
function show() {document.title = (localStorage.getItem('pulse_probe') || 'empty') +
 '|' + (document.cookie.includes('pulse_probe=session-marker') ? 'session-cookie' : 'no-cookie');}
document.querySelector('#save').onclick = () => {
 localStorage.setItem('pulse_probe', document.querySelector('#value').value);
 document.cookie='pulse_probe=session-marker; path=/'; show();}; show();
</script>'''


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-service", action="store_true")
    args = parser.parse_args()
    fixture = ThreadingHTTPServer(("127.0.0.1", 8793), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    client = CloudPC()
    marker = "probe-" + uuid.uuid4().hex
    path = "verification/" + marker + ".txt"
    try:
        try:
            urlopen(client.base + "/v1/health", timeout=5)
            raise AssertionError("unauthenticated access allowed")
        except HTTPError as exc:
            assert exc.code == 401
        before = client.request("/v1/health")
        assert before["ok"]
        client.request("/v1/files/write", {"path": path, "content": marker})
        assert client.request("/v1/files/read", {"path": path})["content"] == marker
        shell = client.request("/v1/shell", {"command": "cat " + path})
        assert shell["exit_code"] == 0 and shell["output"] == marker
        first = client.request("/v1/files/read", {"path": path, "max_lines": 1})
        assert first["content"] == marker and first["total_lines"] == 1
        try:
            client.request("/v1/files/write", {"path": path, "content": "changed", "overwrite": True, "expected_sha256": "stale"})
            raise AssertionError("stale file overwrite allowed")
        except HTTPError as exc:
            assert exc.code == 400
        for bad in ("../session.json", "/etc/pulse-cloudpc/token"):
            try:
                client.request("/v1/files/read", {"path": bad})
                raise AssertionError("workspace boundary violated")
            except HTTPError as exc:
                assert exc.code == 400
        timed = client.request("/v1/shell", {"command": "sleep 5", "timeout": 1})
        assert timed["timed_out"]
        client.request("/v1/browser/navigate", {"url": "http://127.0.0.1:8793/"})
        client.request("/v1/browser/type", {"selector": "#value", "text": marker})
        binding = client.request("/v1/browser/describe", {"selector": "#save"})
        try:
            client.request("/v1/browser/click", {"selector": "#save", "_binding": {**binding, "url": "https://stale.example"}})
            raise AssertionError("stale browser approval allowed")
        except HTTPError as exc:
            assert exc.code == 400
        client.request("/v1/browser/click", {"selector": "#save", "_binding": binding})
        expected = marker + "|session-cookie"
        assert client.request("/v1/browser/state")["title"] == expected
        image = client.request("/v1/browser/screenshot")
        assert image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) > 1000
        # A new client has no in-memory identity or browser state of its own.
        assert CloudPC().request("/v1/health")["runtime_id"] == before["runtime_id"]
        if args.restart_service:
            subprocess.run(["systemctl", "restart", "pulse-cloudpc"], check=True)
            for _ in range(60):
                try:
                    after = CloudPC().request("/v1/health")
                    break
                except (OSError, HTTPError):
                    time.sleep(1)
            else:
                raise AssertionError("runtime did not return after restart")
            assert after["runtime_id"] == before["runtime_id"]
            assert after["started_at"] > before["started_at"]
            assert client.request("/v1/files/read", {"path": path})["content"] == marker
            assert client.request("/v1/browser/state")["title"] == expected
            assert client.request("/v1/browser/screenshot").startswith(b"\x89PNG")
        # Verify real public HTTPS navigation as well as the deterministic fixture.
        result = client.request("/v1/browser/navigate", {"url": "https://example.com"})
        assert "Example Domain" in result["title"]
        print(json.dumps({"passed": True, "authentication": True, "workspace_boundary": True,
                          "file_read_write": True, "shell": True, "shell_timeout": True,
                          "click_type": True, "screenshot_png_bytes": len(image),
                          "reconnect_same_runtime": True, "restart_persistence": args.restart_service,
                          "cookie_and_local_storage": True, "public_https_navigation": True}))
    finally:
        fixture.shutdown()


if __name__ == "__main__":
    main()
