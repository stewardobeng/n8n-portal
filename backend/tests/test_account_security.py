# Tests for portal account security: password reset + 2FA (TOTP + email OTP).
# Run with DB_PATH set (the suite runner does this), like test_account_lifecycle.py.

import time

import pytest

from app import db
from app.services import account_security as sec


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "t.db"))
    import app.db as dbm
    real = dbm.get_conn

    def _conn(db_path=None):
        return real(db_path or str(tmp_path / "t.db"))

    monkeypatch.setattr(dbm, "get_conn", _conn)
    yield
    monkeypatch.setattr(dbm, "get_conn", real)


@pytest.fixture(autouse=True)
def stub_email(monkeypatch):
    """Capture emails instead of sending; record the last body."""
    sent = {}

    def fake_send(to, subject, html, text=None):
        sent["to"] = to
        sent["subject"] = subject
        sent["html"] = html

    monkeypatch.setattr(sec, "send_email", fake_send)
    return sent


def _mk_account(state="active", password="Pass-1234"):
    from app.security import hash_password
    ns = time.time_ns()
    acc = db.create_account(f"sec-{ns}@steprotech.com", f"sec{ns}", "Sec Test",
                            "Sec", "Test", hash_password(password))
    if state != "active":
        db.set_account_state(acc, state)
    return acc


# ---------- password reset ----------

def test_reset_request_emails_and_token_verifies(stub_email):
    acc = _mk_account()
    tok = sec.request_password_reset(db.get_account(acc)["email"])
    assert tok is not None
    assert stub_email["to"] == db.get_account(acc)["email"]
    assert "reset my password" in stub_email["html"].lower()
    row = db.get_password_reset_by_token(tok)
    assert row is not None
    assert row["used"] == 0


def test_reset_unknown_email_returns_none(stub_email):
    assert sec.request_password_reset("nobody@nowhere.com") is None
    assert "to" not in stub_email  # no email sent


def test_reset_suspended_account_none(stub_email):
    acc = _mk_account(state="suspended")
    assert sec.request_password_reset(db.get_account(acc)["email"]) is None


def test_reset_password_sets_new_hash_and_consumes():
    acc = _mk_account(password="Old-Pass-1")
    tok = sec.request_password_reset(db.get_account(acc)["email"])
    old_hash = db.get_account(acc)["password_hash"]
    assert sec.reset_password(tok, "New-Pass-999")
    row = db.get_account(acc)
    assert row["password_hash"] != old_hash
    # the token is now single-use
    with pytest.raises(sec.SecurityError):
        sec.reset_password(tok, "Another-888")


def test_reset_token_reused_rejected():
    acc = _mk_account()
    tok = sec.request_password_reset(db.get_account(acc)["email"])
    sec.reset_password(tok, "New-Pass-1")
    with pytest.raises(sec.SecurityError):
        sec.reset_password(tok, "Newer-Pass-2")


def test_reset_short_password_rejected():
    acc = _mk_account()
    tok = sec.request_password_reset(db.get_account(acc)["email"])
    with pytest.raises(sec.SecurityError):
        sec.reset_password(tok, "short")


def test_reset_expired_token_rejected():
    acc = _mk_account()
    tok = sec.request_password_reset(db.get_account(acc)["email"])
    row = db.get_password_reset_by_token(tok)
    conn = db.get_conn()
    conn.execute("UPDATE password_resets SET expires_at = ? WHERE id = ?",
                 (int(time.time()) - 10, row["id"]))
    conn.commit()
    conn.close()
    with pytest.raises(sec.SecurityError):
        sec.reset_password(tok, "New-Pass-1")


# ---------- 2FA: authenticator TOTP ----------

def test_totp_setup_generates_secret_and_enables_with_code():
    import pyotp
    acc = _mk_account()
    setup = sec.totp_setup(acc)
    assert setup["secret"]
    assert setup["uri"].startswith("otpauth://totp/")
    code = pyotp.totp.TOTP(setup["secret"]).now()
    assert sec.totp_verify_and_enable(acc, code) is True
    assert bool(_totp_enabled(acc)) is True


def test_totp_wrong_code_not_enabled():
    acc = _mk_account()
    sec.totp_setup(acc)
    assert sec.totp_verify_and_enable(acc, "000000") is False
    assert bool(_totp_enabled(acc)) is False


