# Admin access-request disapprove + delete endpoints (Steward 2026-09-03).
# The admin requests dashboard previously only offered "Approve"; now an admin can
# disapprove a request (terminal 'denied' state, clears any issued code, blocks
# registration) or delete a request row entirely (email may re-request later).

import os
import time

from app import db
from app.config import settings
from app.services import access_gate


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ar.db"))
    db.init_db()
    rid = db.create_access_request("request@" + os.getenv("TEST_DOMAIN", "steprotech.com"))
    return rid


def _admin_token():
    from app.security import create_access_token
    return create_access_token("admin")


def test_deny_endpoint_admin_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    rid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    r = client.post(f"/api/v1/admin/access-requests/{rid}/deny")
    assert r.status_code in (401, 403), r.status_code


def test_deny_marks_terminal_and_blocks_registration(tmp_path, monkeypatch):
    """Deny sets status 'denied', clears the code, and the entry gate returns
    action 'denied' so the person cannot proceed to register."""
    from fastapi.testclient import TestClient
    from app.main import app
    rid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + _admin_token()}

    # first issue a token so we can prove deny clears it
    tok_r = client.post(f"/api/v1/admin/access-requests/{rid}/token", headers=h)
    assert tok_r.status_code == 200, tok_r.text
    tok = tok_r.json()["token"]
    req_before = db.get_access_request_by_id(rid)
    assert req_before["status"] == "token_sent" and req_before["token_hash"]

    # gate should now report token (code sent)
    email = "request@" + os.getenv("TEST_DOMAIN", "steprotech.com")
    gate_r = client.post("/api/v1/auth/check", json={"email": email})
    assert gate_r.json()["action"] == "token"

    # deny it
    d = client.post(f"/api/v1/admin/access-requests/{rid}/deny", headers=h)
    assert d.status_code == 200, d.text
    assert d.json()["status"] == "denied"

    req_after = db.get_access_request_by_id(rid)
    assert req_after["status"] == "denied"
    assert not req_after["token_hash"]
    assert req_after["token_sent_at"] is None

    # gate now reports denied
    gate_r2 = client.post("/api/v1/auth/check", json={"email": email})
    assert gate_r2.json()["action"] == "denied"

    # register must be rejected even with the old token
    reg = client.post("/api/v1/accounts", json={
        "email": email, "username": "requser", "first_name": "R", "last_name": "U",
        "password": "Abcdef123", "access_token": tok,
    })
    assert reg.status_code in (403, 409), reg.status_code


def test_deny_already_registered_rejected(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    rid = _seed(tmp_path, monkeypatch)
    db.update_access_request(rid, status="registered", registered_at=int(time.time()))
    client = TestClient(app)
    h = {"Authorization": "Bearer " + _admin_token()}
    r = client.post(f"/api/v1/admin/access-requests/{rid}/deny", headers=h)
    assert r.status_code == 409, r.text


def test_delete_endpoint_admin_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    rid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    r = client.delete(f"/api/v1/admin/access-requests/{rid}")
    assert r.status_code in (401, 403), r.status_code


def test_delete_removes_row(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    rid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + _admin_token()}
    r = client.delete(f"/api/v1/admin/access-requests/{rid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert db.get_access_request_by_id(rid) is None


def test_delete_unknown_404(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + _admin_token()}
    r = client.delete("/api/v1/admin/access-requests/999999", headers=h)
    assert r.status_code == 404, r.text
