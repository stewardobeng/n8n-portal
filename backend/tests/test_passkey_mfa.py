# Passkey as a SECOND factor (post-password MFA) — Steward 2026-09-03.
# These cover the new /auth/passkey/mfa/* and /admin/passkey/mfa/* endpoints,
# plus that enabled_methods()/_admin_methods() now surface 'passkey' when a
# passkey is registered. WebAuthn assertion verification needs a live browser +
# authenticator (covered by the manual/live E2E), so the endpoint tests focus
# on auth-gating, MFA-challenge binding, and method exposure.

import time
import pytest

from app import db
from app.config import settings
from app.security import create_access_token, create_mfa_token


def _seed_account(tmp_path):
    db.init_db()
    conn = db.get_conn()
    conn.execute("DELETE FROM accounts WHERE email='pkm@steprotech.com'")
    conn.execute(
        "INSERT INTO accounts (email, username, password_hash, account_state, subscription_status, quota, created_at)"
        " VALUES ('pkm@steprotech.com','pkm','x','active','active',1,?)",
        (int(time.time()),))
    conn.commit()
    r = conn.execute("SELECT id FROM accounts WHERE email='pkm@steprotech.com'").fetchone()
    conn.close()
    return r["id"]


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_enabled_methods_surfaces_passkey_then_totp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m1.db"))
    from app.services import account_security
    aid = _seed_account(tmp_path)
    # no passkey yet -> no passkey method
    assert not any(m["method"] == "passkey" for m in account_security.enabled_methods(aid))
    db.add_passkey("account", aid, "cred-abc", b"\x00" * 32, 0, "", "My key")
    meths = account_security.enabled_methods(aid)
    assert any(m["method"] == "passkey" for m in meths)
    # enabling TOTP adds it alongside passkey
    db.set_account_totp_enabled(aid, True)
    names = [m["method"] for m in account_security.enabled_methods(aid)]
    assert "passkey" in names and "totp" in names


def test_admin_methods_surfaces_passkey(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m2.db"))
    db.init_db()
    from app.services import account_security
    assert not any(m["method"] == "passkey" for m in account_security.admin_2fa_state()["methods"])
    db.add_passkey("admin", None, "cred-admin", b"\x00" * 32, 0, "", "Admin key")
    assert any(m["method"] == "passkey" for m in account_security.admin_2fa_state()["methods"])


def test_customer_passkey_mfa_start_requires_valid_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m3.db"))
    c = _client()
    aid = _seed_account(tmp_path)
    db.add_passkey("account", aid, "cred-x", b"\x00" * 32, 0, "", "k")
    # no challenge -> 422
    r = c.post("/api/v1/auth/passkey/mfa/start", json={})
    assert r.status_code == 422
    # bogus challenge -> 401/403
    r = c.post("/api/v1/auth/passkey/mfa/start", json={"challenge": "not-a-jwt" * 3})
    assert r.status_code in (401, 403)
    # valid challenge but account has no passkey in methods -> 403
    ch = create_mfa_token(aid, ["totp"])
    r = c.post("/api/v1/auth/passkey/mfa/start", json={"challenge": ch})
    assert r.status_code == 403
    # valid challenge with passkey method -> 200 options
    ch2 = create_mfa_token(aid, ["passkey"])
    r = c.post("/api/v1/auth/passkey/mfa/start", json={"challenge": ch2})
    assert r.status_code == 200, r.text
    assert "challenge" in r.json()


def test_customer_passkey_mfa_verify_rejects_without_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m4.db"))
    c = _client()
    aid = _seed_account(tmp_path)
    db.add_passkey("account", aid, "cred-x", b"\x00" * 32, 0, "", "k")
    ch = create_mfa_token(aid, ["passkey"])
    r = c.post("/api/v1/auth/passkey/mfa/verify", json={"challenge": ch, "credential": {"id": "bogus"}})
    assert r.status_code == 401  # assertion verification fails for unknown cred


def test_admin_passkey_mfa_start_requires_admin_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m5.db"))
    db.init_db()
    c = _client()
    db.add_passkey("admin", None, "cred-admin", b"\x00" * 32, 0, "", "Admin key")
    # customer-scoped challenge (acc:N) must NOT work for the admin endpoint
    ch_cust = create_mfa_token(5, ["passkey"])
    r = c.post("/api/v1/admin/passkey/mfa/start", json={"challenge": ch_cust})
    assert r.status_code == 403
    # admin challenge (acc:0) with passkey -> 200
    ch_adm = create_mfa_token(0, ["passkey"])
    r = c.post("/api/v1/admin/passkey/mfa/start", json={"challenge": ch_adm})
    assert r.status_code == 200, r.text
    assert "challenge" in r.json()


def test_admin_passkey_mfa_verify_rejects_without_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m6.db"))
    db.init_db()
    c = _client()
    db.add_passkey("admin", None, "cred-admin", b"\x00" * 32, 0, "", "Admin key")
    ch_adm = create_mfa_token(0, ["passkey"])
    r = c.post("/api/v1/admin/passkey/mfa/verify", json={"challenge": ch_adm, "credential": {"id": "bogus"}})
    assert r.status_code == 401


def test_admin_mfa_send_otp_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "m7.db"))
    db.init_db()
    c = _client()
    # email 2FA not enabled for admin -> 409
    r = c.post("/api/v1/admin/mfa-send-otp")
    assert r.status_code == 409
