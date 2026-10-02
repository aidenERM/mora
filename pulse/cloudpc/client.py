"""Internal Pulse client. Credentials remain server-side."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


class CloudPC:
    def __init__(self, token_file=None):
        self.token_file = token_file or os.environ.get("PULSE_CLOUDPC_TOKEN_FILE", "/etc/pulse-cloudpc/token")
        self.base = "http://127.0.0.1:" + os.environ.get("PULSE_CLOUDPC_PORT", "8792")

    def request(self, endpoint, payload=None):
        if not endpoint.startswith("/v1/"):
            raise ValueError("invalid Cloud PC endpoint")
        token = Path(self.token_file).read_text().strip()
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + endpoint, data=data, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
        })
        with urlopen(request, timeout=70) as response:
            result = response.read()
            return result if response.headers.get_content_type() == "image/png" else json.loads(result)
