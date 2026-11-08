"""Per-account self-update permission (0.1.88, Steward 2026-10-07).

His words: "implement the user update but, let it only be available to the accounts that are
permitted. which means add option on the admin portal to allow update on an account, just like
how we do it for the backups."

So the gate is a per-account admin permission (`accounts.update_enabled`), OFF by default, with
exactly the shape of the backup permission:
  * while it is OFF the customer sees NO update UI at all, and the API refuses the request;
  * the admin toggles it from the accounts table and from the account page, behind a confirm
    modal, and can revoke it at any time;
  * the old plan setting is now only a SECOND restriction: 'none' = global kill switch,
    'any' = no plan restriction, 'b' = Workspace Plus only.
"""
import time

import pytest

from app import db
from app.config import settings
from app.services import backup_ops, update_queue as uq


def _release_ok(force=False):
    return {"latest": {"tag": "2.43.1"}}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """No network, no threads: the permission rules are what is under test."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ua.db"))
    monkeypatch.setattr(settings, "update_self_service_plan", "any")
    monkeypatch.setattr(settings, "update_worker_enabled", False)
    monkeypatch.setattr(settings, "update_customer_cooldown_hours", 24)
    monkeypatch.setattr(uq, "VERIFY_FLOOR_SECONDS", 1)
    monkeypatch.setattr(uq.time, "sleep", lambda *_: None)
    monkeypatch.setattr(uq, "_env_name", lambda env: "server " + str(env))
    monkeypatch.setattr(uq.n8n_releases, "latest_release", _release_ok)
    monkeypatch.setattr(backup_ops, "true_versions",
                        lambda insts, pc=None: {int(i["id"]): {"version": "2.40.5", "source": "image-label"}
                                                for i in insts})
    monkeypatch.setattr(backup_ops, "current_image", lambda inst: "n8nio/n8n:2.40.5")
    monkeypatch.setattr(backup_ops, "current_version",
                        lambda inst, allow_exec=False: {"version": "2.40.5", "source": "image-label"})
    monkeypatch.setattr(backup_ops, "update_instance_image", lambda inst, image: image)
    monkeypatch.setattr(uq, "_http_ok", lambda inst: True)
    uq._RUNNING.clear()
    db.init_db()
    yield
    uq._RUNNING.clear()


def _mk(tmp_path, monkeypatch, plan="b", update_enabled=0):
    """One account with one workspace that is genuinely behind (2.40.5 vs 2.43.1)."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ua.db"))
    db.init_db()
    now = int(time.time())
    conn = db.get_conn()
    conn.execute("INSERT INTO accounts (email, username, display_name, password_hash, "
                 "account_state, subscription_status, quota, created_at, plan, update_enabled) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("ua@steprotech.com", "uauser", "UA User", "x", "active", "active", 9, now,
                  plan, update_enabled))
    conn.commit()
    aid = conn.execute("SELECT id FROM accounts WHERE email='ua@steprotech.com'").fetchone()["id"]
    conn.close()
    iid = db.create_instance(account_id=aid, stack_name="uaws", environment_id=8,
                             environment_name="server 8", port=33000,
                             domain="uaws.steprotech.com", basic_auth_user="u",
                             basic_auth_password="p", n8n_encryption_key="e" * 32, plan=plan)
    db.update_instance(iid, status="healthy", stack_id=100, image="n8nio/n8n:2.40.5")
    return aid, iid


# --------------------------------------------------------------- the permission itself

