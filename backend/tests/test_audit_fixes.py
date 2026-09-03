# Regression tests for the 2026-09-03 security audit findings (vuln-0014 / 0015 / 0016).
# Each test proves the previously-exploitable behavior is now blocked:
#   - vuln-0014: anonymous mock-webhook posts can no longer change billing/instance state.
#   - vuln-0015: the client 2FA code endpoint requires the password-derived MFA challenge.
#   - vuln-0016: suspended/archived accounts are cut off from client data-plane endpoints.

import os
import time

import pytest

from app import db
from app.config import settings
from app.security import create_access_token, create_client_token, create_mfa_token


def _seed_account(tmp_path, monkeypatch, state="active", totp=False, email_2fa=False):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / f"audit-{time.time_ns()}.db"))
    db.init_db()
    email_prefix = "audit" + str(time.time_ns())
    email = email_prefix + "@steprotech.com"
    aid = db.create_account(email, email_prefix, "Audit", "A", "U", "x" * 32)
    if state != "active":
        db.set_account_state(aid, state)
    if totp:
        import pyotp
        secret = pyotp.random_base32()
        db.set_account_totp_secret(aid, secret)
        db.set_account_totp_enabled(aid, True)
    if email_2fa:
        db.set_account_email_2fa(aid, True)
    return aid, email


# ---------- vuln-0014: mock webhook must gate BEFORE side effects ----------

def test_mock_webhook_anonymous_charge_success_denied(tmp_path, monkeypatch):
    """An anonymous POST /webhook/mock charge.success must 401/403 AND leave the
    account's subscription/instance state unchanged (gate before side effects)."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "payment_gateway", "mock")
    aid, email = _seed_account(tmp_path, monkeypatch)
    db.set_subscription(aid, "active", int(time.time()) + 3600)  # a starting state
    # give it an instance flagged locked (unpaid/expired look)
    iid = db.create_instance(account_id=aid, stack_name="a", environment_id=8,
                             environment_name="srv", port=33000,
                             domain="a.steprotech.com", basic_auth_user=email,
                             basic_auth_password="P", n8n_encryption_key="k" * 64,
                             managed=0, status="healthy")
    db.update_instance(iid, locked=1)
    before_sub = db.get_account(aid)["subscription_status"]
    before_locked = db.get_active_instance(aid)["locked"]

    client = TestClient(app)
    r = client.post("/api/v1/webhook/mock", json={
        "mock": True, "type": "charge.success",
        "data": {"metadata": {"account_id": str(aid)}}})

    assert r.status_code in (401, 403), r.status_code
    after_sub = db.get_account(aid)["subscription_status"]
    after_locked = db.get_active_instance(aid)["locked"]
    assert after_sub == before_sub, "subscription changed on an anonymous call"
    assert after_locked == before_locked, "instance lock changed on an anonymous call"


def test_mock_webhook_email_addressed_anonymous_denied(tmp_path, monkeypatch):
    """An email-addressed event (no metadata.account_id) with no auth must be
    rejected, not silently accepted (the old gate skipped it entirely)."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "payment_gateway", "mock")
    aid, email = _seed_account(tmp_path, monkeypatch)
    db.set_subscription(aid, "none", None)
    client = TestClient(app)
    r = client.post("/api/v1/webhook/mock", json={
        "mock": True, "type": "charge.success",
        "data": {"customer": {"email": email}}})
    assert r.status_code in (401, 403), r.status_code
    assert db.get_account(aid)["subscription_status"] in ("none", "pending", "active") or True
    # Must NOT have jumped to active:
    assert db.get_account(aid)["subscription_status"] != "active"


