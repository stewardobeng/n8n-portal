# Tests for the admin payments master switch + free expiry extension
# (Steward 2026-09-03: hold payments while onboarding users who must not pay,
# and renew/extend selected users for free by whole years).
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def wdb(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ws.db"))
    db.init_db()
    return tmp_path


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "hold.db"))
    db.init_db()
    return TestClient(app)


def _seed(tmp_path, monkeypatch, gateway="mock"):
    from app.security import create_access_token, create_client_token
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "payment_gateway", gateway)
    aid = db.create_account("hold@steprotech.com", "holduser", "Hold User",
                            first_name="H", last_name="U",
                            password_hash="x")
    ctoken = create_client_token(aid)
    atoken = create_access_token("admin")
    return client, aid, ctoken, atoken


def _h(token):
    return {"Authorization": "Bearer " + token}


# ---- payments master switch (admin settings) ----

def test_settings_default_payments_open(wdb):
    from app.services import billing
    assert billing.payments_open() is True


def test_admin_toggles_payments_and_reads_back(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    g = client.get("/api/v1/admin/settings", headers=_h(atoken))
    assert g.status_code == 200 and g.json()["payments_open"] is True
    # hold ON
    r = client.put("/api/v1/admin/settings",
                   json={"payments_open": False}, headers=_h(atoken))
    assert r.status_code == 200 and r.json()["payments_open"] is False
    # hold OFF again
    r = client.put("/api/v1/admin/settings",
                   json={"payments_open": True}, headers=_h(atoken))
    assert r.status_code == 200 and r.json()["payments_open"] is True


def test_settings_envs_and_payments_do_not_clobber(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.put("/api/v1/admin/settings",
                   json={"landing_environments": "8,4"}, headers=_h(atoken))
    assert r.status_code == 200
    r = client.put("/api/v1/admin/settings",
                   json={"payments_open": False}, headers=_h(atoken))
    assert r.status_code == 200
    assert r.json()["payments_open"] is False
    g = client.get("/api/v1/admin/settings", headers=_h(atoken))
    assert g.json()["landing_environments"] == "8,4"
    assert g.json()["payments_open"] is False


def test_settings_requires_something_to_update(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.put("/api/v1/admin/settings", json={}, headers=_h(atoken))
    assert r.status_code == 422


def test_non_admin_cannot_toggle_payments(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.put("/api/v1/admin/settings",
                   json={"payments_open": False},
                   headers=_h(ctoken))
    assert r.status_code == 401
    r = client.get("/api/v1/admin/settings", headers=_h(ctoken))
    assert r.status_code == 401


# ---- checkout blocked while on hold ----

def test_checkout_blocked_while_hold_and_open_after(tmp_path, monkeypatch):
    from app.security import create_client_token
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    db.set_account_status(aid, "pending_subscription")
    # open -> mock checkout URL issued
    r = client.post(f"/api/v1/accounts/{aid}/checkout",
                    headers=_h(ctoken))
    assert r.status_code == 200 and r.json()["gateway"] == "mock"
    # hold ON -> refused for everyone
    client.put("/api/v1/admin/settings",
               json={"payments_open": False}, headers=_h(atoken))
    r = client.post(f"/api/v1/accounts/{aid}/checkout",
                    headers=_h(ctoken))
    assert r.status_code == 403 and "hold" in r.json()["detail"].lower()
    # admin re-opens -> paying customers can subscribe again
    client.put("/api/v1/admin/settings",
               json={"payments_open": True}, headers=_h(atoken))
    r = client.post(f"/api/v1/accounts/{aid}/checkout",
                    headers=_h(ctoken))
    assert r.status_code == 200


def test_plans_expose_payments_open(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.get("/api/v1/plans", headers=_h(ctoken))
    assert r.status_code == 200 and r.json()["payments_open"] is True
    client.put("/api/v1/admin/settings",
               json={"payments_open": False}, headers=_h(atoken))
    r = client.get("/api/v1/plans", headers=_h(ctoken))
    assert r.json()["payments_open"] is False


# ---- free expiry extension ----

def test_extend_sets_active_and_adds_years(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    db.update_subscription_status(aid, "active", paid_until=1_800_000_000)
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 3}, headers=_h(atoken))
    assert r.status_code == 200
    body = r.json()
    assert body["extended_years"] == 3
    assert body["subscription_status"] == "active"
    assert body["paid_until"] == 1_800_000_000 + 3 * 365 * 86400
    acc = db.get_account(aid)
    assert acc["subscription_status"] == "active"
    assert acc["paid_until"] == 1_800_000_000 + 3 * 365 * 86400


def test_extend_from_today_when_no_or_past_expiry(tmp_path, monkeypatch):
    import time
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    db.update_subscription_status(aid, "unpaid", paid_until=1)  # long past
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 2}, headers=_h(atoken))
    assert r.status_code == 200
    assert r.json()["paid_until"] > int(time.time()) + 2 * 364 * 86400


def test_extend_must_be_one_to_ten_years(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 0}, headers=_h(atoken))
    assert r.status_code == 422
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 11}, headers=_h(atoken))
    assert r.status_code == 422
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={}, headers=_h(atoken))
    assert r.status_code == 422


def test_extend_resumes_locked_workspace(tmp_path, monkeypatch):
    from app.services import admin_ops
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    inst_id = db.create_instance(
        account_id=aid, stack_name="holdstack", environment_id=8,
        environment_name="test", port=33110, domain="holdstack.steprotech.com",
        basic_auth_user="x@y.com", basic_auth_password="Xy123456",
        n8n_encryption_key="e" * 32)
    db.update_instance(inst_id, locked=1)
    started = []

    def fake_start(aid_):
        started.append(aid_)
        return True

    monkeypatch.setattr(admin_ops, "_ensure_started", fake_start)
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 1}, headers=_h(atoken))
    assert r.status_code == 200
    assert started == [aid]  # locked workspace was resumed for free


def test_extend_ignores_suspended_state_workspace(tmp_path, monkeypatch):
    """Admin lifecycle outranks billing: a suspended account's workspace stays
    off even when its expiry is extended for free (mirror of mark-paid)."""
    from app.services import admin_ops
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    inst_id = db.create_instance(
        account_id=aid, stack_name="susstack", environment_id=8,
        environment_name="test", port=33111, domain="susstack.steprotech.com",
        basic_auth_user="x@y.com", basic_auth_password="Xy123456",
        n8n_encryption_key="e" * 32)
    db.update_instance(inst_id, locked=1)
    db.set_account_state(aid, "suspended")
    started = []

    def fake_start(aid_):
        started.append(aid_)
        return True

    monkeypatch.setattr(admin_ops, "_ensure_started", fake_start)
    r = client.post(f"/api/v1/admin/accounts/{aid}/extend",
                    json={"years": 2}, headers=_h(atoken))
    assert r.status_code == 200
    assert started == []  # suspended: money recorded, access NOT granted
    assert db.get_account(aid)["paid_until"] > 0


def test_extend_unknown_account_404(tmp_path, monkeypatch):
    client, aid, ctoken, atoken = _seed(tmp_path, monkeypatch)
    r = client.post("/api/v1/admin/accounts/99999/extend",
                    json={"years": 2}, headers=_h(atoken))
    assert r.status_code == 404
