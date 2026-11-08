# Tests: 0.1.73 package display + per-package allowance.
#  - /plans carries `key` plus real `specs` derived from the stack templates
#  - the provision route gate counts the TARGET package (not the legacy quota)
#  - a legacy single-quota write also raises the current package's allowance

import sys
from pathlib import Path

import pytest

from app import db
from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_A = REPO_ROOT / "templates" / "n8n-stack-compose.yml"
TEMPLATE_B = REPO_ROOT / "templates" / "n8n-stack-compose-plus.yml"


@pytest.fixture
def pkg_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pkg.db"))
    monkeypatch.setattr(settings, "compose_template_path", str(TEMPLATE_A))
    monkeypatch.setattr(settings, "plan_b_compose_template_path", str(TEMPLATE_B))
    db.init_db()
    return tmp_path


def _account(email="pkg@example.com"):
    return db.create_account(email, email.split("@")[0], "Name",
                             first_name="F", last_name="L")


def test_specs_come_from_the_real_templates(pkg_db):
    from app.services import plan_specs
    a = {r["label"]: r["value"] for r in plan_specs.specs_for_plan("a")}
    b = {r["label"]: r["value"] for r in plan_specs.specs_for_plan("b")}
    assert a["vCPU"].startswith("0.30")
    assert a["Memory"] == "768 MB"
    assert a["Node.js heap"] == "384 MB"
    assert "Python code node" not in a, "Python is a Plus-only row"
    assert b["vCPU"].startswith("0.50")
    assert b["Memory"] == "1 GB"
    assert b["Node.js heap"] == "768 MB"
    assert "Python runner" in b["Python code node"]
    for rows in (a, b):
        assert "Storage" in rows
        assert "Execution history kept" in rows
        assert "Max run time" in rows


def test_specs_are_empty_when_the_template_is_unreadable(pkg_db, monkeypatch):
    """No numbers are better than invented ones: the UI falls back to its copy."""
    from app.services import plan_specs
    monkeypatch.setattr(settings, "compose_template_path", "/nope/missing.yml")
    assert plan_specs.specs_for_plan("a") == []


def test_plans_endpoint_exposes_key_and_specs(pkg_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.get("/api/v1/plans")
    assert r.status_code == 200
    plans = {p["key"]: p for p in r.json()["plans"]}
    assert set(plans) == {"a", "b"}
    assert any(row["label"] == "vCPU" for row in plans["a"]["specs"])
    assert any(row["label"] == "Python code node" for row in plans["b"]["specs"])


def test_provision_gate_counts_the_target_package(pkg_db, monkeypatch):
    """Regression (0.1.73): an account holding package A with one workspace that
    ALSO holds package B must be able to create its B workspace. The route read
    the legacy single quota and answered 409 even though B had a free slot."""
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_run_provision", lambda *a, **k: None)
    aid = _account("two@example.com")
    db.set_account_plan(aid, "b")
    db.set_account_quota_for(aid, "a", 1)
    db.set_account_quota_for(aid, "b", 1)
    db.set_account_quota(aid, 1)  # legacy field deliberately still 1
    db.create_instance(account_id=aid, stack_name="first", environment_id=4,
                       environment_name="n8n Server 1", port=32768,
                       domain="first.x.test", basic_auth_user="",
                       basic_auth_password="", n8n_encryption_key="k",
                       status="healthy", plan="a")
    db.update_subscription_status(aid, "active")
    hdr = {"Authorization": "Bearer " + security.create_client_token(aid)}
    r = TestClient(app).post(f"/api/v1/accounts/{aid}/provision", headers=hdr,
                             json={"password": "StrongPass123"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "provisioning_started"


def test_provision_gate_still_blocks_when_the_target_package_is_full(pkg_db, monkeypatch):
    """Each package's allowance is independent: a generous allowance on the OTHER
    package must not let this package's slot be overfilled."""
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_run_provision", lambda *a, **k: None)
    aid = _account("full@example.com")
    db.set_account_quota_for(aid, "a", 1)
    db.set_account_quota_for(aid, "b", 5)  # the other package's allowance
    db.create_instance(account_id=aid, stack_name="only", environment_id=4,
                       environment_name="n8n Server 1", port=32769,
                       domain="only.x.test", basic_auth_user="",
                       basic_auth_password="", n8n_encryption_key="k",
                       status="healthy", plan="a")
    db.update_subscription_status(aid, "active")
    hdr = {"Authorization": "Bearer " + security.create_client_token(aid)}
    r = TestClient(app).post(f"/api/v1/accounts/{aid}/provision", headers=hdr,
                             json={"password": "StrongPass123"})
    assert r.status_code == 409, r.text
    assert "quota" in r.json()["detail"].lower()


def test_legacy_db_write_mirrors_into_the_current_package(pkg_db):
    """The legacy setter is used by older scripts and tests; it must raise the
    allowance the provisioning gate actually reads, and leave the other package
    alone."""
    aid = _account("mirror@example.com")
    db.set_account_plan(aid, "b")
    db.set_account_quota(aid, 4)
    a = db.get_account(aid)
    assert a is not None
    assert a["quota"] == 4
    assert db.account_quota_for(a, "b") == 4
    assert db.account_quota_for(a, "a") == 1, "other package untouched"


def test_legacy_quota_write_mirrors_into_the_current_package(pkg_db):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    aid = _account("legacy@example.com")
    db.set_account_plan(aid, "b")
    hdr = {"Authorization": "Bearer " + security.create_access_token("admin")}
    r = TestClient(app).put(f"/api/v1/admin/accounts/{aid}/quota", headers=hdr,
                            json={"quota": 3})
    assert r.status_code == 200 and r.json()["plan"] == "b"
    a = db.get_account(aid)
    assert db.account_quota_for(a, "b") == 3, "gate reads the per-package value"