def test_the_column_exists_and_defaults_to_off(tmp_path, monkeypatch):
    """A brand-new account may NOT self-update: permission is granted, never assumed."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ua.db"))
    db.init_db()
    cols = [r[1] for r in db.get_conn().execute("PRAGMA table_info(accounts)")]
    assert "update_enabled" in cols
    aid, _ = _mk(tmp_path, monkeypatch)
    acct = db.get_account(aid)
    assert db._row_get(acct, "update_enabled", 0) == 0
    assert uq.account_update_allowed(aid) is False


def test_a_row_inserted_without_the_column_reads_off(tmp_path, monkeypatch):
    """Any account that predates the feature reads 0: the DEFAULT is 'not permitted'."""
    aid, _ = _mk(tmp_path, monkeypatch)
    now = int(time.time())
    conn = db.get_conn()
    conn.execute("INSERT INTO accounts (email, username, display_name, password_hash, "
                 "account_state, subscription_status, quota, created_at, plan) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 ("old@steprotech.com", "olduser", "Old User", "x", "active", "active", 1,
                  now, "a"))
    conn.commit()
    old_id = conn.execute("SELECT id FROM accounts WHERE email='old@steprotech.com'").fetchone()["id"]
    conn.close()
    assert old_id != aid
    assert uq.account_update_allowed(old_id) is False


def test_the_migration_adds_the_column_to_a_database_that_never_had_it(tmp_path, monkeypatch):
    """A pre-0.1.88 database (accounts table with no update_enabled) is upgraded on boot,
    and its existing customer is NOT silently granted self-update."""
    import sqlite3
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, "
                "username TEXT, display_name TEXT, password_hash TEXT, account_state TEXT, "
                "subscription_status TEXT, quota INTEGER, created_at INTEGER)")
    raw.execute("INSERT INTO accounts (email, username, display_name, password_hash, "
                "account_state, subscription_status, quota, created_at) "
                "VALUES ('legacy@steprotech.com','legacy','Legacy','x','active','active',1,1)")
    raw.commit()
    raw.close()

    db.init_db()

    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]
    assert "update_enabled" in cols
    legacy_id = conn.execute("SELECT id FROM accounts WHERE email='legacy@steprotech.com'").fetchone()["id"]
    conn.close()
    assert db._row_get(db.get_account(legacy_id), "update_enabled", 0) == 0
    assert uq.account_update_allowed(legacy_id) is False


def test_the_admin_can_allow_and_revoke(tmp_path, monkeypatch):
    aid, iid = _mk(tmp_path, monkeypatch)
    assert uq.account_update_allowed(aid) is False
    db.set_account_update(aid, True)
    assert uq.account_update_allowed(aid) is True
    db.set_account_update(aid, False)
    assert uq.account_update_allowed(aid) is False


# ------------------------------------------------------- what the customer is shown

def test_an_unpermitted_customer_sees_no_update_ui_at_all(tmp_path, monkeypatch):
    """Mirrors the backup permission: no permission = NO update UI, and no message either."""
    aid, iid = _mk(tmp_path, monkeypatch)
    row = {i["instance_id"]: i for i in uq.customer_state(aid)}[iid]
    assert row["update_allowed"] is False
    assert row["can_update"] is False
    assert row["blocked_reason"] == ""


def test_a_permitted_customer_can_update_when_genuinely_behind(tmp_path, monkeypatch):
    aid, iid = _mk(tmp_path, monkeypatch, update_enabled=1)
    row = {i["instance_id"]: i for i in uq.customer_state(aid)}[iid]
    assert row["update_allowed"] is True
    assert row["needs_update"] is True
    assert row["can_update"] is True
    assert row["blocked_reason"] == ""


def test_permission_does_not_override_a_current_workspace(tmp_path, monkeypatch):
    """Being permitted never invents an update that is not needed."""
    aid, iid = _mk(tmp_path, monkeypatch, update_enabled=1)
    monkeypatch.setattr(backup_ops, "true_versions",
                        lambda insts, pc=None: {int(i["id"]): {"version": "2.43.1", "source": "image-label"}
                                                for i in insts})
    row = {i["instance_id"]: i for i in uq.customer_state(aid)}[iid]
    assert row["update_allowed"] is True
    assert row["needs_update"] is False
    assert row["can_update"] is False


def test_the_global_switch_still_stops_everything(tmp_path, monkeypatch):
    aid, iid = _mk(tmp_path, monkeypatch, update_enabled=1)
    monkeypatch.setattr(settings, "update_self_service_plan", "none")
    row = {i["instance_id"]: i for i in uq.customer_state(aid)}[iid]
    assert row["update_allowed"] is True       # the account IS permitted
    assert row["can_update"] is False          # but the kill switch wins
    assert row["blocked_reason"] == ""


def test_the_plus_only_switch_still_narrows_it(tmp_path, monkeypatch):
    aid, iid = _mk(tmp_path, monkeypatch, plan="a", update_enabled=1)
    monkeypatch.setattr(settings, "update_self_service_plan", "b")
    row = {i["instance_id"]: i for i in uq.customer_state(aid)}[iid]
    assert row["update_allowed"] is True
    assert row["can_update"] is False
    assert "Workspace Plus" in row["blocked_reason"]


# ------------------------------------------------------------------- the API surface

def test_the_admin_route_grants_and_revokes_and_is_admin_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_access_token
    aid, _ = _mk(tmp_path, monkeypatch)
    client = TestClient(app)

    r = client.put("/api/v1/admin/accounts/%d/update-access" % aid,
                   json={"update_enabled": True})
    assert r.status_code in (401, 403), r.status_code

    h = {"Authorization": "Bearer " + create_access_token("admin")}
    r = client.put("/api/v1/admin/accounts/%d/update-access" % aid,
                   json={"update_enabled": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["update_enabled"] is True
    assert uq.account_update_allowed(aid) is True

    r = client.put("/api/v1/admin/accounts/%d/update-access" % aid,
                   json={"update_enabled": False}, headers=h)
    assert r.status_code == 200 and r.json()["update_enabled"] is False
    assert uq.account_update_allowed(aid) is False

    r = client.put("/api/v1/admin/accounts/999999/update-access",
                   json={"update_enabled": True}, headers=h)
    assert r.status_code == 404


def test_the_account_payload_carries_the_permission(tmp_path, monkeypatch):
    """The admin UI reads `update_enabled` off the account rows."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_access_token
    aid, _ = _mk(tmp_path, monkeypatch, update_enabled=1)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + create_access_token("admin")}
    r = client.get("/api/v1/admin/accounts", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()["accounts"] if isinstance(r.json(), dict) else r.json()
    row = [a for a in rows if a["id"] == aid][0]
    assert row["update_enabled"] is True


def test_me_updates_tells_the_customer_whether_it_is_allowed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_client_token
    aid, iid = _mk(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + create_client_token(aid)}

    d = client.get("/api/v1/me/updates", headers=h).json()
    assert d["update_allowed"] is False
    assert d["items"][0]["can_update"] is False

    db.set_account_update(aid, True)
    d = client.get("/api/v1/me/updates", headers=h).json()
    assert d["update_allowed"] is True
    assert d["items"][0]["can_update"] is True


def test_the_customer_update_route_refuses_without_the_permission(tmp_path, monkeypatch):
    """Defense in depth: hiding the button is not the gate, the API is."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_client_token
    aid, iid = _mk(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + create_client_token(aid)}

    r = client.post("/api/v1/me/instances/%d/update" % iid, headers=h)
    assert r.status_code == 403, r.text
    assert "not enabled for this account" in r.json()["detail"]
    assert uq.queue_rows() == []          # nothing was queued

    db.set_account_update(aid, True)
    r = client.post("/api/v1/me/instances/%d/update" % iid, headers=h)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "queued"
    assert body["target_version"] == "2.43.1"
    assert len(uq.queue_rows()) == 1


def test_revoking_does_not_cancel_an_update_already_in_the_line(tmp_path, monkeypatch):
    """Steward's own wording in the revoke popup: an update already queued keeps running."""
    aid, iid = _mk(tmp_path, monkeypatch, update_enabled=1)
    uq.queue_workspaces([iid], requested_by="customer", requested_account=aid, priority=100)
    assert len(uq.queue_rows()) == 1
    db.set_account_update(aid, False)
    assert len(uq.queue_rows()) == 1
    assert uq.account_update_allowed(aid) is False


# ------------------------------------------------------------------ the admin UI

def _appjs():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()


def test_the_accounts_table_has_a_real_update_column():
    js = _appjs()
    assert "<th>Backup</th><th>Update</th>" in js
    assert 'data-action="admin-update-toggle"' in js
    assert 'data-action="admin-backup-toggle"' in js          # the pattern it mirrors
    assert 'var updBadge = a.update_enabled ?' in js
    assert '<td colspan="8">' in js
    assert 'loadingRow(8, "Loading customer accounts..."' in js


def test_the_account_page_has_an_update_access_card_like_the_backup_card():
    js = _appjs()
    assert "<h2>Backup access</h2>" in js and "<h2>Update access</h2>" in js
    assert "Allow customer to update" in js and "Revoke update access" in js


def test_the_toggle_confirms_before_it_changes_anything():
    js = _appjs()
    assert 'data-confirm-admin-update-toggle' in js
    assert '/update-access", { method: "PUT", body: { update_enabled: updTOn } }' in js
    assert "Revoke update access?" in js and "Allow this customer to update?" in js


def test_the_customer_side_is_gated_on_the_permission_not_the_package():
    js = _appjs()
    flat = " ".join(js.split())
    assert 'if (!state.myUpdatesAllowed) return "";' in flat
    assert 'state.myUpdatesAllowed = !!(d && d.update_allowed);' in flat
    assert 'myUpdatesAllowed: false,' in js
    # the old plan-only gate must be gone: a permitted GHS 300 customer is allowed too
    assert 'if (!info || info.plan !== "b") return "";' not in flat
