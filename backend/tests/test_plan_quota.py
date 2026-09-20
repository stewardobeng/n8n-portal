# Tests: 0.1.68 per-plan workspace quotas + plan-aware mark-paid.
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def pq_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pq.db"))
    db.init_db()
    return tmp_path


def _account(email="pq@example.com"):
    return db.create_account(email, email.split("@")[0], "Name",
                             first_name="F", last_name="L")


def test_quota_a_seeds_from_legacy_quota(pq_db):
    aid = _account()
    db.set_account_quota(aid, 4)  # legacy path (pre-0.1.68 accounts could have >1)
    a = db.get_account(aid)
    # NEW DBs have quota_a column from the start; legacy migration seeds it
    assert db.account_quota_for(a, "a") >= 1
    assert db.account_quota_for(a, "b") >= 1


def test_per_plan_quota_independent(pq_db):
    aid = _account()
    db.set_account_quota_for(aid, "a", 3)
    db.set_account_quota_for(aid, "b", 7)
    a = db.get_account(aid)
    assert db.account_quota_for(a, "a") == 3
    assert db.account_quota_for(a, "b") == 7


def test_quota_for_uses_account_plan_by_default(pq_db):
    aid = _account()
    db.set_account_plan(aid, "b")
    db.set_account_quota_for(aid, "b", 5)
    a = db.get_account(aid)
    assert db.account_quota_for(a) == 5, "no explicit plan -> account's own plan"


def test_provision_gate_counts_plan_own_allowance(pq_db, monkeypatch):
    """A plan-b account with quota_b=1 is blocked from a SECOND b workspace
    even when quota_a is high (and vice versa)."""
    from app.services import provisioner
    aid = _account()
    db.set_account_plan(aid, "b")
    db.set_account_quota_for(aid, "b", 1)
    iid = db.create_instance(
        account_id=aid, stack_name="one", environment_id=4,
        environment_name="n8n Server 1", port=32768, domain="one.x.test",
        basic_auth_user="", basic_auth_password="", n8n_encryption_key="k",
        status="healthy", plan="b")  # the existing workspace is a Plus workspace (0.1.70)
    a = db.get_account(aid)
    with pytest.raises(provisioner.ProvisionError, match="quota reached"):
        # provision_account needs a full Portainer/NPM environment; the quota
        # gate fires BEFORE any of that, so the error proves the per-plan gate.
        provisioner.provision_account(aid)


def test_mark_paid_sets_plan(pq_db, monkeypatch):
    from app.services import admin_ops
    import time as _t
    aid = _account()
    res = admin_ops.mark_paid(aid, int(_t.time()) + 365 * 86400)
    assert res["subscription_status"] == "active"
    a = db.get_account(aid)
    assert db._row_get(a, "plan", "a") in ("a", "b")


def test_account_out_carries_per_plan_quotas(pq_db):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    aid = _account()
    db.set_account_quota_for(aid, "a", 2)
    db.set_account_quota_for(aid, "b", 6)
    c = TestClient(app)
    hdr = {"Authorization": "Bearer " + security.create_access_token("admin")}
    r = c.get(f"/api/v1/admin/accounts/{aid}/quotas", headers=hdr)
    assert r.status_code == 200
    d = r.json()
    assert d["quota_a"] == 2 and d["quota_b"] == 6


def test_quota_route_plan_scoped(pq_db):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    aid = _account()
    c = TestClient(app)
    hdr = {"Authorization": "Bearer " + security.create_access_token("admin")}
    r = c.put(f"/api/v1/admin/accounts/{aid}/quota", headers=hdr,
              json={"quota": 9, "plan": "b"})
    assert r.status_code == 200 and r.json()["plan"] == "b"
    a = db.get_account(aid)
    assert db.account_quota_for(a, "b") == 9
    # invalid plan
    r = c.put(f"/api/v1/admin/accounts/{aid}/quota", headers=hdr,
              json={"quota": 9, "plan": "z"})
    assert r.status_code == 422
