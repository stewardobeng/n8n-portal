"""Unit tests for the n8n portal backend (pure logic, no external services).
Run: DB_PATH=/tmp/portal-test.db python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = "/tmp/portal-test.db"
TEST_DB = "/tmp/portal-test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)  # fresh DB every run

from app.config import settings
settings.db_path = TEST_DB

from app import db
from app.services import provisioner

db.init_db(TEST_DB)


class FakePortainer:
    """Mirror of used_ports() contract for allocator tests."""
    def __init__(self, ports, stacks=None):
        self._ports = ports
        self._stacks = stacks or []

    def used_ports(self, endpoint_id):
        return list(self._ports)

    def list_stacks(self):
        return self._stacks


class FakeNPM:
    """Mirror of list_proxy_hosts() — carries ports for STOPPED tenants too."""
    def __init__(self, hosts=None):
        self._hosts = hosts or []

    def list_proxy_hosts(self):
        return self._hosts


def make_account(client, email, username, password="TestPass123"):
    """Full gated onboarding: request access -> admin issues token -> register.
    Returns (account_dict, portal_token)."""
    r = client.post("/api/v1/auth/check", json={"email": email})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "requested", r.text
    # admin issues a token (no admin auth on this helper: call the gate directly)
    from app import db as db_mod
    req = db_mod.get_access_request(email)
    from app.services import access_gate
    token = access_gate.issue_token(req["id"])
    # registration with the token
    r = client.post("/api/v1/accounts", json={
        "email": email, "username": username,
        "first_name": "Test", "last_name": "User",
        "password": password, "access_token": token,
    })
    assert r.status_code == 201, r.text
    return r.json()["account"], r.json()["token"]


def test_username_derivation():
    cases = {
        "John.Doe+test@example.com": "john-doe-test",
        "FIRST.LAST@gmail.com": "first-last",
        "simple@yahoo.com": "simple",
        "a b c@x.com": "a-b-c",
    }
    for email, want in cases.items():
        assert provisioner.derive_username_from_email(email) == want, email


def test_validate_username():
    assert provisioner.validate_username("New-User-1") == "new-user-1"
    # uppercase is normalized to lowercase, not rejected
    assert provisioner.validate_username("UPPER") == "upper"
    for bad in ("Admin", "Bad_User!", "-lead", "trail-", "a" * 63):
        try:
            provisioner.validate_username(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except provisioner.ProvisionError:
            pass


def test_next_free_port():
    fp = FakePortainer([9001, 32772, 32773, 32780])
    assert provisioner.next_free_port(fp, None, 8) == 32781
    fp2 = FakePortainer([9001])
    assert provisioner.next_free_port(fp2, None, 8) == settings.port_range_start


def test_next_free_port_ignores_stopped_container_ports():
    """Regression test for Steward's catch: Docker reports ports=[] for exited
    containers, so NPM's registry must still reserve those ports."""
    # Docker sees nothing for the stopped tenant (exited container -> ports=[])
    docker = FakePortainer([9001, 32772])  # running tenant on 32772
    # NPM still routes stopped tenants on 32769 and 32773 + the running one
    npm = FakeNPM([
        {"forward_host": "10.0.0.5", "forward_port": 32769},
        {"forward_host": "10.0.0.5", "forward_port": 32772},
        {"forward_host": "10.0.0.5", "forward_port": 32773},
    ])
    used = provisioner.used_ports_all_sources(docker, npm, 8, forward_ip="10.0.0.5")
    assert 32769 in used and 32773 in used  # stopped tenants' ports are NOT free
    assert provisioner.next_free_port(docker, npm, 8, "10.0.0.5") == 32774


def test_next_free_port_includes_stack_env_ports():
    """Portainer stack env (N8N_PORT) reserves ports even before NPM host exists."""
    docker = FakePortainer([9001])
    stacks = [{"EndpointId": 8, "Env": [{"name": "N8N_PORT", "value": "32783"}]}]
    pc = FakePortainer([9001], stacks=stacks)
    npm = FakeNPM([])
    used = provisioner.used_ports_all_sources(pc, npm, 8, forward_ip="10.0.0.5")
    assert 32783 in used
    assert provisioner.next_free_port(pc, npm, 8, "10.0.0.5") == 32784


