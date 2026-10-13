# 0.1.98 (Steward, 2026-10-11): THE APPROVAL QUEUE IS GONE.
#
# A first-time email opens the create-account form immediately. What replaces the
# human gate is the payment gate: a registered account is born unpaid
# (subscription_status 'none') and cannot provision a workspace until a verified
# payment marks it active. These tests pin both halves:
#
#   * the journey needs no human and no code, and
#   * a brand-new account still cannot get a workspace for free, and
#   * Disapprove remains a real block, so removing the queue did not remove control.

import app.services.billing as billing_mod
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.services import access_gate

EMAIL = "fresh@steprotech.com"


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "open_registration.db"))
    db.init_db()


def _client():
    return TestClient(app)


def _register(client, email=EMAIL, username="fresh", password="Str0ngPass1"):
    return client.post("/api/v1/accounts", json={
        "email": email, "username": username,
        "first_name": "Fresh", "last_name": "Person", "password": password,
    })


def test_the_first_visit_opens_the_form_and_leaves_a_record(tmp_path, monkeypatch):
    """No waiting, no code, and still one row for the admin's audit trail."""
    _fresh_db(tmp_path, monkeypatch)
    r = _client().post("/api/v1/auth/check", json={"email": EMAIL})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "approved", r.text

    req = db.get_access_request(EMAIL)
    assert req is not None, "the attempt is still recorded"
    assert req["status"] == "approved"
    assert not req["token_hash"], "no code is ever generated now"
    assert req["token_sent_at"] is not None, "stamped like an admin approval"
    assert req["token_expires_at"] is None, "an open door does not expire"


def test_asking_again_is_stable_and_creates_no_second_row(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    client = _client()
    for _ in range(3):
        assert client.post("/api/v1/auth/check",
                           json={"email": EMAIL}).json()["action"] == "approved"
    assert len([q for q in db.list_access_requests() if q["email"] == EMAIL]) == 1


def test_the_whole_journey_needs_no_human(tmp_path, monkeypatch):
    """check -> register -> signed in, with nothing typed but the form itself."""
    _fresh_db(tmp_path, monkeypatch)
    client = _client()
    assert client.post("/api/v1/auth/check",
                       json={"email": EMAIL}).json()["action"] == "approved"

    r = _register(client)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token

    me = client.get("/api/v1/me", headers={"Authorization": "Bearer " + token})
    assert me.status_code == 200
    assert me.json()["account"]["email"] == EMAIL
    # becoming a member consumes the request, exactly as it did before
    assert db.get_access_request(EMAIL) is None


def test_a_row_left_from_the_approval_era_opens_the_form(tmp_path, monkeypatch):
    """Somebody who asked to be approved before this change is not left waiting."""
    _fresh_db(tmp_path, monkeypatch)
    rid = db.create_access_request("waiting@steprotech.com")
    before = db.get_access_request_by_id(rid)
    assert before is not None and before["status"] == "requested"

    r = _client().post("/api/v1/auth/check", json={"email": "waiting@steprotech.com"})
    assert r.json()["action"] == "approved"
    after = db.get_access_request_by_id(rid)
    assert after is not None and after["status"] == "approved"


def test_a_cancelled_row_opens_the_form(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    rid = db.create_access_request("canceled@steprotech.com")
    db.update_access_request(rid, status="canceled")
    r = _client().post("/api/v1/auth/check", json={"email": "canceled@steprotech.com"})
    assert r.json()["action"] == "approved"


def test_disapprove_is_still_a_real_block(tmp_path, monkeypatch):
    """Removing the queue must not remove the admin's ability to keep someone out."""
    _fresh_db(tmp_path, monkeypatch)
    rid = db.create_access_request("blocked@steprotech.com")
    access_gate.deny_request(rid)
    client = _client()

    assert client.post("/api/v1/auth/check",
                       json={"email": "blocked@steprotech.com"}).json()["action"] == "denied"
    # and it stays denied however many times the address asks again
    assert client.post("/api/v1/auth/check",
                       json={"email": "blocked@steprotech.com"}).json()["action"] == "denied"

    r = _register(client, email="blocked@steprotech.com", username="blocked")
    assert r.status_code == 403, r.text
    assert db.get_account_by_email("blocked@steprotech.com") is None


def test_an_existing_account_still_signs_in(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    client = _client()
    client.post("/api/v1/auth/check", json={"email": EMAIL})
    _register(client)
    assert client.post("/api/v1/auth/check",
                       json={"email": EMAIL}).json()["action"] == "login"


def test_an_open_door_still_cannot_get_a_free_workspace(tmp_path, monkeypatch):
    """The point of the change: payment, not a human click, protects the workspace.

    With a live gateway a freshly registered account is unpaid, so the provision
    route refuses it with 402 even though the caller owns the account.
    """
    _fresh_db(tmp_path, monkeypatch)
    old = billing_mod.settings.payment_gateway
    billing_mod.settings.payment_gateway = "paystack"
    try:
        client = _client()
        client.post("/api/v1/auth/check", json={"email": EMAIL})
        r = _register(client)
        assert r.status_code == 201, r.text
        account = r.json()["account"]
        token = r.json()["token"]

        assert account["subscription_status"] == "none", "a new account is born unpaid"

        # the alternative route, in case anyone tries to skip the entry page
        direct = client.post("/api/v1/accounts", json={
            "email": "sneaky@steprotech.com", "username": "sneaky",
            "first_name": "S", "last_name": "N", "password": "Str0ngPass1",
        })
        assert direct.status_code == 403, "registering without the check still needs the row"

        prov = client.post(f"/api/v1/accounts/{account['id']}/provision",
                           headers={"Authorization": "Bearer " + token},
                           json={"password": "Str0ngPass1"})
        assert prov.status_code == 402, prov.text
        assert "Payment required" in prov.json()["detail"]
        assert db.list_instances(account["id"]) == [], "no workspace was created"
    finally:
        billing_mod.settings.payment_gateway = old


def test_a_checkout_needs_a_signed_in_owner(tmp_path, monkeypatch):
    """Checkout is owner-or-admin, so the amount can never be chosen by a stranger."""
    _fresh_db(tmp_path, monkeypatch)
    client = _client()
    client.post("/api/v1/auth/check", json={"email": EMAIL})
    account = _register(client).json()["account"]
    anon = client.post(f"/api/v1/accounts/{account['id']}/checkout", json={"plan": "a"})
    assert anon.status_code == 401, anon.text
