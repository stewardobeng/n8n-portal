"""Restart a workspace, for the admin and for the customer (0.1.96, Steward 2026-10-08).

His words: "at where we have the workspaces for each account, add the option to restart the
workspace for both the admin and the user".

Restart is deliberately NOT an update: the same container starts again on the same image with
the same data, so it needs no permission, no release check and no queue. What it does need is
to be harmless:
  * the container is RESOLVED, never taken from a stored id that a recreate has invalidated;
  * the server refuses while an update is queued or running for that workspace, because
    restarting mid-update would fight the update;
  * customers are paced so a restart loop cannot keep a workspace down;
  * the admin is not paced and can act whenever it is needed.
"""
import time

import pytest
from fastapi import HTTPException

from app import db
from app.config import settings
from app.services import backup_ops

LIVE_ID = "10d97c84dc734426" + "0" * 48


@pytest.fixture
def r_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "restart.db"))
    db.init_db()
    return tmp_path


def _account(email="chibz@steprotech.com", sub="chibz"):
    return db.create_account(email, sub, "Chibz", first_name="C", last_name="Z")


def _inst(account_id, stack_name="chibz", container_id="stale0000000", env=4, managed=0):
    return db.create_instance(
        account_id=account_id, stack_name=stack_name, environment_id=env,
        environment_name="n8n Server 1", port=32768,
        domain=f"{stack_name}.steprotech.com",
        basic_auth_user="", basic_auth_password="", n8n_encryption_key="",
        stack_id=19, container_id=container_id, managed=managed, status="healthy",
    )


# ---- the service --------------------------------------------------------------

def test_restart_starts_the_resolved_container(r_db, monkeypatch):
    """Restart must act on the container that exists, exactly like resolve_container decides."""
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid)
    seen = {}
    monkeypatch.setattr(backup_ops, "resolve_container",
                        lambda inst, require_running=True: (4, LIVE_ID))
    monkeypatch.setattr(PortainerClient, "restart_container",
                        lambda self, env, cid, timeout=30: seen.update(env=env, cid=cid))
    assert backup_ops.restart_workspace(db.get_instance(iid)) == LIVE_ID
    assert seen == {"env": 4, "cid": LIVE_ID}


def test_restart_says_why_when_there_is_nothing_to_restart(r_db, monkeypatch):
    aid = _account()
    iid = _inst(aid)
    monkeypatch.setattr(backup_ops, "resolve_container", lambda inst, require_running=True: None)
    with pytest.raises(RuntimeError, match="not running at the moment"):
        backup_ops.restart_workspace(db.get_instance(iid))


def test_restart_never_touches_the_stack_or_the_image(r_db, monkeypatch):
    """No pull, no redeploy: this is the guarantee that restart cannot change a version."""
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid)
    monkeypatch.setattr(backup_ops, "resolve_container",
                        lambda inst, require_running=True: (4, LIVE_ID))
    monkeypatch.setattr(PortainerClient, "restart_container", lambda self, e, c, timeout=30: None)

    def forbidden(*a, **k):
        raise AssertionError("a restart must not update a stack or pull an image")

    monkeypatch.setattr(PortainerClient, "update_stack_image", forbidden)
    monkeypatch.setattr(PortainerClient, "pull_image", forbidden, raising=False)
    monkeypatch.setattr(backup_ops, "update_instance_image", forbidden)
    before = db.get_instance(iid)["image"]
    backup_ops.restart_workspace(db.get_instance(iid))
    assert db.get_instance(iid)["image"] == before


# ---- the admin and customer entry points --------------------------------------

def test_customer_restart_works_and_is_recorded(r_db, monkeypatch):
    from app.main import _restart_instance

    aid = _account()
    iid = _inst(aid)
    monkeypatch.setattr(backup_ops, "restart_workspace", lambda inst: LIVE_ID)
    res = _restart_instance(db.get_instance(iid), actor="customer")
    assert res["ok"] is True and res["status"] == "restarting"
    assert res["container"] == LIVE_ID[:12]
    assert "half a minute" in res["message"]
    assert db.get_setting(f"restart_at:{iid}"), "the pacing clock must be written"


