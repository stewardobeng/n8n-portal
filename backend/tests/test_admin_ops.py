# Tests for admin-assisted operations (2026-09-02): environment numbering,
# unlinked-stack discovery, attach, and mark-paid date semantics.
# Uses stub Portainer/NPM clients; no network.

import time

import pytest

from app import db
from app.services import admin_ops


class StubPC:
    """Minimal PortainerClient stand-in for environment/attach logic."""

    def __init__(self, endpoints=None, containers_by_env=None):
        self._eps = endpoints or []
        self._containers = containers_by_env or {}

    def list_endpoints(self):
        return self._eps

    def get_endpoint(self, endpoint_id):
        for e in self._eps:
            if e["Id"] == endpoint_id:
                return e
        raise RuntimeError("not found")

    def list_containers(self, endpoint_id, all=True):
        return self._containers.get(endpoint_id, [])

    def _req(self, method, path, **kw):
        raise AssertionError("stub does not support _req (df)")

    def list_stacks(self):
        return []

    def stop_stack(self, stack_id, endpoint_id):
        pass

    def start_stack(self, stack_id, endpoint_id):
        pass

    def stop_container(self, endpoint_id, container_id):
        pass

    def start_container(self, endpoint_id, container_id):
        pass


class StubNPM:
    def __init__(self, hosts=None):
        self._hosts = hosts or []

    def list_proxy_hosts(self):
        return self._hosts


def _n8n_container(name, stack, state="running", port=None, cid=None):
    c = {
        "Id": cid or f"cid-{stack}",
        "Names": [f"/{name}"],
        "Image": "n8nio/n8n:latest",
        "State": state,
        "Labels": {"com.docker.compose.project": stack},
        "Ports": [{"PublicPort": port}] if port else [],
    }
    return c


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    # settings live on the global conn; ensure a clean env
    old = db.get_conn
    db._TEST_PATH = db_path

    def get_conn(db_path=None):
        return old(db_path or db._TEST_PATH)

    db.get_conn = get_conn
    yield
    db.get_conn = old


def _endpoints():
    return [
        {"Id": 3, "Name": "local", "URL": "unix:///var/run/docker.sock", "Status": 1, "Type": 1},
        {"Id": 4, "Name": "n8n-cloud", "URL": "tcp://141.148.139.50:9001", "Status": 1, "Type": 2},
        {"Id": 8, "Name": "n8n-cloud 2", "URL": "tcp://129.146.2.18:9001", "Status": 1, "Type": 2},
        {"Id": 9, "Name": "n8n Premium 1", "URL": "tcp://132.226.124.178:9001", "Status": 1, "Type": 2},
    ]


def test_display_numbering_excludes_control_host():
    pc = StubPC(_endpoints())
    servers = admin_ops._n8n_server_endpoints(pc)
    ids = [s["Id"] for s in servers]
    assert ids == [4, 8, 9]  # control host (unix/local) excluded, sorted
    assert admin_ops._display_number(1) == "n8n Server 1"
    assert admin_ops._display_number(3) == "n8n Server 3"


def test_admin_create_account_auto_password_and_email(monkeypatch):
    sent = {}

    def fake_send(to, email, username, password):
        sent["to"] = to
        sent["password"] = password

    monkeypatch.setattr(admin_ops, "send_admin_welcome_credentials", fake_send)
    result = admin_ops.admin_create_account("John.Doe@Steprotech.com", "John", "Doe")
    assert result["email"] == "john.doe@steprotech.com"
    assert "." not in result["username"]  # dots -> hyphens
    assert result["password_once"]
    assert sent["to"] == result["email"]
    # account exists, pending, no subscription
    acc = db.get_account(result["account_id"])
    assert acc["status"] == "pending"
    assert acc["subscription_status"] == "none"
    assert not acc["paid_until"]
    # password hash stored, not plaintext
    assert acc["password_hash"] and acc["password_hash"] != result["password_once"]


def test_admin_create_account_duplicate_email():
    admin_ops.admin_create_account("dup@steprotech.com", "D", "U")
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.admin_create_account("dup@steprotech.com", "D", "U")


