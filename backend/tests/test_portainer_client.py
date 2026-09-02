# Regression: container start via Portainer docker proxy needs an explicit empty
# JSON body + content-type (Docker >=1.24 rejects non-empty body on start), and
# _req must merge caller headers instead of crashing with duplicate kwarg.

import httpx
import pytest

from app.services.portainer_client import PortainerClient


class FakeResp:
    status_code = 204

    def json(self):
        return {}


def test_start_container_sends_empty_json_body(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["content"] = kw.get("content")
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    pc.start_container(8, "abc123")
    assert captured["method"] == "POST"
    assert "/endpoints/8/docker/containers/abc123/start" in captured["url"]
    assert captured["content"] == b"{}"
    assert captured["headers"].get("Content-Type") == "application/json"
    # auth header survives merge
    assert captured["headers"].get("X-API-Key") == "tok"


def test_stop_container_plain_post(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kw):
        captured["method"] = method
        captured["headers"] = headers
        captured["content"] = kw.get("content")
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    pc.stop_container(8, "abc123")
    assert captured["method"] == "POST"
    assert captured["content"] is None  # stop accepts plain POST
    assert captured["headers"].get("X-API-Key") == "tok"
