# Passkey (WebAuthn) tests — Steward 2026-09-03.
# WebAuthn itself needs a genuine authenticator (can't be unit-tested end-to-end
# without a device + browser ceremony), so these cover:
#   * the DB passkey/challenge schema round-trip
#   * registration options generation (challenge is stored + returned)
#   * authentication options generation for a registered passkey
#   * account-scoped list/delete/ownership guards
# A real registration/authentication verification needs a live browser + device
# (covered separately by the manual/live passkey E2E).

import json
import time

import pytest

from app import db
from app.config import settings


def _seed_account(tmp_path):
    db.init_db()
    conn = db.get_conn()
    conn.execute("DELETE FROM accounts WHERE email='pk@steprotech.com'")
    conn.execute(
        "INSERT INTO accounts (email, username, password_hash, account_state, subscription_status, quota, created_at)"
        " VALUES ('pk@steprotech.com','pkusertest','x','active','active',1,?)",
        (int(time.time()),))
    conn.commit()
    r = conn.execute("SELECT id FROM accounts WHERE email='pk@steprotech.com'").fetchone()
    conn.close()
    return r["id"]


def test_query_private_field_should_exist():
    assert hasattr(db, "add_passkey")
    assert hasattr(db, "list_passkeys")
    assert hasattr(db, "get_passkey_by_credential_id")
    assert hasattr(db, "save_passkey_challenge")
    assert hasattr(db, "get_latest_passkey_challenge")
    assert hasattr(db, "consume_passkey_challenge")


def test_registration_options_stores_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pk1.db"))
    from app.services import passkeys
    aid = _seed_account(tmp_path)
    opts_json = passkeys.registration_options("account", aid, "pk@steprotech.com")
    opts = json.loads(opts_json) if isinstance(opts_json, str) else opts_json
    assert opts["rp"]["id"]  # rp id present
    assert opts["user"]["id"]  # user handle present
    # challenge was persisted
    ch = db.get_latest_passkey_challenge("account", aid, "register")
    assert ch is not None
    assert ch["expires_at"] > int(time.time())


def test_authentication_options_requires_registered_passkey(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pk2.db"))
    from app.services import passkeys
    aid = _seed_account(tmp_path)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.authentication_options("account", aid)
    # add a passkey then it works
    db.add_passkey("account", aid, "cred-abc", b"\x00" * 32, 0, "usb", "My key")
    opts_json = passkeys.authentication_options("account", aid)
    opts = json.loads(opts_json) if isinstance(opts_json, str) else opts_json
    assert opts.get("allowCredentials")  # allowlist present


def test_list_and_delete_passkey_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pk3.db"))
    from app.services import passkeys
    aid = _seed_account(tmp_path)
    db.add_passkey("account", aid, "cred-1", b"\x00" * 32, 0, "", "A")
    db.add_passkey("account", aid, "cred-2", b"\x01" * 32, 0, "", "B")
    # admin scope empty
    assert passkeys.list_passkeys_for("admin") == []
    assert len(passkeys.list_passkeys_for("account", aid)) == 2
    # delete one
    assert passkeys.delete_passkey_for("account", aid, "cred-1") is True
    assert len(passkeys.list_passkeys_for("account", aid)) == 1
    # admin cannot delete an account passkey
    assert passkeys.delete_passkey_for("admin", aid, "cred-2") is False
    # deleting a non-existent returns False
    assert passkeys.delete_passkey_for("account", aid, "nope") is False


def test_challenge_single_use_consume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pk4.db"))
    aid = _seed_account(tmp_path)
    cid = db.save_passkey_challenge("account", aid, b"CHAL" * 8, "register", 600)
    assert db.get_latest_passkey_challenge("account", aid, "register")["id"] == cid
    db.consume_passkey_challenge(cid)
    assert db.get_latest_passkey_challenge("account", aid, "register") is None
