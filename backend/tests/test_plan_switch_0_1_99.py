# 0.1.99 (Steward, 2026-11-12): the package can be changed, and a workspace can be re-specced.
#
# Steward's words: "I want the change plan and make it in such a way that we can change it from
# the 300 to 500 and we can also change it from the 500 to the 300. Then you also add the upgrade
# workspace... if you add the upgrade workspace ... that same upgrade workspace to plus should
# also say downgrade workspace." And: "the customer can also request... the admin will contact
# the user" (payment for upgrades comes later).
#
# So this file pins four things:
#   * the account's package moves BOTH ways, and only an admin may move it,
#   * a workspace's resources move BOTH ways, by rewriting only the three resource lines in its
#     live stack file (so nothing else in the compose is lost),
#   * the rewrite fails closed when a line is missing or ambiguous,
#   * a customer's request is recorded and the admin is told, without charging anything.

import os

import pytest

import app.services.plan_switch as plan_switch
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.security import create_access_token, create_client_token
from app.services import plan_switch as ps


@pytest.fixture(autouse=True)
def _repo_templates(monkeypatch):
    """Point both template settings at the repo copies.

    In the container they live at /app/templates; a local run has no /app, and every test in this
    module reads them (the switch reads the template to know what a package IS).
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setattr(settings, "compose_template_path",
                        os.path.join(repo, "templates", "n8n-stack-compose.yml"))
    monkeypatch.setattr(settings, "plan_b_compose_template_path",
                        os.path.join(repo, "templates", "n8n-stack-compose-plus.yml"))


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "plan_switch.db"))
    db.init_db()


def _admin():
    return {"Authorization": "Bearer " + create_access_token("admin")}


def _seed_account(plan="a", email="cust@steprotech.com", username="cust", paid=True):
    aid = db.create_account(email=email, username=username, display_name="Cust Omer",
                            password_hash="x", first_name="Cust", last_name="Omer")
    db.set_account_plan(aid, plan)
    if paid:
        db.set_subscription(aid, status="active", paid_until=db.now() + 86400 * 300)
    return aid


def _seed_instance(account_id, plan="a", stack_name="cust", domain="cust.steprotech.com"):
    iid = db.create_instance(account_id, stack_name=stack_name, stack_id=4242,
                             environment_id=4, environment_name="n8n-cloud", port=32768,
                             domain=domain, basic_auth_user="u", basic_auth_password="p",
                             n8n_encryption_key="k")
    db.update_instance(iid, plan=plan, managed=1, status="healthy")
    return iid


# --------------------------------------------------------------------- the three-line rewrite
def test_plan_a_to_b_changes_exactly_the_three_resource_lines():
    with open(settings.compose_template_path) as fh:
        content = fh.read()
    new, changes = ps.rewrite_resources(content, "b")
    assert [c["resource"] for c in changes] == ["cpus", "mem_limit", "heap_mb"]
    assert {c["change"] for c in changes} == {"changed"}
    assert 'cpus: "0.50"' in new and 'mem_limit: "1g"' in new
    assert "--max-old-space-size=768" in new
    # nothing else moved: same number of lines, same volume/identity lines
    assert len(new.splitlines()) == len(content.splitlines())
    assert "n8n_data:/home/node/.n8n" in new
    assert "EXECUTIONS_DATA_MAX_AGE=168" in new


def test_plan_b_to_a_is_the_same_operation_in_reverse():
    with open(settings.plan_b_compose_template_path) as fh:
        content = fh.read()
    new, changes = ps.rewrite_resources(content, "a")
    assert {c["change"] for c in changes} == {"changed"}
    assert 'cpus: "0.30"' in new and 'mem_limit: "768m"' in new
    assert "--max-old-space-size=384" in new


def test_a_workspace_already_on_the_target_reports_no_change():
    with open(settings.compose_template_path) as fh:
        content = fh.read()
    _, changes = ps.rewrite_resources(content, "a")
    assert {c["change"] for c in changes} == {"unchanged"}


def test_the_rewrite_fails_closed_when_a_line_is_missing():
    broken = 'services:\n  n8n:\n    cpus: "0.30"\n'  # no mem_limit, no NODE_OPTIONS
    try:
        ps.rewrite_resources(broken, "b")
    except ValueError as exc:
        assert "memory limit" in str(exc) or "node heap" in str(exc)
    else:
        raise AssertionError("a stack file missing a resource line must not be rewritten")


def test_the_rewrite_refuses_an_ambiguous_file():
    # 0.1.102 scoped the rewrite to the n8n SERVICE, so the duplicate lines must live inside a real
    # service block for this to test ambiguity rather than a missing service.
    dup = ('services:\n  n8n:\n    image: n8nio/n8n:3.2.0\n'
           '    cpus: "0.30"\n    cpus: "0.40"\n    mem_limit: "768m"\n'
           '      - NODE_OPTIONS=--max-old-space-size=384\n')
    try:
        ps.rewrite_resources(dup, "b")
    except ValueError as exc:
        assert "expected exactly 1" in str(exc)
    else:
        raise AssertionError("two cpus lines must abort the switch, not pick one")


def test_template_resources_come_from_the_real_templates():
    assert ps.template_resources("a") == {"cpus": "0.30", "mem_limit": "768m", "heap_mb": "384"}
    assert ps.template_resources("b") == {"cpus": "0.50", "mem_limit": "1g", "heap_mb": "768"}


# --------------------------------------------------------------------- admin changes the plan
def test_admin_moves_an_account_from_a_to_b_and_back(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    client = TestClient(app)

    up = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "b"}, headers=_admin())
    assert up.status_code == 200, up.text
    assert up.json()["changed"] is True and up.json()["plan"] == "b"
    assert (db._row_get(db.get_account(aid), "plan") or "") == "b"

    down = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "a"}, headers=_admin())
    assert down.status_code == 200, down.text
    assert down.json()["plan"] == "a"
    assert (db._row_get(db.get_account(aid), "plan") or "") == "a"


def test_setting_the_same_plan_is_a_no_op(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    client = TestClient(app)
    r = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "a"}, headers=_admin())
    assert r.status_code == 200 and r.json()["changed"] is False


def test_an_unknown_plan_is_refused(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account()
    client = TestClient(app)
    r = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "z"}, headers=_admin())
    assert r.status_code == 422


def test_a_customer_cannot_change_their_own_plan(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account()
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_client_token(aid)}
    r = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "b"}, headers=customer)
    assert r.status_code in (401, 403), r.text
    assert (db._row_get(db.get_account(aid), "plan") or "") == "a"


def test_the_plan_change_names_workspaces_still_on_the_old_package(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    _seed_instance(aid, plan="a")
    client = TestClient(app)
    r = client.put(f"/api/v1/admin/accounts/{aid}/plan", json={"plan": "b"}, headers=_admin())
    assert r.json()["workspaces_on_other_plan"] == ["cust"]
    assert "keep the resources" in r.json()["note"]


# --------------------------------------------------------------------- admin re-specs a workspace
class _FakePortainer:
    """Captures what would be sent to Portainer; never touches a real stack."""

    instances = []
    # Which package's template the fake stack file starts from: a workspace on Plus must start
    # from the Plus file, or a "downgrade" would look like a no-op.
    start_plan = "a"

    def __init__(self, *a, **kw):
        path = (settings.plan_b_compose_template_path if _FakePortainer.start_plan == "b"
                else settings.compose_template_path)
        with open(path) as fh:
            self._content = fh.read().replace("${N8N_PORT}", "32768")
        self.written = None
        _FakePortainer.instances.append(self)

    def list_stacks(self):
        return []

    def get_stack(self, stack_id):
        return {"Id": stack_id, "Env": [{"name": "N8N_PORT", "value": "32768"}]}

    def get_stack_file(self, stack_id):
        return self._content

    def update_stack_file(self, stack_id, endpoint_id, content, env, prune=False, timeout=600.0):
        self.written = {"content": content, "env": env, "stack_id": stack_id, "endpoint_id": endpoint_id}
        self._content = content


def test_switching_a_workspace_up_rewrites_and_redeploys(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    iid = _seed_instance(aid, plan="a")
    monkeypatch.setattr(plan_switch, "PortainerClient", _FakePortainer)
    client = TestClient(app)

    r = client.post(f"/api/v1/admin/instances/{iid}/plan",
                    json={"plan": "b", "backup_first": False}, headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "upgrade" and body["to_plan"] == "b" and body["changed"] is True
    fake = _FakePortainer.instances[-1]
    assert fake.written is not None, "the stack must actually be redeployed"
    assert 'cpus: "0.50"' in fake.written["content"]
    assert fake.written["env"] == [{"name": "N8N_PORT", "value": "32768"}], "stack env must be preserved"
    inst = db.get_instance(iid)
    assert (db._row_get(inst, "plan") or "") == "b"


def test_switching_a_workspace_down_is_the_same_call(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="b")
    iid = _seed_instance(aid, plan="b")
    monkeypatch.setattr(_FakePortainer, "start_plan", "b")
    monkeypatch.setattr(plan_switch, "PortainerClient", _FakePortainer)
    client = TestClient(app)
    r = client.post(f"/api/v1/admin/instances/{iid}/plan",
                    json={"plan": "a", "backup_first": False}, headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "downgrade"
    assert 'cpus: "0.30"' in _FakePortainer.instances[-1].written["content"]
    assert (db._row_get(db.get_instance(iid), "plan") or "") == "a"


def test_preview_shows_the_change_without_writing(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    iid = _seed_instance(aid, plan="a")
    monkeypatch.setattr(plan_switch, "PortainerClient", _FakePortainer)
    client = TestClient(app)
    r = client.get(f"/api/v1/admin/instances/{iid}/plan-preview?plan=b", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "upgrade"
    assert r.json()["to_label"].startswith("Plus")
    assert _FakePortainer.instances[-1].written is None, "a preview must never deploy"
    assert (db._row_get(db.get_instance(iid), "plan") or "") == "a"


def test_a_workspace_with_no_stack_record_is_refused(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    iid = _seed_instance(aid, plan="a")
    db.update_instance(iid, stack_id=None)
    monkeypatch.setattr(plan_switch, "PortainerClient", _FakePortainer)
    client = TestClient(app)
    r = client.post(f"/api/v1/admin/instances/{iid}/plan",
                    json={"plan": "b", "backup_first": False}, headers=_admin())
    assert r.status_code == 409
    assert "Portainer stack" in r.json()["detail"]


# --------------------------------------------------------------------- customer asks to upgrade
def test_a_customer_request_is_recorded_and_the_admin_is_told(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    _seed_instance(aid, plan="a")
    sent = {}
    monkeypatch.setattr(settings, "admin_email", "admin@steprotech.com")
    monkeypatch.setattr("app.main.send_email",
                        lambda to, subject, html, text=None: sent.update(
                            to=to, subject=subject, html=html))
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_client_token(aid)}
    r = client.post(f"/api/v1/accounts/{aid}/upgrade-request",
                    json={"note": "need more room"}, headers=customer)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "requested" and r.json()["admin_notified"] is True
    assert sent["to"] == "admin@steprotech.com"
    assert "Upgrade request" in sent["subject"]
    assert "need more room" in sent["html"]
    # the request is visible to the admin
    listing = client.get("/api/v1/admin/upgrade-requests", headers=_admin()).json()
    assert listing["open_count"] == 1
    assert listing["data"][0]["account_id"] == aid
    assert listing["data"][0]["to_label"].startswith("Plus")


def test_a_second_request_says_already_requested_instead_of_duplicating(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    monkeypatch.setattr(settings, "admin_email", "")
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_client_token(aid)}
    first = client.post(f"/api/v1/accounts/{aid}/upgrade-request", json={}, headers=customer)
    second = client.post(f"/api/v1/accounts/{aid}/upgrade-request", json={}, headers=customer)
    assert first.json()["status"] == "requested"
    assert second.json()["status"] == "already_requested"
    assert len(db.list_upgrade_requests("open")) == 1


def test_an_account_already_on_plus_cannot_request_an_upgrade(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="b")
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_client_token(aid)}
    r = client.post(f"/api/v1/accounts/{aid}/upgrade-request", json={}, headers=customer)
    assert r.status_code == 409


def test_one_customer_cannot_raise_a_request_for_another_account(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    mine = _seed_account(plan="a", email="mine@steprotech.com", username="mine")
    other = _seed_account(plan="a", email="other@steprotech.com", username="other")
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_access_token(str(mine))}
    r = client.post(f"/api/v1/accounts/{other}/upgrade-request", json={}, headers=customer)
    assert r.status_code in (401, 403), r.text


def test_the_account_endpoint_tells_the_customer_what_package_they_hold(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    client = TestClient(app)
    customer = {"Authorization": "Bearer " + create_client_token(aid)}
    body = client.get(f"/api/v1/accounts/{aid}", headers=customer).json()
    assert body["plan"]["current"] == "a"
    assert body["plan"]["can_request_upgrade"] is True
    assert body["plan"]["target_label"].startswith("Plus")
    assert body["upgrade_request"] is None


def test_an_admin_can_close_a_request(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    aid = _seed_account(plan="a")
    rid = db.create_upgrade_request(aid, "a", "b")
    client = TestClient(app)
    r = client.post(f"/api/v1/admin/upgrade-requests/{rid}/handle", headers=_admin())
    assert r.status_code == 200
    assert db.list_upgrade_requests("open") == []
    again = client.post(f"/api/v1/admin/upgrade-requests/{rid}/handle", headers=_admin())
    assert again.status_code == 409, "closing twice must not look like success"