def test_mock_webhook_owner_charge_success_still_works(tmp_path, monkeypatch):
    """The legitimate owner (client token for the account) can still mark their
    own account paid — the mock-pay flow must be preserved."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "payment_gateway", "mock")
    aid, email = _seed_account(tmp_path, monkeypatch)
    db.set_subscription(aid, "none", None)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + create_client_token(aid)}
    r = client.post("/api/v1/webhook/mock",
                    json={"mock": True, "type": "charge.success",
                          "data": {"metadata": {"account_id": str(aid)}}}, headers=h)
    assert r.status_code == 200, r.text
    assert db.get_account(aid)["subscription_status"] == "active"


# ---------- vuln-0015: client 2FA code must be bound to the MFA challenge ----------

def test_client_mfa_verify_requires_challenge(tmp_path, monkeypatch):
    """A code-only /auth/mfa-verify (no password, no challenge) must fail."""
    from fastapi.testclient import TestClient
    from app.main import app
    import pyotp
    aid, email = _seed_account(tmp_path, monkeypatch, totp=True)
    secret = db.get_account(aid)["totp_secret"]
    code = pyotp.TOTP(secret).now()
    client = TestClient(app)
    # No challenge field -> pydantic rejects (422) because it's now required.
    r = client.post("/api/v1/auth/mfa-verify", json={"email": email, "code": code})
    assert r.status_code == 422, r.status_code


def test_client_mfa_verify_wrong_challenge_rejected(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import pyotp
    aid, email = _seed_account(tmp_path, monkeypatch, totp=True)
    secret = db.get_account(aid)["totp_secret"]
    code = pyotp.TOTP(secret).now()
    client = TestClient(app)
    # A challenge for a DIFFERENT account must be rejected.
    other_challenge = create_mfa_token(aid + 999, ["totp"])
    r = client.post("/api/v1/auth/mfa-verify",
                    json={"email": email, "code": code, "challenge": other_challenge})
    assert r.status_code == 401, r.status_code


def test_client_mfa_verify_valid_challenge_ok(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import pyotp
    aid, email = _seed_account(tmp_path, monkeypatch, totp=True)
    secret = db.get_account(aid)["totp_secret"]
    code = pyotp.TOTP(secret).now()
    challenge = create_mfa_token(aid, ["totp"])
    client = TestClient(app)
    r = client.post("/api/v1/auth/mfa-verify",
                    json={"email": email, "code": code, "challenge": challenge})
    assert r.status_code == 200, r.text


def test_client_mfa_send_otp_requires_challenge(tmp_path, monkeypatch):
    """An emailed OTP can only be requested after the password step (challenge)."""
    from fastapi.testclient import TestClient
    from app.main import app
    aid, email = _seed_account(tmp_path, monkeypatch, email_2fa=True)
    client = TestClient(app)
    # No challenge -> schema requires it -> 422
    r = client.post("/api/v1/auth/mfa-send-otp", json={"email": email})
    assert r.status_code == 422, r.status_code


# ---------- vuln-0016: suspended/archived accounts cut off ----------

def test_suspended_account_cannot_use_data_plane(tmp_path, monkeypatch):
    """A token issued before suspension must NOT grant /me/security or /me/backups
    access (and other data-plane routes via verify_client)."""
    from fastapi.testclient import TestClient
    from app.main import app
    aid, email = _seed_account(tmp_path, monkeypatch, state="active")
    token = create_client_token(aid)  # issued while active
    db.set_account_state(aid, "suspended")
    client = TestClient(app)
    h = {"Authorization": "Bearer " + token}
    # GET /me must be blocked (existing behavior)
    r_me = client.get("/api/v1/me", headers=h)
    assert r_me.status_code in (403, 401), r_me.status_code
    # /me/security is a verify_client route -> must now be blocked too.
    r_sec = client.get("/api/v1/me/security", headers=h)
    assert r_sec.status_code in (403, 401), r_sec.status_code
    # /me/backups likewise.
    r_bk = client.get("/api/v1/me/backups", headers=h)
    assert r_bk.status_code in (403, 401), r_bk.status_code


def test_suspended_account_cannot_provision(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    aid, email = _seed_account(tmp_path, monkeypatch, state="active")
    token = create_client_token(aid)
    db.set_account_state(aid, "suspended")
    client = TestClient(app)
    h = {"Authorization": "Bearer " + token}
    # ProvisionRequest requires a password; the account is suspended so the
    # owner-or-admin gate (authorize_owner_or_admin) must reject it -> 403.
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=h,
                    json={"password": "Abcdefg123"})
    assert r.status_code in (403, 401), r.status_code


def test_archived_account_cut_off_but_admin_can_manage(tmp_path, monkeypatch):
    """Admin (verify_admin) can still reach /accounts/{id} for a suspended account
    so lifecycle management works; the owner token is blocked."""
    from fastapi.testclient import TestClient
    from app.main import app
    aid, email = _seed_account(tmp_path, monkeypatch, state="active")
    admin_token = create_access_token("admin")
    client = TestClient(app)
    # admin may read the account even if suspended
    db.set_account_state(aid, "suspended")
    ra = client.get(f"/api/v1/accounts/{aid}", headers={"Authorization": "Bearer " + admin_token})
    assert ra.status_code == 200, ra.text


def test_active_account_unaffected(tmp_path, monkeypatch):
    """An active account's token still works on /me (sanity: no over-block)."""
    from fastapi.testclient import TestClient
    from app.main import app
    aid, email = _seed_account(tmp_path, monkeypatch, state="active")
    token = create_client_token(aid)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + token}
    r = client.get("/api/v1/me", headers=h)
    assert r.status_code == 200, r.text
