# 0.1.77 (Steward, 2026-10-01): the emailed access CODE is gone.
# An admin APPROVAL is the gate: approving a request opens registration at once,
# the person is emailed that they are approved, and creating the account signs
# them in. 'token_sent' rows issued before this change keep working.

from app import db
from app.config import settings
from app.services import access_gate, emailer


def _seed(tmp_path, monkeypatch, email="approved@steprotech.com"):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "approve.db"))
    db.init_db()
    return db.create_access_request(email)


def _admin():
    from app.security import create_access_token
    return {"Authorization": "Bearer " + create_access_token("admin")}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_approve_records_no_code_and_emails_the_person(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    sent = []
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "send_access_approved", lambda to: sent.append(to))

    client = _client()
    r = client.post(f"/api/v1/admin/access-requests/{rid}/approve", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["email_sent"] is True

    req = db.get_access_request_by_id(rid)
    assert req["status"] == "approved"
    assert not req["token_hash"]            # nothing was generated
    assert req["token_sent_at"]             # the approval is stamped
    assert req["token_expires_at"] is None  # an approval does not expire
    assert sent == ["approved@steprotech.com"]


def test_email_failure_is_reported_and_not_fatal(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    import app.main as main_mod

    def boom(to):
        raise emailer.EmailError("smtp down")

    monkeypatch.setattr(main_mod, "send_access_approved", boom)
    client = _client()
    r = client.post(f"/api/v1/admin/access-requests/{rid}/approve", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["email_sent"] is False
    assert db.get_access_request_by_id(rid)["status"] == "approved"


def test_gate_opens_for_new_and_pre_change_approvals(tmp_path, monkeypatch):
    """0.1.98: a first-time address needs no human, so it opens registration at once.
    The admin Approve action and pre-0.1.77 'token_sent' rows still work, which is
    what keeps old rows and the Disapprove/Delete controls meaningful."""
    rid = _seed(tmp_path, monkeypatch, "gate@steprotech.com")
    db.create_access_request("legacy@steprotech.com")
    client = _client()

    # a request nobody approved opens the form anyway
    assert client.post("/api/v1/auth/check",
                       json={"email": "gate@steprotech.com"}).json()["action"] == "approved"
    access_gate.approve_request(rid)
    assert client.post("/api/v1/auth/check",
                       json={"email": "gate@steprotech.com"}).json()["action"] == "approved"

    # a request that was sent a code before 0.1.77 opens the same page
    legacy = db.get_access_request("legacy@steprotech.com")
    access_gate.issue_token(legacy["id"])
    assert client.post("/api/v1/auth/check",
                       json={"email": "legacy@steprotech.com"}).json()["action"] == "approved"


def test_register_without_a_code_and_signed_in(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch, "newbie@steprotech.com")
    access_gate.approve_request(rid)
    client = _client()
    r = client.post("/api/v1/accounts", json={
        "email": "newbie@steprotech.com", "username": None,
        "first_name": "New", "last_name": "Person",
        "password": "Str0ngPass1"})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    assert tok
    me = client.get("/api/v1/me", headers={"Authorization": "Bearer " + tok})
    assert me.status_code == 200 and me.json()["account"]["email"] == "newbie@steprotech.com"
    # the request is consumed: the person is a member now
    assert db.get_access_request("newbie@steprotech.com") is None


def test_register_still_refused_without_an_approval(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, "pending@steprotech.com")
    client = _client()
    r = client.post("/api/v1/accounts", json={
        "email": "pending@steprotech.com", "username": "pending",
        "first_name": "Pen", "last_name": "Ding",
        "password": "Str0ngPass1", "access_token": "AAAA-BBBB"})
    assert r.status_code == 403, r.text


def test_legacy_token_endpoint_now_approves(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch, "legacy2@steprotech.com")
    client = _client()
    r = client.post(f"/api/v1/admin/access-requests/{rid}/token", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert db.get_access_request_by_id(rid)["status"] == "approved"
