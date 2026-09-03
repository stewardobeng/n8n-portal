# Tests for admin impersonation ("login as a customer", 2026-09-03).
# The admin can mint a short-lived customer session for troubleshooting. Only an
# admin can start one, only active accounts are eligible, the token carries the
# 'imp' flag (so it is rejected on admin routes and is required to end the
# session), and both the start and the end are recorded on the security trail.
import sys
import time

import jwt
import pytest

from app import db
from app.config import settings


@pytest.fixture
def account(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "imp.db"))
    db.init_db()
    return db.create_account("imp@steprotech.com", "impuser", "Imp User",
                             first_name="Imp", last_name="User")


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "impapi.db"))
    db.init_db()
    return TestClient(app)


def _seed(tmp_path, monkeypatch):
    from app.security import create_access_token, create_client_token
    client = _client(tmp_path, monkeypatch)
    aid = db.create_account("impapi@steprotech.com", "impapi", "Imp API",
                            first_name="Imp", last_name="API")
    atoken = create_access_token("admin")
    ctoken = create_client_token(aid)
    return client, aid, atoken, ctoken


def _h(token):
    return {"Authorization": "Bearer " + token}


def _start(client, aid, atoken):
    return client.post(f"/api/v1/admin/accounts/{aid}/impersonate",
                       headers=_h(atoken))


# ---- token unit behaviour ----

def test_impersonation_token_short_lived_and_flagged():
    from app.security import create_impersonation_token
    tok = create_impersonation_token(7)
    payload = jwt.decode(tok, settings.jwt_secret, algorithms=["HS256"],
                         options={"verify_exp": False})
    assert payload["sub"] == "acc:7"
    assert payload.get("imp") is True
    ttl = payload["exp"] - int(time.time())
    assert 59 * 60 <= ttl <= 61 * 60, f"expected ~60min TTL, got {ttl}s"


# ---- start: admin-only, active accounts only ----

def test_impersonate_requires_admin(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    # a customer token cannot start impersonation (sub is acc:<id>)
    assert client.post(f"/api/v1/admin/accounts/{aid}/impersonate",
                       headers=_h(ctoken)).status_code == 401
    # no token at all
    assert client.post(f"/api/v1/admin/accounts/{aid}/impersonate").status_code == 401


def test_impersonate_unknown_account_404(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    assert client.post("/api/v1/admin/accounts/99999/impersonate",
                       headers=_h(atoken)).status_code == 404


def test_impersonate_rejects_suspended_account(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    db.set_account_state(aid, "suspended")
    r = _start(client, aid, atoken)
    assert r.status_code == 409
    # archived too
    db.set_account_state(aid, "active")
    db.set_account_state(aid, "archived")
    assert _start(client, aid, atoken).status_code == 409


def test_impersonate_returns_customer_session(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    r = _start(client, aid, atoken)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["impersonation"] is True
    assert body["expires_minutes"] == settings.impersonation_ttl_minutes
    assert body["account"]["email"] == "impapi@steprotech.com"
    # the minted token acts as that customer on /me
    mr = client.get("/api/v1/me", headers=_h(body["token"]))
    assert mr.status_code == 200
    assert mr.json()["account"]["username"] == "impapi"


def test_impersonation_token_cannot_reach_admin_routes(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    imp = _start(client, aid, atoken).json()["token"]
    # sub is acc:<id>, so every admin route rejects it exactly like a customer
    assert client.get("/api/v1/admin/accounts", headers=_h(imp)).status_code == 401


def test_impersonate_start_is_audited(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    _start(client, aid, atoken)
    events = db.list_auth_events(20)
    rows = [e for e in events if e["event"] == "impersonate_start"]
    assert rows, "impersonate_start must be recorded on the security trail"
    assert rows[0]["account_id"] == aid
    assert "username=impapi" in rows[0]["detail"]


# ---- end: imp flag required, audited ----

def test_impersonate_end_requires_imp_flag(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    # a normal customer session token cannot end an impersonation
    assert client.post("/api/v1/auth/impersonate-end",
                       headers=_h(ctoken)).status_code == 401
    assert client.post("/api/v1/auth/impersonate-end").status_code == 401


def test_impersonate_end_ok_and_audited(tmp_path, monkeypatch):
    client, aid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    imp = _start(client, aid, atoken).json()["token"]
    e = client.post("/api/v1/auth/impersonate-end", headers=_h(imp))
    assert e.status_code == 200
    assert e.json()["ended"] is True
    names = [x["event"] for x in db.list_auth_events(20)]
    assert "impersonate_start" in names and "impersonate_end" in names