def test_customer_is_paced_but_the_admin_is_not(r_db, monkeypatch):
    from app.main import RESTART_COOLDOWN_SECONDS, _restart_instance

    aid = _account()
    iid = _inst(aid)
    calls = []
    monkeypatch.setattr(backup_ops, "restart_workspace", lambda inst: calls.append(1) or LIVE_ID)
    inst = db.get_instance(iid)

    _restart_instance(inst, actor="customer")
    with pytest.raises(HTTPException) as e:
        _restart_instance(inst, actor="customer")
    assert e.value.status_code == 429, "a customer must not be able to restart in a loop"
    assert str(RESTART_COOLDOWN_SECONDS) in e.value.detail or "seconds" in e.value.detail

    # the admin is not paced: this is the same workspace, in the same second
    _restart_instance(inst, actor="admin")
    assert len(calls) == 2, "the admin restart must go through immediately"

    # and once the cooldown has passed the customer may restart again
    db.set_setting(f"restart_at:{iid}", str(int(time.time()) - RESTART_COOLDOWN_SECONDS - 1))
    _restart_instance(inst, actor="customer")
    assert len(calls) == 3


def test_restart_is_refused_while_an_update_is_queued(r_db, monkeypatch):
    """Restarting mid-update would fight the update, so the server says so instead."""
    from app.main import _restart_instance

    aid = _account()
    iid = _inst(aid)
    monkeypatch.setattr(backup_ops, "restart_workspace", lambda inst: LIVE_ID)
    conn = db.get_conn()
    with conn:
        conn.execute("INSERT INTO update_queue (instance_id, account_id, environment_id,"
                     " stack_name, target_image, target_version, requested_by, status, priority,"
                     " created_at) VALUES (?,?,?,?,?,?,'customer','queued',100,?)",
                     (iid, aid, 4, "chibz", "n8nio/n8n:2.43.2", "2.43.2", int(time.time())))
    with pytest.raises(HTTPException) as e:
        _restart_instance(db.get_instance(iid), actor="customer")
    assert e.value.status_code == 409
    assert "being updated right now" in e.value.detail


def test_a_failed_restart_reports_plainly(r_db, monkeypatch):
    from app.main import _restart_instance

    aid = _account()
    iid = _inst(aid)

    def broken(inst):
        raise RuntimeError("its container is not running at the moment")

    monkeypatch.setattr(backup_ops, "restart_workspace", broken)
    with pytest.raises(HTTPException) as e:
        _restart_instance(db.get_instance(iid), actor="admin")
    assert e.value.status_code == 502
    assert "not running at the moment" in e.value.detail
    assert db.get_setting(f"restart_at:{iid}") is None, "a failed restart must not start the pacing clock"


# ---- the API surface and the buttons -----------------------------------------

def test_both_routes_exist_with_the_right_guards(r_db):
    from app.main import app
    found = {}
    for r in app.routes:
        p = getattr(r, "path", "")
        if p.endswith("/instances/{instance_id}/restart"):
            found[p] = (sorted(getattr(r, "methods", []) or []), len(getattr(r, "dependencies", []) or []))
    assert "/api/v1/me/instances/{instance_id}/restart" in found, "the customer needs their own button"
    assert "/api/v1/admin/instances/{instance_id}/restart" in found, "the admin needs theirs"
    for path, (methods, deps) in found.items():
        assert "POST" in methods, path
        assert deps >= 1, "each restart route must require an authenticated caller: " + path


def test_customer_route_only_restarts_their_own_workspace(r_db):
    """Ownership is checked through the same helper as every other customer route."""
    from app.main import _get_owned_instance

    mine = _account("mine@steprotech.com", "mine")
    theirs = _account("theirs@steprotech.com", "theirs")
    iid = _inst(theirs, stack_name="theirs")
    with pytest.raises(HTTPException) as e:
        _get_owned_instance(mine, iid)
    assert e.value.status_code in (403, 404)
    assert _get_owned_instance(theirs, iid)["id"] == iid


def test_the_buttons_are_wired_in_the_ui(r_db):
    """Both sides get a button, and both buttons hit their own route."""
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-action="ws-restart"' in js, "the customer card needs a restart button"
    assert '"/me/instances/" + Number(act.dataset.id) + "/restart"' in js, "customer route wiring"
    assert js.count('data-action="admin-restart"') >= 2, "admin list and workspace sheet both"
    assert '"/admin/instances/" + rid + "/restart"' in js, "admin route wiring"
    assert "instRestarting" in js, "the button must show it is working"
    # a restart is not an update: it must never be hidden behind the update permission,
    # but it must not be offered while an update is queued or running
    restart_fn = js.split("function restartHtml(inst)", 1)[1].split("async function ensureMyUpdates", 1)[0]
    assert "myUpdatesAllowed" not in restart_fn, "restart must not be gated like self-update"
    assert "info.queued" in restart_fn, "no restart while an update is in the line"
