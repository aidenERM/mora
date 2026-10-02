from unittest.mock import Mock
from urllib.error import URLError

import pytest

from cloudpc.client import CloudPC


def test_refused_connection_retries_without_replaying_received_action(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-private-token-for-tests-only")
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"ok":true}'
    response.headers.get_content_type.return_value = "application/json"
    request = Mock(side_effect=[URLError(ConnectionRefusedError("not ready")), response])
    monkeypatch.setattr("cloudpc.client.urlopen", request)
    monkeypatch.setattr("cloudpc.client.time.sleep", Mock())
    assert CloudPC(token).request("/v1/browser/click", {"selector": "button"})["ok"]
    assert request.call_count == 2


def test_lost_response_never_replays_mutation(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-private-token-for-tests-only")
    request = Mock(side_effect=URLError(TimeoutError("response lost")))
    monkeypatch.setattr("cloudpc.client.urlopen", request)
    with pytest.raises(URLError):
        CloudPC(token).request("/v1/browser/click", {"selector": "button"})
    assert request.call_count == 1