def test_discover_unlinked_stacks():
    containers = {
        8: [
            _n8n_container("beta-n8n-1", "beta", "running", 32786),      # linked
            _n8n_container("iyke-n8n-1", "iyke", "running", 32779),      # unlinked
            _n8n_container("oak-n8n-1", "oak", "exited", 32778),         # unlinked, off
        ],
        9: [],
    }
    pc = StubPC(_endpoints(), containers)
    npm = StubNPM([{"domain_names": ["iyke.steprotech.com"],
                    "forward_host": "129.146.2.18", "forward_port": 32779}])
    # pre-link beta to account 1
    acc_id = db.create_account("beta@steprotech.com", "beta", "", "", "",
                               db.new_key(prefix="h", nbytes=16))
    db.create_instance(acc_id, "beta", 8, "n8n Server 2", 32786,
                       "beta.steprotech.com", "beta@steprotech.com", "x",
                       "k" * 64, stack_id=79, managed=1, status="healthy")

    found = admin_ops.discover_unlinked_stacks(pc, npm)
    names = {f["stack_name"]: f for f in found}
    assert "iyke" in names and "oak" in names
    assert "beta" not in names  # linked stacks excluded
    assert names["iyke"]["domain"] == "iyke.steprotech.com"
    assert names["oak"]["running"] is False
    assert names["oak"]["port"] == 32778


def test_attach_instance_and_mark_paid_future(monkeypatch):
    containers = {
        8: [_n8n_container("destiny-n8n-1", "destiny", "exited", 32774)],
    }
    pc = StubPC(_endpoints(), containers)
    monkeypatch.setattr(admin_ops, "PortainerClient", lambda *a, **k: pc)
    monkeypatch.setattr(admin_ops, "NPMClient", lambda *a, **k: StubNPM())
    # billing import is lazy inside mark_paid helpers; stub lock/unlock
    monkeypatch.setattr(admin_ops, "_stop_for_account", lambda aid: True)
    monkeypatch.setattr(admin_ops, "_start_for_account", lambda aid: True)

    acc_id = db.create_account("destiny@steprotech.com", "destiny", "", "", "",
                               db.new_key(prefix="h", nbytes=16))
    res = admin_ops.attach_instance(acc_id, 8, "destiny", 32774, "destiny.steprotech.com", pc=pc)
    assert res["stack_name"] == "destiny"
    inst = db.get_instance(res["instance_id"])
    assert inst["managed"] == 0
    assert inst["status"] == "healthy"
    assert inst["locked"] == 1  # was exited/stopped -> recorded as locked

    # mark paid with a future expiry -> active; instance started via unlock path
    fut = int(time.time()) + 365 * 86400
    out = admin_ops.mark_paid(acc_id, fut, paid_from=int(time.time()))
    acc = db.get_account(acc_id)
    assert acc["subscription_status"] == "active"
    assert acc["paid_until"] == fut
    assert acc["paid_from"] is not None
    assert out["subscription_status"] == "active"


def test_mark_paid_backdated_past_expiry(monkeypatch):
    containers = {8: [_n8n_container("old-n8n-1", "old", "running", 32770)]}
    pc = StubPC(_endpoints(), containers)
    monkeypatch.setattr(admin_ops, "PortainerClient", lambda *a, **k: pc)
    monkeypatch.setattr(admin_ops, "NPMClient", lambda *a, **k: StubNPM())
    stopped = []
    monkeypatch.setattr(admin_ops, "_stop_for_account",
                        lambda aid: stopped.append(aid) or True)
    monkeypatch.setattr(admin_ops, "_start_for_account", lambda aid: True)

    acc_id = db.create_account("old@steprotech.com", "old", "", "", "",
                               db.new_key(prefix="h", nbytes=16))
    admin_ops.attach_instance(acc_id, 8, "old", 32770, "old.steprotech.com", pc=pc)
    past = int(time.time()) - 30 * 86400  # expired 30 days ago
    out = admin_ops.mark_paid(acc_id, past, paid_from=past - 365 * 86400)
    acc = db.get_account(acc_id)
    assert acc["subscription_status"] == "unpaid"
    assert acc["paid_until"] == past
    assert out["subscription_status"] == "unpaid"
    assert acc_id in stopped


def test_attach_rejects_unknown_stack():
    pc = StubPC(_endpoints(), {8: []})
    acc_id = db.create_account("x@steprotech.com", "x", "", "", "",
                               db.new_key(prefix="h", nbytes=16))
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.attach_instance(acc_id, 8, "ghost", 0, "", pc=pc)


def test_attach_rejects_already_linked():
    containers = {8: [_n8n_container("taken-n8n-1", "taken", "running", 32760)]}
    pc = StubPC(_endpoints(), containers)
    a1 = db.create_account("a1@steprotech.com", "a1", "", "", "",
                           db.new_key(prefix="h", nbytes=16))
    a2 = db.create_account("a2@steprotech.com", "a2", "", "", "",
                           db.new_key(prefix="h", nbytes=16))
    admin_ops.attach_instance(a1, 8, "taken", 32760, "taken.steprotech.com", pc=pc)
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.attach_instance(a2, 8, "taken", 32760, "taken.steprotech.com", pc=pc)