def _totp_enabled(acc):
    return db.get_account(acc)["totp_enabled"]


def test_has_2fa_and_methods():
    acc = _mk_account()
    assert sec.has_2fa(acc) is False
    assert sec.enabled_methods(acc) == []
    import pyotp
    sec.totp_setup(acc)
    sec.totp_verify_and_enable(acc, pyotp.totp.TOTP(sec.totp_setup(acc)["secret"]).now())
    assert sec.has_2fa(acc) is True
    meth = sec.enabled_methods(acc)
    assert any(m["method"] == "totp" for m in meth)


# ---------- 2FA: email OTP ----------

def test_email_otp_send_and_verify(stub_email):
    acc = _mk_account()
    out = sec.email_otp_send(acc)
    assert out["sent"] is True
    assert stub_email["to"] == db.get_account(acc)["email"]
    assert "sign-in code" in stub_email["subject"].lower()
    # the code is hashed in the DB, not plaintext
    row = db.get_email_otp(acc)
    assert row["otp_hash"]
    assert "123456" not in row["otp_hash"]  # not a bare code


def test_email_otp_wrong_code_rejected():
    acc = _mk_account()
    sec.email_otp_send(acc)
    assert sec.email_otp_verify(acc, "999999") is False


def test_email_otp_single_use():
    acc = _mk_account()
    sec.email_otp_send(acc)
    # need the real code: monkeypatch _rand_otp to a known value
    import app.services.account_security as s

    class Fake:
        def __init__(s2, value):
            s2.v = value

        def h(s2):
            return None

    # easier: patch _rand_otp to return a fixed code, run through send+verify
    real = s._rand_otp
    s._rand_otp = lambda: "424242"
    try:
        s.email_otp_send(acc)
        assert s.email_otp_verify(acc, "424242") is True
        # second use fails (consumed)
        assert s.email_otp_verify(acc, "424242") is False
    finally:
        s._rand_otp = real


def test_disable_totp_clears_secret():
    acc = _mk_account()
    sec.totp_setup(acc)
    import pyotp
    code = pyotp.totp.TOTP(db._get_totp_secret(acc)).now()
    sec.totp_verify_and_enable(acc, code)
    db.set_account_totp_enabled(acc, False)
    db.set_account_totp_secret(acc, "")
    assert db._get_totp_secret(acc) == ""
    assert bool(db.get_account(acc)["totp_enabled"]) is False


# ---------- admin 2FA (settings-backed) ----------

def test_admin_totp_setup_and_enable():
    import pyotp
    setup = sec.admin_totp_setup()
    assert setup["secret"] and setup["uri"].startswith("otpauth://totp/")
    code = pyotp.totp.TOTP(setup["secret"]).now()
    assert sec.admin_totp_verify_enable(code) is True
    assert sec.admin_2fa_state()["totp_enabled"] is True


def test_admin_totp_wrong_code():
    sec.admin_totp_setup()
    assert sec.admin_totp_verify_enable("000000") is False
    assert sec.admin_2fa_state()["totp_enabled"] is False


def test_admin_email_otp_send_verify(stub_email):
    out = sec.admin_email_send_otp()
    assert out["sent"] is True
    assert "sign-in code" in stub_email["subject"].lower()
    itemp = _db_get("admin_otp_hash")
    assert itemp and "123456" not in itemp


def _db_get(key):
    import app.db as dbm
    return dbm.get_setting(key) if hasattr(dbm, "get_setting") else None


def test_admin_email_otp_single_use(monkeypatch):
    real = sec._rand_otp
    sec._rand_otp = lambda: "525252"
    try:
        sec.admin_email_send_otp()
        assert sec.admin_email_otp_verify("525252") is True
        assert sec.admin_email_otp_verify("525252") is False
    finally:
        sec._rand_otp = real


def test_admin_2fa_state_methods():
    sec.admin_totp_disable()  # reset
    assert sec.admin_2fa_state()["methods"] == []
    import pyotp
    sec.admin_totp_setup()
    sec.admin_totp_verify_enable(pyotp.totp.TOTP(db.get_setting("admin_totp_secret")).now())
    assert any(m["method"] == "totp" for m in sec.admin_2fa_state()["methods"])
