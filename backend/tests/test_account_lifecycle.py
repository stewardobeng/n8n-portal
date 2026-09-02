# Tests for admin account lifecycle: suspend / unsuspend / archive / restore
# (2026-09-02). Suspend/archive STOP the workspace immediately (absolute-lock
# semantics via billing.lock_instance) and block portal login; nothing is
# permanently deleted; sweep + renewals skip admin-managed accounts.
# Run with DB_PATH set (the suite runner does this), like test_admin_ops.py.

import time

import pytest

from app import db
from app.services import admin_ops, billing


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "t.db"))
    import app.db as dbm
    real = dbm.get_conn

    def _conn(db_path=None):
        return real(db_path or str(tmp_path / "t.db"))

    monkeypatch.setattr(dbm, "get_conn", _conn)
    yield
    monkeypatch.setattr(dbm, "get_conn", real)


@pytest.fixture(autouse=True)
def no_portainer(monkeypatch):
    """Lock engine stubs: record calls AND mirror the real engine's DB effect
    (lock_instance sets instances.locked=1, unlock_instance clears it)."""
    calls = {"stopped": [], "started": []}

    def fake_stop(account_id):
        calls["stopped"].append(account_id)
        inst = db.get_active_instance(account_id)
        if inst:
            db.update_instance(inst["id"], locked=1)
        return True

    def fake_start(account_id):
        calls["started"].append(account_id)
        inst = db.get_active_instance(account_id)
        if inst:
            db.update_instance(inst["id"], locked=0)
        return True

    monkeypatch.setattr(billing, "lock_instance", fake_stop)
    monkeypatch.setattr(billing, "unlock_instance", fake_start)
    return calls


def _mk_account(state="active", paid=False, with_instance=False):
    ns = time.time_ns()
    acc = db.create_account(f"sus-{ns}@steprotech.com", f"sus-{ns}", "Sus Test",
                            "Sus", "Test", db.new_key(prefix="h", nbytes=16))
    if with_instance:
        db.create_instance(acc, f"sus-stack-{ns}", 8, "n8n Server 2", 32770,
                           f"sus-{ns}.steprotech.com", f"sus-{ns}@steprotech.com",
                           "", "k" * 64, stack_id=None, container_id="c1",
                           managed=0, status="healthy")
    if paid:
        db.update_subscription_status(acc, "active", int(time.time()) + 86400 * 30)
    if state != "active":
        db.set_account_state(acc, state)
    return acc


# ---------- suspend ----------

def test_suspend_stops_workspace_and_sets_state(no_portainer):
    acc = _mk_account(paid=True)
    out = admin_ops.suspend_account(acc)
    assert out["account_state"] == "suspended"
    assert out["workspace_stopped"] is True
    assert no_portainer["stopped"] == [acc]
    assert db.get_account(acc)["account_state"] == "suspended"


def test_suspended_blocks_login(monkeypatch):
    from app import main as app_main
    acc = _mk_account(paid=True)
    # correct password, active -> token
    pw = "Correct-Pass-1"
    conn = db.get_conn()
    conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                 (db.new_password(0) if False else __import__(
                     "app.security", fromlist=["hash_password"]).hash_password(pw), acc))
    conn.commit()
    conn.close()
    out = app_main.client_login(type("P", (), {"email": db.get_account(acc)["email"],
                                               "password": pw})())
    assert out.token
    # suspend -> 403
    db.set_account_state(acc, "suspended")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        app_main.client_login(type("P", (), {"email": db.get_account(acc)["email"],
                                             "password": pw})())
    assert ei.value.status_code == 403
    assert "suspended" in ei.value.detail


def test_me_blocks_suspended(monkeypatch):
    from app import main as app_main
    from fastapi import HTTPException
    acc = _mk_account(paid=True)
    db.set_account_state(acc, "suspended")
    with pytest.raises(HTTPException) as ei:
        app_main.me(acc)
    assert ei.value.status_code == 403


def test_suspend_archived_rejected():
    acc = _mk_account(state="archived")
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.suspend_account(acc)