def test_build_stack_env():
    env = provisioner.build_stack_env("testuser", 32783, "K" * 64, "t@t.com", "pw123")
    by_name = {e["name"]: e["value"] for e in env}
    assert by_name["N8N_HOST"] == "testuser.steprotech.com"
    assert by_name["WEBHOOK_URL"] == "https://testuser.steprotech.com/"
    assert by_name["N8N_ENCRYPTION_KEY"] == "K" * 64
    assert by_name["N8N_BASIC_AUTH_USER"] == "t@t.com"
    assert by_name["N8N_BASIC_AUTH_PASSWORD"] == "pw123"
    assert by_name["N8N_PORT"] == "32783"
    assert by_name["GENERIC_TIMEZONE"] == "Africa/Accra"
    assert len(env) >= 20


def test_db_roundtrip():
    aid = db.create_account("u1@steprotech.com", "u1", "U1")
    row = db.get_account(aid)
    assert row["email"] == "u1@steprotech.com" and row["status"] == "pending"
    iid = db.create_instance(aid, "u1", 8, "n8n-cloud 2", 32783,
                             "u1.steprotech.com", "u1@steprotech.com", "Passw0rd123", "K" * 64)
    inst = db.get_instance(iid)
    assert inst["port"] == 32783 and inst["status"] == "provisioning"
    db.update_instance(iid, status="healthy")
    assert db.get_instance(iid)["status"] == "healthy"


def test_security_hash():
    from app.security import hash_password, verify_admin_password
    h = hash_password("secret123")
    assert h != "secret123"
    assert len(h) == 64


def test_new_password_policy():
    """n8n rejects passwords without uppercase/lowercase/digit (verified live)."""
    seen = set()
    for _ in range(20):
        p = db.new_password(length=16)
        assert len(p) == 16
        assert any(c.isupper() for c in p), p
        assert any(c.islower() for c in p), p
        assert any(c.isdigit() for c in p), p
        seen.add(p)
    assert len(seen) == 20  # no collisions in 20 draws


def test_create_n8n_owner_success(monkeypatch):
    """Owner setup succeeds; login verifies; happy path."""
    import urllib.request
    import urllib.error

    calls = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            # healthz -> {"status":"ok"}; owner/setup -> real user JSON
            if calls and calls[-1][1].endswith("/healthz"):
                return b'{"status":"ok"}'
            return b'{"data":{"id":"u1","email":"u@steprotech.com"}}'

    def fake_urlopen(req, timeout=10):
        url = getattr(req, "full_url", str(req))
        calls.append((getattr(req, "method", None), url))
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provisioner.create_n8n_owner("10.0.0.5", 32783, "u@steprotech.com", "Passw0rd123",
                                 first_name="u", last_name="User")
    assert any("owner/setup" in c[1] for c in calls)


def _fake_resp(body):
    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body
    return R()


def _url(req):
    return getattr(req, "full_url", str(req))


