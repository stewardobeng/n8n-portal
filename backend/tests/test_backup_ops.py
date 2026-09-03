# Backups + image-update (0.1.33+). Unit tests for the new backup_ops service
# and the PortainerClient archive/exec/image helpers, using monkeypatched httpx.

import httpx
import pytest
import sqlite3

from app.services.portainer_client import PortainerClient, _strip_docker_stream
from app.services import backup_ops
from app import db


class FakeResp:
    def __init__(self, status_code=200, content=b"", json_data=None):
        self.status_code = status_code
        self.content = content
        self._json = json_data or {}

    def json(self):
        return self._json


# ---------- docker stream de-framing ----------

def test_strip_docker_stream_removes_framing():
    # Build a framed payload: [1,0,0,0,len(4 bytes)] + data
    def frame(data: bytes, stream_type: int = 1) -> bytes:
        return bytes([stream_type, 0, 0, 0]) + len(data).to_bytes(4, "big") + data

    payload = frame(b'{"name":"x"}') + frame(b"stderr-line\n", 2) + frame(b'"done"')
    out = _strip_docker_stream(payload)
    assert out == b'{"name":"x"}stderr-line\n"done"'


def test_strip_docker_stream_empty_and_short():
    assert _strip_docker_stream(b"") == b""
    # unterminated tail preserved verbatim
    assert _strip_docker_stream(b"\x01\x00\x00\x00\x00\x00\x00") == b"\x01\x00\x00\x00\x00\x00\x00"


# ---------- exec create/start framing ----------

def test_exec_in_container_strips_frames(monkeypatch):
    calls = {}

    def fake_request(method, url, headers=None, **kw):
        calls.setdefault("order", []).append((method, url))
        if url.endswith("/exec"):
            return FakeResp(status_code=201, json_data={"Id": "exec-1"})
        if "/exec/exec-1/start" in url:
            assert kw.get("content") == b"{}"  # empty body per Docker >=1.24
            return FakeResp(status_code=200, content=b"\x01\x00\x00\x00\x00\x00\x00\x08json!end")
        if "/archive" in url:
            return FakeResp(status_code=200, content=b"raw-tar")
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    out = pc.exec_in_container(8, "cid1", ["/bin/sh", "-c", "echo hi"])
    assert out == b"json!end"


def test_get_container_archive(monkeypatch):
    calls = {}

    def fake_request(method, url, headers=None, **kw):
        calls["url"] = url
        calls["params"] = kw.get("params")
        calls["headers"] = headers
        return FakeResp(status_code=200, content=b"tar-bytes")

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    data = pc.get_container_archive(8, "cid1", "/home/node/.n8n")
    assert data == b"tar-bytes"
    assert calls["params"] == {"path": "/home/node/.n8n"}
    assert "containers/cid1/archive" in calls["url"]


# ---------- image update regex ----------

def test_update_stack_image_swaps_tag(monkeypatch):
    compose = "services:\n  n8n:\n    image: n8nio/n8n:latest\n    restart: unless-stopped\n"

    def fake_request(method, url, headers=None, **kw):
        if "/stacks/84/file" in url and method == "GET":
            return FakeResp(status_code=200, json_data={"StackFileContent": compose})
        if "/stacks/84" in url and method == "PUT":
            payload = kw.get("json") or {}
            assert "n8nio/n8n:2.31.6" in payload["stackFileContent"]
            return FakeResp(status_code=200, json_data={"ok": True})
        if "/stacks/84" in url and method == "GET":
            return FakeResp(status_code=200, json_data={"Env": [{"name": "N8N_PORT", "value": "32768"}]})
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    pc.update_stack_image(84, 8, "n8nio/n8n:2.31.6")