# ---------- unsuspend ----------

def test_unsuspend_returns_active_without_starting(no_portainer):
    acc = _mk_account()
    admin_ops.suspend_account(acc)
    out = admin_ops.unsuspend_account(acc)
    assert out["account_state"] == "active"
    # deliberate: no auto-start (payment gate preserved)
    assert no_portainer["started"] == []
    assert db.get_account(acc)["account_state"] == "active"


def test_unsuspend_non_suspended_rejected():
    acc = _mk_account()
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.unsuspend_account(acc)


# ---------- archive / restore ----------

def test_archive_stops_and_hides(no_portainer):
    acc = _mk_account(paid=True)
    out = admin_ops.archive_account(acc)
    assert out["account_state"] == "archived"
    assert no_portainer["stopped"] == [acc]
    # hidden from the default admin list
    listed = admin_ops.suspend_account  # placeholder to keep import used
    assert all(a["id"] != acc for a in _list_default())
    # included with include_archived
    assert any(a["id"] == acc for a in _list_archived())


def _list_default():
    from app import main as app_main
    return app_main.admin_accounts()


def _list_archived():
    from app import main as app_main
    return app_main.admin_accounts(include_archived=1)


def test_restore_lands_in_suspended_not_active(no_portainer):
    acc = _mk_account()
    admin_ops.archive_account(acc)
    out = admin_ops.restore_account(acc)
    assert out["account_state"] == "suspended"
    assert no_portainer["started"] == []  # stays off


def test_restore_non_archived_rejected():
    acc = _mk_account()
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.restore_account(acc)


def test_double_archive_rejected():
    acc = _mk_account()
    admin_ops.archive_account(acc)
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.archive_account(acc)


# ---------- sweep + renewal guards ----------

def test_sweep_skips_suspended_and_archived(monkeypatch):
    stopped = []
    monkeypatch.setattr(billing, "lock_instance", lambda aid: stopped.append(aid) or True)
    live = _mk_account(paid=False)
    # push live account into the expired window with a sweepable status
    conn = db.get_conn()
    conn.execute("UPDATE accounts SET paid_until = ?, subscription_status = 'active' WHERE id = ?",
                 (int(time.time()) - 100, live))
    conn.commit()
    conn.close()
    sus = _mk_account(paid=True)
    arch = _mk_account(paid=True)
    admin_ops.suspend_account(sus)
    admin_ops.archive_account(arch)
    for aid in (sus, arch):
        conn = db.get_conn()
        conn.execute("UPDATE accounts SET paid_until = ? WHERE id = ?",
                     (int(time.time()) - 100, aid))
        conn.commit()
        conn.close()
    res = billing.sweep_expired()
    assert live in res["locked"]
    assert sus not in res["locked"]
    assert arch not in res["locked"]


def test_charge_success_ignores_suspended(monkeypatch):
    from fastapi import HTTPException
    acc = _mk_account()
    admin_ops.suspend_account(acc)
    event = {"mock": True, "type": "charge.success",
             "data": {"metadata": {"account_id": str(acc)}}}
    res = billing.mock_handle_event(event)
    assert res["status"] == "ignored_suspended"
    row = db.get_account(acc)
    assert row["subscription_status"] != "active"
    assert row["account_state"] == "suspended"


def test_mark_paid_does_not_restart_suspended(no_portainer):
    acc = _mk_account()
    admin_ops.suspend_account(acc)
    future = int(time.time()) + 86400 * 365
    admin_ops.mark_paid(acc, future)
    assert no_portainer["started"] == []  # money recorded, workspace stays off
    assert db.get_account(acc)["subscription_status"] == "active"


def test_mark_paid_starts_active_account(no_portainer):
    acc = _mk_account(with_instance=True)
    admin_ops.suspend_account(acc)
    admin_ops.unsuspend_account(acc)  # back to active
    future = int(time.time()) + 86400 * 365
    admin_ops.mark_paid(acc, future)
    assert acc in no_portainer["started"]
