# Tests for extra-workspace provisioning (2026-09-03):
#  - live username availability endpoint (client-authed, DB + NPM checks)
#  - /accounts/{id}/provision accepts a per-workspace profile (unique username,
#    owner email, names, password) and validates it before starting the job
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
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "wsapi.db"))
    db.init_db()
    return TestClient(app)


def _seed(tmp_path, monkeypatch):
    from app.security import create_access_token, create_client_token
    client = _client(tmp_path, monkeypatch)
    aid = db.create_account("ws@steprotech.com", "wsmain", "WS Main",
                            first_name="W", last_name="S",
                            password_hash="x")
    ctoken = create_client_token(aid)
    atoken = create_access_token("admin")
    return client, aid, ctoken, atoken


def _h(token):
    return {"Authorization": "Bearer " + token}


# ---- username availability ----

def test_availability_format_and_reserved(wdb):
    from app.services import provisioner
    ok, msg = provisioner.username_available("")
    assert not ok and msg
    ok, msg = provisioner.username_available("UPPER_name!")
    assert not ok
    ok, msg = provisioner.username_available("-dash")
    assert not ok
    ok, msg = provisioner.username_available("admin")
    assert not ok and "reserved" in msg


def test_availability_account_and_stack_taken(wdb):
    from app.services import provisioner
    aid = db.create_account("taken@steprotech.com", "takenslot", "T",
                            first_name="T", last_name="T")
    ok, msg = provisioner.username_available("takenslot")
    assert not ok and "already taken" in msg
    db.create_instance(account_id=aid, stack_name="takenstack", environment_id=8,
                       environment_name="test", port=33099,
                       domain="takenstack.steprotech.com",
                       basic_auth_user="x@y.com", basic_auth_password="Xy123456",
                       n8n_encryption_key="e" * 32)
    ok, msg = provisioner.username_available("takenstack")
    assert not ok and "already used" in msg


def test_availability_free_and_npm_blocked(wdb, monkeypatch):
    from app.services import provisioner
    from app.services.npm_client import NPMClient
    # no portal usage -> free
    ok, msg = provisioner.username_available("freshbox")
    assert ok and not msg
    # but an NPM proxy host already owns the domain (legacy fleet)
    monkeypatch.setattr(NPMClient, "list_proxy_hosts",
                        lambda self: [{"domain_names": ["freshbox.steprotech.com"]}])
    ok, msg = provisioner.username_available("freshbox")
    assert not ok and "already exists" in msg


# ---- availability endpoint (API level) ----

def test_availability_api_gates_and_shape(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_client_token
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "avail.db"))
    db.init_db()
    client = TestClient(app)
    aid = db.create_account("avail@steprotech.com", "availslot", "Av",
                            first_name="A", last_name="V")
    ctoken = create_client_token(aid)
    # no token -> 401
    assert client.get("/api/v1/username-available?username=x").status_code == 401
    r = client.get("/api/v1/username-available?username=freemind",
                   headers=_h(ctoken))
    assert r.status_code == 200
    assert r.json()["available"] is True
    r = client.get("/api/v1/username-available?username=availslot",
                   headers=_h(ctoken))
    assert r.status_code == 200 and r.json()["available"] is False


# ---- provision route: per-workspace profile ----

def test_provision_validates_profile_before_quota(tmp_path, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pv.db"))
    db.init_db()
    from fastapi.testclient import TestClient
    from app.security import create_client_token
    client = TestClient(app)
    aid = db.create_account("pv@steprotech.com", "pvmain", "PV",
                            first_name="P", last_name="V")
    ctoken = create_client_token(aid)
    h = _h(ctoken)
    # bad username format
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=h,
                    json={"password": "Str0ngPass", "username": "BAD_NAME!"})
    assert r.status_code == 422
    # taken username
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=h,
                    json={"password": "Str0ngPass", "username": "pvmain"})
    assert r.status_code == 422
    # weak password
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=h,
                    json={"password": "weak", "username": "secondbox"})
    assert r.status_code == 422
    # invalid owner email shape
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=h,
                    json={"password": "Str0ngPass", "username": "secondbox",
                          "owner_email": "not-an-email"})
    assert r.status_code == 422


def test_provision_passes_workspace_profile_to_job(tmp_path, monkeypatch):
    import app.main as mainmod
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "pvok.db"))
    db.init_db()
    from fastapi.testclient import TestClient
    from app.security import create_client_token, create_access_token
    client = TestClient(app=mainmod.app)
    aid = db.create_account("pvok@steprotech.com", "pvokmain", "PVOK",
                            first_name="P", last_name="V")
    # raise quota so a second workspace is allowed
    db.set_account_quota(aid, 2)
    # subscription active so the payment gate passes
    db.update_subscription_status(aid, "active")
    # first workspace exists (healthy) so live count = 1 < quota 2
    db.create_instance(account_id=aid, stack_name="pvokmain", environment_id=8,
                       environment_name="test", port=33100,
                       domain="pvokmain.steprotech.com",
                       basic_auth_user="pvok@steprotech.com",
                       basic_auth_password="Str0ngPass1", n8n_encryption_key="e" * 32)
    db.update_instance(1, status="healthy")
    ctoken = create_client_token(aid)
    captured = {}
    monkeypatch.setattr(mainmod, "_run_provision",
                        lambda account_id, password, workspace: captured.update(
                            account_id=account_id, password=password,
                            workspace=workspace))
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=_h(ctoken),
                    json={"password": "SecondPass1", "username": "secondbox",
                          "owner_email": "Owner@Example.com",
                          "first_name": "Second", "last_name": "Owner"})
    assert r.status_code == 200, r.text
    assert captured["account_id"] == aid
    assert captured["password"] == "SecondPass1"
    assert captured["workspace"] == {
        "username": "secondbox",
        "owner_email": "owner@example.com",
        "first_name": "Second",
        "last_name": "Owner",
    }


def test_provision_extra_still_quota_gated(tmp_path, monkeypatch):
    import app.main as mainmod
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "q.db"))
    db.init_db()
    from fastapi.testclient import TestClient
    from app.security import create_client_token
    client = TestClient(app=mainmod.app)
    aid = db.create_account("q@steprotech.com", "qmain", "Q",
                            first_name="Q", last_name="Q")
    db.set_account_quota(aid, 1)
    db.create_instance(account_id=aid, stack_name="qmain", environment_id=8,
                       environment_name="test", port=33101,
                       domain="qmain.steprotech.com",
                       basic_auth_user="q@steprotech.com",
                       basic_auth_password="Str0ngPass1", n8n_encryption_key="e" * 32)
    db.update_instance(1, status="healthy")
    ctoken = create_client_token(aid)
    r = client.post(f"/api/v1/accounts/{aid}/provision", headers=_h(ctoken),
                    json={"password": "SecondPass1", "username": "secondbox"})
    assert r.status_code == 409
