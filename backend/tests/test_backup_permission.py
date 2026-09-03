# Regression tests for the admin-gated backup permission (2026-09-03).
# A customer can only self-service backup when the admin enabled it; otherwise the
# backup endpoints 403 (defense in depth) and the account flag defaults to 0.
import json
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def account(monkeypatch, tmp_path):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bkperm.db"))
    import os
    for f in ["%s-wal" % (tmp_path / "bkperm.db"), "%s-shm" % (tmp_path / "bkperm.db")]:
        if os.path.exists(f):
            os.remove(f)
    db.init_db()
    return db.create_account("bk@steprotech.com", "bk", "BK", first_name="BK")


def test_backup_defaults_off(account):
    a = db.get_account(account)
    assert a["backup_enabled"] == 0, "backup permission must default to OFF"


def test_set_account_backup_on_then_off(account):
    db.set_account_backup(account, True)
    assert db.get_account(account)["backup_enabled"] == 1
    db.set_account_backup(account, False)
    assert db.get_account(account)["backup_enabled"] == 0


def test_db_init_idempotent_with_backup_column(account):
    # re-running init (e.g. on startup) must not error on the new column
    db.init_db()
    assert db.get_account(account)["backup_enabled"] == 0


# ---- API-level gate (defense in depth) ----


def _make_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bkapi.db"))
    db.init_db()
    return TestClient(app)


def _seed_token(tmp_path, monkeypatch):
    """Create an account with an instance + a client token, and an admin token."""
    from app.security import create_access_token, create_client_token
    client = _make_client(tmp_path, monkeypatch)
    from app import db
    aid = db.create_account("bkapi@steprotech.com", "bkapi", "BKAPI",
                            first_name="BK", last_name="API")
    iid = db.create_instance(account_id=aid, stack_name="bkapiws", environment_id=8,
                             environment_name="test", port=33001,
                             domain="bkapiws.steprotech.com", basic_auth_user="bkapi@steprotech.com",
                             basic_auth_password="BkApiPass123", n8n_encryption_key="e" * 32)
    db.update_instance(iid, status="healthy", stack_id=3)
    ctoken = create_client_token(aid)
    atoken = create_access_token("admin")
    return client, aid, iid, ctoken, atoken


def test_backup_endpoints_403_when_not_enabled(tmp_path, monkeypatch):
    client, aid, iid, ctoken, atoken = _seed_token(tmp_path, monkeypatch)
    h = {"Authorization": "Bearer " + ctoken}
    # default backup_enabled=0 -> all customer backup endpoints refuse
    assert client.get("/api/v1/me/backups", headers=h).status_code == 403
    assert client.post(f"/api/v1/me/instances/{iid}/backup?kind=full",
                       headers=h).status_code == 403
    assert client.get(f"/api/v1/me/backups/1/download", headers=h).status_code == 403


def test_admin_can_enable_backup_then_customer_gets_200(tmp_path, monkeypatch):
    client, aid, iid, ctoken, atoken = _seed_token(tmp_path, monkeypatch)
    ah = {"Authorization": "Bearer " + atoken}
    ch = {"Authorization": "Bearer " + ctoken}
    # admin grants backup
    r = client.put(f"/api/v1/admin/accounts/{aid}/backup", headers=ah,
                   json={"backup_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["backup_enabled"] is True
    # customer now allowed to list backups
    assert client.get("/api/v1/me/backups", headers=ch).status_code == 200
    # admin can revoke again
    assert client.put(f"/api/v1/admin/accounts/{aid}/backup", headers=ah,
                      json={"backup_enabled": False}).status_code == 200
    assert client.get("/api/v1/me/backups", headers=ch).status_code == 403


def test_admin_backup_toggle_requires_admin(tmp_path, monkeypatch):
    client, aid, iid, ctoken, atoken = _seed_token(tmp_path, monkeypatch)
    ch = {"Authorization": "Bearer " + ctoken}
    # a customer cannot toggle their own backup permission
    assert client.put(f"/api/v1/admin/accounts/{aid}/backup", headers=ch,
                      json={"backup_enabled": True}).status_code in (401, 403)