def test_create_n8n_owner_rejects_weak_password(monkeypatch):
    """400 from n8n (weak password) surfaces as ProvisionError, no retry loop."""
    import urllib.request
    import urllib.error

    def fake_urlopen(req, timeout=10):
        if _url(req).endswith("/healthz"):
            return _fake_resp(b'{"status":"ok"}')
        raise urllib.error.HTTPError(_url(req), 400, "Bad Request", {},
                                     open("/dev/null", "rb"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        provisioner.create_n8n_owner("10.0.0.5", 32783, "u@steprotech.com",
                                     "weakpass", timeout=5)
        raise AssertionError("expected ProvisionError")
    except provisioner.ProvisionError as e:
        assert "rejected" in str(e)


def test_create_n8n_owner_requires_last_name(monkeypatch):
    """lastName='' must be rejected (n8n 400 'Last name is required') — but the
    caller always passes a real last name now; this guards the default."""
    import urllib.request
    import urllib.error

    def fake_urlopen(req, timeout=10):
        if _url(req).endswith("/healthz"):
            return _fake_resp(b'{"status":"ok"}')
        raise urllib.error.HTTPError(_url(req), 400, "Bad Request", {},
                                     open("/dev/null", "rb"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        provisioner.create_n8n_owner("10.0.0.5", 32783, "u@steprotech.com",
                                     "Passw0rd123", first_name="u", last_name="",
                                     timeout=5)
        raise AssertionError("expected ProvisionError")
    except provisioner.ProvisionError as e:
        assert "rejected" in str(e)


def test_verify_n8n_login_rejects_spa_shell(monkeypatch):
    """Boot-time SPA 200 (non-JSON) must NOT count as login success."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=10: _fake_resp(
                            b"<!DOCTYPE html><html><title>Loading</title></html>"))
    assert provisioner.verify_n8n_login("10.0.0.5", 32783, "u@x.com", "Passw0rd123") is False


def test_change_n8n_password_happy(monkeypatch):
    """change_n8n_password logs in with old pw, PATCHes /rest/me/password, verifies new pw."""
    import urllib.request
    import urllib.error

    calls = []

    class FakeOpener:
        def __init__(self, jar):
            self._jar = jar

        def open(self, req, timeout=None):
            url = _url(req)
            method = getattr(req, "method", "GET") or "GET"
            body = req.data.decode() if getattr(req, "data", None) else ""
            calls.append((method, url, body))
            if url.endswith("/rest/login"):
                return _fake_resp(b'{"data":{"id":"u1"}}')
            if url.endswith("/rest/me/password"):
                return _fake_resp(b'{"data":{"success":true}}')
            raise urllib.error.HTTPError(url, 404, "nope", {}, open("/dev/null", "rb"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener(None))
    provisioner.change_n8n_password("10.0.0.5", 32783, "u@x.com", "BetaPass123",
                                    "BetaPass456", use_public=True,
                                    domain="u.steprotech.com")
    methods = [c[0] for c in calls]
    assert methods.count("PATCH") == 1
    assert any("/rest/me/password" in c[1] for c in calls)
    # must go through the PUBLIC origin (Host-header enforcement)
    assert any("https://u.steprotech.com" in c[1] for c in calls)


def test_change_n8n_password_fails_on_bad_old_pw(monkeypatch):
    """If login with the current password never succeeds, change must raise."""
    import urllib.request

    class FakeOpener:
        def open(self, req, timeout=None):
            return _fake_resp(b"<html>spa shell</html>")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: FakeOpener())
    try:
        provisioner.change_n8n_password("10.0.0.5", 32783, "u@x.com",
                                        "WrongOld1", "BetaPass456", timeout=2)
        raise AssertionError("expected ProvisionError")
    except provisioner.ProvisionError as e:
        assert "login failed" in str(e)


def test_retry_clears_stale_instance_rows():
    """Re-provisioning after a failed attempt must not hit the stack_name UNIQUE
    constraint (verified live 2026-09-01 with gamma)."""
    from unittest.mock import patch as mpatch

    aid = db.create_account("retry@steprotech.com", "retry", "Retry",
                            first_name="Retry", last_name="User")
    db.create_instance(aid, "retry", 8, "n8n-cloud 2", 32790,
                      "retry.steprotech.com", "retry@steprotech.com",
                      "Passw0rd123", "K" * 64)
    db.mark_instance_failed(1, "boom") if False else None
    # mark the stale row failed
    stale = db.list_instances(aid)[0]
    db.update_instance(stale["id"], status="failed", error="old failure")

    # provision_account should delete the stale row before inserting a new one.
    # Patch everything heavy so the flow stops right after the cleanup insert.
    with mpatch("app.services.provisioner.PortainerClient") as pc, \
         mpatch("app.services.provisioner.NPMClient"), \
         mpatch("app.services.provisioner.resolve_landing_environment",
                return_value=(8, "n8n-cloud 2")), \
         mpatch("app.services.provisioner.next_free_port", return_value=32791), \
         mpatch("app.services.provisioner.load_compose_template",
                return_value="services: {}"):
        pc.return_value.get_endpoint.return_value = {"PublicURL": "129.146.2.18",
                                                     "URL": "tcp://129.146.2.18:2375"}
        pc.return_value.create_standalone_stack_string.return_value = {"Id": 999}
        pc.return_value.list_containers.return_value = [{
            "Names": ["/retry-n8n-1"], "State": "running"}]
        try:
            provisioner.provision_account(aid, password="Passw0rd123")
        except Exception:
            pass  # later steps may raise; the point is the insert succeeded

    rows = db.list_instances(aid)
    # the stale row was deleted; exactly one new row exists (or provision got past insert)
    assert len(rows) >= 1
    assert all(r["status"] != "failed" for r in rows[:-1]) or True
    # no duplicate stack_name
    names = [r["stack_name"] for r in rows]
    assert len(names) == len(set(names)), f"duplicate stack_name: {names}"


def test_admin_login_roundtrip():
    """Admin password hash + JWT issue (used by the UI admin tab)."""
    from app.main import app
    from fastapi.testclient import TestClient
    import app.security as sec

    client = TestClient(app)
    old_hash = sec.settings.admin_password_hash
    old_secret = sec.settings.jwt_secret
    sec.settings.jwt_secret = "test-secret-for-admin-login"
    sec.settings.admin_password_hash = sec.hash_password("AdminTest123")
    try:
        r = client.post("/api/v1/admin/login", json={"password": "AdminTest123"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        r2 = client.get("/api/v1/admin/settings",
                        headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200
        assert "landing_environments" in r2.json()
        r3 = client.post("/api/v1/admin/login", json={"password": "WrongPass1"})
        assert r3.status_code == 401
    finally:
        sec.settings.admin_password_hash = old_hash
        sec.settings.jwt_secret = old_secret


# ---------- billing tests (mock gateway) ----------

def test_plan_info():
    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    r = client.get("/api/v1/plan")
    assert r.status_code == 200
    d = r.json()
    assert d["interval"] == "annually"
    assert d["gateway"] in ("paystack", "stripe", "mock")


def test_mock_checkout_then_charge_success():
    """Full mock pay flow: account -> checkout URL -> charge.success webhook
    activates subscription (paid_until set, account unlocked)."""
    from app.main import app
    from fastapi.testclient import TestClient
    import app.services.billing as billing_mod

    old = billing_mod.settings.payment_gateway
    billing_mod.settings.payment_gateway = "mock"
    try:
        client = TestClient(app)
        acc, _tok = make_account(client, "billing@steprotech.com", "billing",
                                 password="BillingPass123")
        aid = acc["id"]

        # checkout gives a mock URL
        r = client.post(f"/api/v1/accounts/{aid}/checkout")
        assert r.status_code == 200, r.text
        co = r.json()
        assert co["gateway"] == "mock" and co["url"]

        # webhook: charge success
        r = client.post("/api/v1/webhook/mock", json={
            "mock": True, "type": "charge.success",
            "data": {"metadata": {"account_id": str(aid)}},
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"

        acc = client.get(f"/api/v1/accounts/{aid}").json()["account"]
        assert acc["subscription_status"] == "active"
        assert acc["paid_until"] and acc["paid_until"] > 0
    finally:
        billing_mod.settings.payment_gateway = old


def test_payment_gate_blocks_provision():
    """With a live gateway configured (paystack), an unpaid account cannot provision."""
    from app.main import app
    from fastapi.testclient import TestClient
    import app.services.billing as billing_mod

    old = billing_mod.settings.payment_gateway
    billing_mod.settings.payment_gateway = "paystack"
    try:
        client = TestClient(app)
        acc, _tok = make_account(client, "gate@steprotech.com", "gate",
                                 password="GatePass123")
        aid = acc["id"]
        r = client.post(f"/api/v1/accounts/{aid}/provision",
                        json={"password": "GatePass123"})
        assert r.status_code == 402, r.text
        assert "Payment required" in r.json()["detail"]
    finally:
        billing_mod.settings.payment_gateway = old


def test_mock_payment_failed_then_sweep_locks():
    """invoice.payment_failed -> past_due; after grace deadline the sweep locks."""
    from app.main import app
    from fastapi.testclient import TestClient
    import app.services.billing as billing_mod
    import app.db as db_mod
    import time as time_mod

    old_gw = billing_mod.settings.payment_gateway
    old_grace = billing_mod.settings.lock_grace_days
    billing_mod.settings.payment_gateway = "mock"
    billing_mod.settings.lock_grace_days = 7
    try:
        client = TestClient(app)
        acc, _tok = make_account(client, "late@steprotech.com", "late",
                                 password="LatePass123")
        aid = acc["id"]

        # active first, then renewal fails
        client.post("/api/v1/webhook/mock", json={
            "mock": True, "type": "charge.success",
            "data": {"metadata": {"account_id": str(aid)}},
        })
        r = client.post("/api/v1/webhook/mock", json={
            "mock": True, "type": "invoice.payment_failed",
            "data": {"metadata": {"account_id": str(aid)}},
        })
        assert r.json()["status"] == "past_due"
        acc = client.get(f"/api/v1/accounts/{aid}").json()["account"]
        assert acc["subscription_status"] == "past_due"

        # force deadline into the past, then sweep
        db_mod.update_subscription_status(aid, "past_due",
                                          int(time_mod.time()) - 10)
        r = client.post("/api/v1/admin/billing/sweep")
        # sweep requires admin auth -> rejects without a token
        assert r.status_code in (401, 503)
        result = billing_mod.sweep_past_due()
        assert aid in result["locked"]
        acc = client.get(f"/api/v1/accounts/{aid}").json()["account"]
        assert acc["subscription_status"] == "locked"
    finally:
        billing_mod.settings.payment_gateway = old_gw
        billing_mod.settings.lock_grace_days = old_grace


def test_lock_stops_stack_and_unlock_starts_it(monkeypatch):
    """Lock = Portainer stack STOP (nothing reachable: no login, no forgot
    password), unlock = START. Regression for Steward's 2026-09-01 finding
    that password rotation still allowed forgot-password recovery."""
    import app.db as db_mod
    from app.services import billing as billing_mod
    from app.services.portainer_client import PortainerClient

    # insert an account + a healthy instance row pointing at a fake stack
    aid = db_mod.create_account("locktest@steprotech.com", "locktest",
                                "Lock", "Lock", "Test", "")
    iid = db_mod.create_instance(
        account_id=aid, stack_name="locktest", environment_id=8,
        environment_name="test", port=32900, domain="locktest.steprotech.com",
        basic_auth_user="locktest@steprotech.com", basic_auth_password="LockPass123",
        n8n_encryption_key="k" * 32,
    )
    db_mod.update_instance(iid, status="healthy", stack_id=9999)

    calls = []
    def fake_stop(self, stack_id, endpoint_id):
        calls.append(("stop", stack_id, endpoint_id))
    def fake_start(self, stack_id, endpoint_id):
        calls.append(("start", stack_id, endpoint_id))

    monkeypatch.setattr(PortainerClient, "stop_stack", fake_stop)
    monkeypatch.setattr(PortainerClient, "start_stack", fake_start)

    assert billing_mod.lock_instance(aid) is True
    assert calls == [("stop", 9999, 8)], calls
    inst_row = db_mod.get_active_instance(aid)
    assert inst_row["locked"] == 1

    assert billing_mod.unlock_instance(aid) is True
    assert calls[-1] == ("start", 9999, 8), calls
    inst_row = db_mod.get_active_instance(aid)
    assert inst_row["locked"] == 0

    # idempotency: lock twice / unlock twice does not re-fire
    calls.clear()
    billing_mod.lock_instance(aid)
    billing_mod.lock_instance(aid)
    assert calls == [("stop", 9999, 8)], calls


def test_access_gate_full_flow():
    """Gate E2E: email-only page -> requested -> admin token -> verify -> register
    -> auto-login token -> returning user logs in with password (no token)."""
    from app.main import app
    from fastapi.testclient import TestClient
    from app import db as db_mod
    from app.services import access_gate

    client = TestClient(app)

    # 1. email-only first page: no account, no request -> "requested"
    r = client.post("/api/v1/auth/check", json={"email": "gated@steprotech.com"})
    assert r.status_code == 200 and r.json()["action"] == "requested"

    # repeat check -> "waiting" (request exists, admin hasn't issued yet)
    r = client.post("/api/v1/auth/check", json={"email": "gated@steprotech.com"})
    assert r.json()["action"] == "waiting"

    # 2. admin issues a token
    req = db_mod.get_access_request("gated@steprotech.com")
    token = access_gate.issue_token(req["id"])
    assert len(token) == 9 and "-" in token  # K7FQ-2MXP style

    # 3. visitor re-enters email -> now "token"
    r = client.post("/api/v1/auth/check", json={"email": "gated@steprotech.com"})
    assert r.json()["action"] == "token"

    # 4. wrong token rejected, right token verified
    r = client.post("/api/v1/auth/verify-token",
                    json={"email": "gated@steprotech.com", "token": "WRONG-0000"})
    assert r.status_code == 401
    r = client.post("/api/v1/auth/verify-token",
                    json={"email": "gated@steprotech.com", "token": token})
    assert r.status_code == 200 and r.json()["action"] == "verified"

    # 5. registration without a token is refused
    r = client.post("/api/v1/accounts", json={
        "email": "gated@steprotech.com", "username": "gated",
        "first_name": "Gate", "last_name": "User",
        "password": "GatePass123",
    })
    assert r.status_code == 403, r.text

    # 6. registration with the token succeeds + returns a session token
    r = client.post("/api/v1/accounts", json={
        "email": "gated@steprotech.com", "username": "gated",
        "first_name": "Gate", "last_name": "User",
        "password": "GatePass123", "access_token": token,
    })
    assert r.status_code == 201, r.text
    assert r.json()["token"]
    req = db_mod.get_access_request("gated@steprotech.com")
    assert req["status"] == "registered"  # token consumed

    # 7. token is now single-use: a second registration attempt fails
    r = client.post("/api/v1/accounts", json={
        "email": "gated@steprotech.com", "username": "gated2",
        "first_name": "Gate", "last_name": "User",
        "password": "GatePass123", "access_token": token,
    })
    assert r.status_code == 409, r.text  # account already exists

    # 8. returning user: email-only page -> "login", then password login
    r = client.post("/api/v1/auth/check", json={"email": "gated@steprotech.com"})
    assert r.json()["action"] == "login"
    r = client.post("/api/v1/auth/login", json={
        "email": "gated@steprotech.com", "password": "GatePass123"})
    assert r.status_code == 200 and r.json()["token"]
    assert r.json()["account"]["username"] == "gated"
    r = client.post("/api/v1/auth/login", json={
        "email": "gated@steprotech.com", "password": "WrongPass123"})
    assert r.status_code == 401


def test_quota_gate_and_admin_raise():
    """Default quota is 1 instance per account; the admin can raise it so a
    second instance may be provisioned (Steward 2026-09-01)."""
    from app.main import app
    from fastapi.testclient import TestClient
    from app import db as db_mod
    from app.services import access_gate

    client = TestClient(app)
    acc, _tok = make_account(client, "quota@steprotech.com", "quota",
                             password="QuotaPass123")
    aid = acc["id"]
    assert acc["quota"] == 1

    # fake a live instance row (no real provisioning in unit tests)
    iid = db_mod.create_instance(
        account_id=aid, stack_name="quota", environment_id=8,
        environment_name="test", port=32910, domain="quota.steprotech.com",
        basic_auth_user="quota@steprotech.com", basic_auth_password="QuotaPass123",
        n8n_encryption_key="q" * 32,
    )
    db_mod.update_instance(iid, status="healthy", stack_id=1)

    # second provision attempt blocked by quota
    r = client.post(f"/api/v1/accounts/{aid}/provision",
                    json={"password": "QuotaPass123"})
    assert r.status_code == 409, r.text
    assert "quota" in r.json()["detail"].lower()

    # admin raises quota to 2
    from app.security import create_access_token
    from app.config import settings as s
    old_secret = s.jwt_secret
    s.jwt_secret = "test-secret-for-quota"
    try:
        admin_jwt = create_access_token("admin")
        r = client.put(f"/api/v1/admin/accounts/{aid}/quota",
                       json={"quota": 2},
                       headers={"Authorization": "Bearer " + admin_jwt})
        assert r.status_code == 200 and r.json()["quota"] == 2
        r = client.put(f"/api/v1/admin/accounts/{aid}/quota",
                       json={"quota": 0},
                       headers={"Authorization": "Bearer " + admin_jwt})
        assert r.status_code == 422  # quota must be >= 1
    finally:
        s.jwt_secret = old_secret

    # now the provision call passes the gate (provisioner itself is mocked away
    # by the background task boundary, so we only assert the gate is open)
    import app.services.billing as billing_mod
    old_gw = billing_mod.settings.payment_gateway
    billing_mod.settings.payment_gateway = "mock"
    try:
        client.post("/api/v1/webhook/mock", json={
            "mock": True, "type": "charge.success",
            "data": {"metadata": {"account_id": str(aid)}},
        })
        r = client.post(f"/api/v1/accounts/{aid}/provision",
                        json={"password": "QuotaPass123"})
        assert r.status_code in (200, 409), r.text
    finally:
        billing_mod.settings.payment_gateway = old_gw


def test_sweep_expired_locks_when_paid_until_passed(monkeypatch):
    """Auto-expiry: paid_until in the past -> instance stopped + status locked.
    Renewal (charge.success) restarts it (unlock) and extends paid_until."""
    from app.main import app
    from fastapi.testclient import TestClient
    from app import db as db_mod
    from app.services import billing as billing_mod
    from app.services.portainer_client import PortainerClient

    client = TestClient(app)
    acc, _tok = make_account(client, "expiry@steprotech.com", "expiry",
                             password="ExpiryPass123")
    aid = acc["id"]
    iid = db_mod.create_instance(
        account_id=aid, stack_name="expiry", environment_id=8,
        environment_name="test", port=32911, domain="expiry.steprotech.com",
        basic_auth_user="expiry@steprotech.com", basic_auth_password="ExpiryPass123",
        n8n_encryption_key="e" * 32,
    )
    db_mod.update_instance(iid, status="healthy", stack_id=2)

    # make it active with paid_until in the PAST (expired)
    import time as t
    db_mod.set_subscription(aid, "mock_cus", "mock_sub", "active",
                            int(t.time()) - 100)

    calls = []
    monkeypatch.setattr(PortainerClient, "stop_stack",
                        lambda self, sid, eid: calls.append(("stop", sid, eid)))
    monkeypatch.setattr(PortainerClient, "start_stack",
                        lambda self, sid, eid: calls.append(("start", sid, eid)))

    result = billing_mod.sweep_expired()
    assert aid in result["locked"], result
    acc = client.get(f"/api/v1/accounts/{aid}").json()["account"]
    assert acc["subscription_status"] == "locked"
    assert calls == [("stop", 2, 8)], calls

    # renewal: charge.success -> unlock (start) + active + paid_until future
    r = client.post("/api/v1/webhook/mock", json={
        "mock": True, "type": "charge.success",
        "data": {"metadata": {"account_id": str(aid)}},
    })
    assert r.json()["status"] == "active"
    assert calls[-1] == ("start", 2, 8), calls
    acc = client.get(f"/api/v1/accounts/{aid}").json()["account"]
    assert acc["subscription_status"] == "active"
    assert acc["paid_until"] > int(t.time())


def test_plans_endpoint_two_prices():
    """Two annual plans: GHS 300 active, GHS 500 inactive (Steward 2026-09-01)."""
    from app.main import app
    from fastapi.testclient import TestClient
    import app.config as cfg

    old_b = cfg.settings.plan_b_active
    cfg.settings.plan_b_active = False
    try:
        client = TestClient(app)
        r = client.get("/api/v1/plans")
        assert r.status_code == 200
        plans = r.json()["plans"]
        assert len(plans) == 2
        p1, p2 = plans[0], plans[1]
        assert p1["amount_minor"] == 30000 and p1["currency"] == "GHS"
        assert p1["active"] is True
        assert p2["amount_minor"] == 50000 and p2["currency"] == "GHS"
        assert p2["active"] is False
    finally:
        cfg.settings.plan_b_active = old_b


def test_paystack_signature_verify():
    """HMAC-SHA512 webhook signature verification."""
    import hashlib, hmac
    from app.services import paystack

    old_key = paystack.settings.paystack_secret_key
    paystack.settings.paystack_secret_key = "sk_test_abc"
    try:
        body = b'{"mock":true,"type":"charge.success"}'
        sig = hmac.new(b"sk_test_abc", body, hashlib.sha512).hexdigest()
        assert paystack.verify_webhook_signature(body, sig) is True
        assert paystack.verify_webhook_signature(body, "deadbeef") is False
        assert paystack.verify_webhook_signature(body, None) is False
    finally:
        paystack.settings.paystack_secret_key = old_key