def test_update_stack_image_same_tag_redeploys(monkeypatch):
    # identical tag = the Portainer "re-pull current version" case: the content
    # is unchanged but the stack must still be PUT with pull (0.1.50 behaviour).
    compose = "image: n8nio/n8n:latest\n"
    calls = {}

    def fake_request(method, url, headers=None, **kw):
        if "/stacks/84/file" in url and method == "GET":
            return FakeResp(status_code=200, json_data={"StackFileContent": compose})
        if "/stacks/84" in url and method == "GET":
            return FakeResp(status_code=200, json_data={"Env": []})
        if "/stacks/84" in url and method == "PUT":
            calls["put"] = url
            return FakeResp(status_code=200, json_data={"ok": True})
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    pc.update_stack_image(84, 8, "n8nio/n8n:latest")
    assert "pullImage=true" in calls["put"], calls


def test_update_stack_image_missing_image_line_raises(monkeypatch):
    # a stack whose compose has no n8nio/n8n image line at all is an error
    compose = "image: busybox:1.36\n"

    def fake_request(method, url, headers=None, **kw):
        if "/stacks/84/file" in url and method == "GET":
            return FakeResp(status_code=200, json_data={"StackFileContent": compose})
        return FakeResp()

    monkeypatch.setattr(httpx, "request", fake_request)
    pc = PortainerClient(base_url="http://pt:9000", token="tok")
    with pytest.raises(RuntimeError):
        pc.update_stack_image(84, 8, "n8nio/n8n:2.31.6")


# ---------- backup service (mocked db + portainer) ----------

def test_full_backup_writes_gzip(monkeypatch, tmp_path):
    import gzip
    # fake instance row
    inst = {
        "id": 1, "account_id": 1, "stack_name": "delta", "stack_id": 84,
        "environment_id": 8, "managed": 1, "container_id": "",
        "image": "n8nio/n8n:latest",
    }
    # stub resolve_container to give a fake running container
    monkeypatch.setattr(backup_ops, "resolve_container", lambda inst: (8, "cid1"))
    # monkeypatch the client's archive via httpx? Simpler: stub settings.backup_dir
    import app.services.backup_ops as bo
    monkeypatch.setattr(bo.settings, "backup_dir", str(tmp_path))

    # stub PortainerClient return of archive bytes and DB row guard
    created = {"called": False}

    class FakePC:
        def get_container_archive(self, env, cid, path):
            created["called"] = True
            return b"fake-tar-data"

    monkeypatch.setattr(backup_ops, "PortainerClient", lambda: FakePC())
    # record finish and fail on a real-ish temp db
    monkeypatch.setattr(db, "finish_backup", lambda bid, size: None)
    monkeypatch.setattr(db, "fail_backup", lambda bid, err: None)

    bo.run_full_backup(9, inst)
    # file should now exist gzipped
    import glob, os
    matches = glob.glob(os.path.join(str(tmp_path), "delta", "*", "n8n-data.tar.gz"))
    assert matches, "backup file not created"
    with gzip.open(matches[0], "rb") as gz:
        assert gz.read() == b"fake-tar-data"
    assert created["called"]


def test_resolve_container_stopped_returns_none(monkeypatch):
    inst = {"id": 1, "account_id": 1, "stack_name": "delta", "stack_id": 84,
            "environment_id": 8, "managed": 1, "container_id": ""}
    cs = [{"Names": ["/delta-n8n-1"], "State": "exited", "Id": "abc"}]
    monkeypatch.setattr("app.services.backup_ops.PortainerClient", lambda: type("P", (), {
        "list_containers": lambda self, env, all: cs
    })())
    assert backup_ops.resolve_container(inst) is None


def test_resolve_container_running_finds_stack_name(monkeypatch):
    inst = {"id": 1, "account_id": 1, "stack_name": "delta", "stack_id": 84,
            "environment_id": 8, "managed": 1, "container_id": ""}
    cs = [{"Names": ["/delta-n8n-1"], "State": "running", "Id": "abc123"},
          {"Names": ["/other-n8n-1"], "State": "running", "Id": "zzz"}]
    monkeypatch.setattr("app.services.backup_ops.PortainerClient", lambda: type("P", (), {
        "list_containers": lambda self, env, all: cs
    })())
    env, cid = backup_ops.resolve_container(inst)
    assert env == 8 and cid == "abc123"
