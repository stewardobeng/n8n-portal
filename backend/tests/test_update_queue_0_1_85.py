"""Workspace update queue (0.1.85, Steward 2026-10-07).

His rules, each pinned by a test below:
  * one workspace at a time PER SERVER, three servers so three streams;
  * five minutes of quiet on that server after a workspace is verified stable;
  * admin batches jump ahead of customer requests;
  * a manual admin update runs immediately and never waits, but it takes the
    server slot so the worker cannot pile another update onto that box;
  * a report when a batch finishes, an immediate report when one fails, and a
    confirmation to the customer whose own workspace was updated;
  * no safety backup before an update (his call, it would eat storage);
  * self-update belongs to the 500 cedi Workspace Plus package only.
"""
import time

import pytest

from app import db
from app.config import settings
from app.services import backup_ops, emailer, update_queue as uq


class _FakeExecutor:
    """Records what the supervisor wanted to start instead of running it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append(args)
        return None


@pytest.fixture(autouse=True)
def _offline_and_fast(tmp_path, monkeypatch):
    """No network, no waiting, no threads: the queue itself is what is under test."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "uq.db"))
    monkeypatch.setattr(settings, "update_interval_minutes", 5)
    monkeypatch.setattr(settings, "update_worker_enabled", True)
    monkeypatch.setattr(settings, "update_verify_timeout_seconds", 1)
    monkeypatch.setattr(settings, "update_report_to", "reports@example.com")
    monkeypatch.setattr(settings, "update_customer_cooldown_hours", 24)
    monkeypatch.setattr(settings, "update_self_service_plan", "b")
    db.init_db()

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
    yield
    uq._RUNNING.clear()


def _mk(tmp_path, monkeypatch, plans=("b", "b", "b", "b"), hosts=(4, 8, 4, 8), statuses=None,
        locked=()):
    """One account with four workspaces spread over two servers."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "uq.db"))
    db.init_db()
    now = int(time.time())
    conn = db.get_conn()
    conn.execute("INSERT INTO accounts (email, username, display_name, password_hash, account_state, "
                 "subscription_status, quota, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("uq@steprotech.com", "uquser", "UQ User", "x", "active", "active", 9, now))
    conn.commit()
    aid = conn.execute("SELECT id FROM accounts WHERE email='uq@steprotech.com'").fetchone()["id"]
    conn.close()
    statuses = statuses or {}
    ids = []
    for n, (plan, host) in enumerate(zip(plans, hosts)):
        iid = db.create_instance(account_id=aid, stack_name="uqws%d" % n, environment_id=host,
                                 environment_name="server " + str(host), port=33000 + n,
                                 domain="uqws%d.example.com" % n, basic_auth_user="u",
                                 basic_auth_password="p", n8n_encryption_key="e" * 32, plan=plan)
        db.update_instance(iid, status=statuses.get(n, "healthy"), stack_id=100 + n,
                           image="n8nio/n8n:2.40.5",
                           locked=1 if n in locked else 0)
        ids.append(iid)
    return aid, ids


# --------------------------------------------------------------- version facts

def test_a_floating_or_digest_tag_never_yields_a_version():
    assert uq.tag_to_version("n8nio/n8n:2.43.1") == "2.43.1"
    assert uq.tag_to_version("n8nio/n8n:latest") == ""
    assert uq.tag_to_version("n8nio/n8n@sha256:abc") == ""
    assert uq.tag_to_version("") == ""


def test_the_queue_refuses_to_guess_a_target(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(uq.n8n_releases, "latest_release", lambda force=False: {"latest": ""})
    res = uq.queue_workspaces(ids[:1], requested_by="admin")
    assert res["ok"] is False and "could not be resolved" in res["error"]
    assert uq.queue_rows() == []


def test_needs_update_is_never_claimed_from_missing_data(tmp_path, monkeypatch):
    """0.1.80 rule kept: an unknown running version is unknown, not 'current'."""
    _, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(backup_ops, "true_versions",
                        lambda insts, pc=None: {int(i["id"]): {"version": "", "source": "unknown"}
                                                for i in insts})
    ws = {w["instance_id"]: w for w in uq.overview()["workspaces"]}
    assert ws[ids[0]]["needs_update"] is False
    assert ws[ids[0]]["current_version"] == ""


def _release_ok(force=False):
    """Exactly what n8n_releases.latest_release() returns: a DICT, not a string."""
    return {"latest": {"tag": "2.43.1", "published_at": "2026-10-07T00:00:00Z"},
            "recent": [], "checked_at": 0}


def _release_down(force=False):
    return {"latest": None, "recent": [], "checked_at": 0}


def test_release_tag_of_accepts_every_shape_the_service_can_return():
    assert uq.release_tag_of({"latest": {"tag": "2.43.1"}}) == "2.43.1"
    assert uq.release_tag_of({"latest": {"tag": "v2.43.1"}}) == "2.43.1"
    assert uq.release_tag_of({"latest": "2.42.0"}) == "2.42.0"
    assert uq.release_tag_of({"latest": None}) == ""
    assert uq.release_tag_of({}) == ""
    assert uq.release_tag_of(None) == ""


def test_the_target_is_resolved_from_the_real_service_shape(tmp_path, monkeypatch):
    """Regression (found live 2026-10-07): the release service returns
    {"latest": {"tag": ...}}. Unwrapping it as a string made every update impossible
    while the page cheerfully reported that no workspace was behind."""
    _mk(tmp_path, monkeypatch)
    tgt = uq.latest_target()
    assert tgt["version"] == "2.43.1" and tgt["source"] == "release"
    assert tgt["image"] == "n8nio/n8n:2.43.1"
    assert uq.queue_all_behind(requested_by="admin")["considered"] > 0


def test_when_the_release_check_is_down_nothing_is_called_up_to_date(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(uq.n8n_releases, "latest_release", _release_down)
    ov = uq.overview()
    assert ov["latest_known"] is False and ov["latest_version"] == ""
    assert ov["compare_note"], "the page must be told why nothing can be compared"
    assert all(w["needs_update"] is False for w in ov["workspaces"]), "no claim either way"
    assert all(w["latest_known"] is False for w in ov["workspaces"])
    items = uq.customer_state(aid)
    assert all(i["can_update"] is False and i["updates_unavailable"] is True for i in items)
    res = uq.queue_all_behind(requested_by="admin")
    assert res["queued"] == [] and "could not be resolved" in res["error"].lower()


def test_a_workspace_behind_the_latest_release_needs_an_update(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    ws = {w["instance_id"]: w for w in uq.overview()["workspaces"]}
    assert ws[ids[0]]["current_version"] == "2.40.5"
    assert ws[ids[0]]["latest_version"] == "2.43.1"
    assert ws[ids[0]]["needs_update"] is True
    monkeypatch.setattr(backup_ops, "true_versions",
                        lambda insts, pc=None: {int(i["id"]): {"version": "2.43.1", "source": "image-label"}
                                                for i in insts})
    ws = {w["instance_id"]: w for w in uq.overview()["workspaces"]}
    assert ws[ids[0]]["needs_update"] is False


# ------------------------------------------------------------------ priority

def test_admin_rows_jump_ahead_of_customer_rows_on_the_same_server(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    uq.queue_workspaces([ids[0]], requested_by="customer", requested_account=aid, priority=100)
    uq.queue_workspaces([ids[2]], requested_by="admin", priority=10)   # ids[2] is on server 4 too
    order = [int(r["instance_id"]) for r in uq.queued_for_env(4)]
    assert order == [ids[2], ids[0]]
    cust_row = [r for r in uq.queue_rows() if r["requested_by"] == "customer"][0]
    assert uq.position_of(int(cust_row["id"]))["position"] == 2


def test_the_customer_sees_their_place_and_a_time_estimate(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    uq.queue_workspaces([ids[0]], requested_by="customer", requested_account=aid, priority=100)
    row = uq.queue_rows()[0]
    pos = uq.position_of(int(row["id"]))
    assert pos["position"] == 1
    assert pos["eta_minutes"] == 5 + uq._typical_minutes()


# --------------------------------------------------- one per server, and pacing

def test_only_one_update_runs_per_server_at_a_time(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    fake = _FakeExecutor()
    monkeypatch.setattr(uq, "_EXECUTOR", fake)
    uq.queue_workspaces(ids, requested_by="admin")          # 2 on server 4, 2 on server 8
    first = uq.worker_tick()
    assert sorted(s["host"] for s in first["started"]) == [4, 8]
    running = [r for r in uq.queue_rows(statuses=("running",))]
    assert len(running) == 2, "one per server, never two on the same box"
    assert sorted({int(r["environment_id"]) for r in running}) == [4, 8]
    # a second tick must not add another update to those busy servers
    assert uq.worker_tick()["started"] == []
    assert len(uq.queue_rows(statuses=("running",))) == 2


def test_a_server_rests_the_quiet_gap_before_its_next_workspace(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    fake = _FakeExecutor()
    monkeypatch.setattr(uq, "_EXECUTOR", fake)
    uq.queue_workspaces([ids[0], ids[2]], requested_by="admin")   # both on server 4
    uq.worker_tick()
    row = uq.queue_rows(statuses=("running",))[0]
    uq.run_row_sync(int(row["id"]))            # finishes + sets the gap
    uq._RUNNING.clear()
    ready = uq.host_ready_at(4)
    assert 4 * 60 <= ready - int(time.time()) <= 5 * 60 + 5, "five minutes of quiet"
    assert uq.worker_tick()["started"] == [], "the second update waits for the gap"
    # once the gap has passed the next one may start
    uq._touch_host(4, next_allowed_at=int(time.time()) - 1)
    assert [s["host"] for s in uq.worker_tick()["started"]] == [4]


def test_finishing_an_update_never_blocks_the_other_servers(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    fake = _FakeExecutor()
    monkeypatch.setattr(uq, "_EXECUTOR", fake)
    uq.queue_workspaces([ids[0]], requested_by="admin")     # server 4
    uq.worker_tick()
    row = uq.queue_rows(statuses=("running",))[0]
    uq.run_row_sync(int(row["id"]))
    uq._RUNNING.clear()
    uq.queue_workspaces([ids[1]], requested_by="admin")     # server 8
    assert [s["host"] for s in uq.worker_tick()["started"]] == [8]


# ------------------------------------------------------------ manual updates

def test_a_manual_update_never_waits_but_takes_the_server_slot(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    fake = _FakeExecutor()
    monkeypatch.setattr(uq, "_EXECUTOR", fake)
    inst = db.get_instance(ids[0])
    qid = uq.reserve_manual(inst, "n8nio/n8n:2.43.1", "2.43.1")
    assert uq.host_state(4)["busy_row"] == qid
    # the worker will not start anything on that server while the manual update runs
    uq.queue_workspaces([ids[2]], requested_by="admin")
    assert uq.worker_tick()["started"] == []
    uq.complete_manual(qid, True, note="manual update from the dashboard", result_version="2.43.1")
    assert uq.host_state(4)["busy_row"] is None
    assert uq.host_ready_at(4) > int(time.time()) + 3 * 60
    got = uq.get_row(qid)
    assert got["status"] == "done" and got["requested_by"] == "admin-manual"


# ---------------------------------------------------------------------- skips

def test_locked_and_provisioning_workspaces_are_skipped_with_a_reason(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch, statuses={1: "provisioning"}, locked=(2,))
    res = uq.queue_workspaces(ids, requested_by="admin")
    reasons = {s["instance_id"]: s["reason"] for s in res["skipped"]}
    assert "locked" in reasons[ids[2]]
    assert "provisioned" in reasons[ids[1]]
    assert len(res["queued"]) == 2


def test_the_same_workspace_is_never_queued_twice(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    uq.queue_workspaces([ids[0]], requested_by="admin")
    res = uq.queue_workspaces([ids[0]], requested_by="customer", requested_account=1)
    assert res["queued"] == []
    assert res["skipped"][0]["reason"] == "already queued or updating"


def test_history_is_newest_first_not_priority_first(tmp_path, monkeypatch):
    """The page labels that list "newest first": an admin row queued FIRST must not
    outrank a customer row that FINISHED later (found in the live rig)."""
    aid, ids = _mk(tmp_path, monkeypatch)
    admin = uq.queue_workspaces([ids[0]], requested_by="admin", priority=10)
    caller = uq.queue_workspaces([ids[1]], requested_by="customer", requested_account=aid, priority=100)
    a_row = uq.queue_rows(batch_id=admin["batch_id"])[0]
    c_row = uq.queue_rows(batch_id=caller["batch_id"])[0]
    uq._finish_row(int(a_row["id"]), "done", result_version="2.43.1")
    conn = db.get_conn()
    conn.execute("UPDATE update_queue SET finished_at = ? WHERE id = ?",
                 (int(time.time()) - 600, int(a_row["id"])))
    conn.commit(); conn.close()
    uq._finish_row(int(c_row["id"]), "done", result_version="2.43.1")
    order = [int(r["id"]) for r in uq.queue_history(10)]
    assert order == [int(c_row["id"]), int(a_row["id"])], "newest first"
    # and each workspace's "last" entry is its genuinely latest row
    ws = {w["instance_id"]: w for w in uq.overview()["workspaces"]}
    assert ws[ids[1]]["last"]["status"] == "done"


def test_a_batch_can_be_cancelled_before_it_starts(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    fake = _FakeExecutor()
    monkeypatch.setattr(uq, "_EXECUTOR", fake)
    res = uq.queue_workspaces(ids, requested_by="admin")
    uq.worker_tick()                                  # two of them start
    n = uq.cancel_batch(res["batch_id"])
    assert n == 2
    assert len(uq.queue_rows(statuses=("running",))) == 2   # running ones are left alone


# --------------------------------------------------------- doing the work

def test_a_successful_update_is_verified_and_recorded(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(backup_ops, "update_instance_image",
                        lambda inst, image: calls.append(image) or image)
    monkeypatch.setattr(backup_ops, "current_version",
                        lambda inst, allow_exec=False: {"version": "2.43.1", "source": "image-label"})
    res = uq.queue_workspaces([ids[0]], requested_by="admin")
    row = uq.queue_rows(statuses=("queued",))[0]
    out = uq.run_row_sync(int(row["id"]))
    assert out["ok"] is True
    assert calls == ["n8nio/n8n:2.43.1"]
    got = uq.get_row(int(row["id"]))
    assert got["status"] == "done"
    assert got["result_version"] == "2.43.1"
    assert got["from_version"] == "2.43.1"          # snapshot taken before the change
    assert "verified running 2.43.1" in got["note"]
    assert res["batch_id"] == got["batch_id"]


def test_a_failed_update_is_rolled_back_and_reported(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(backup_ops, "update_instance_image",
                        lambda inst, image: calls.append(image) or image)
    # the new version never comes up: the workspace keeps reporting the old one
    monkeypatch.setattr(backup_ops, "current_version",
                        lambda inst, allow_exec=False: {"version": "2.40.5", "source": "image-label"})
    mails = []
    monkeypatch.setattr(emailer, "send_update_failure",
                        lambda to, row: mails.append((to, row["stack_name"])))
    report = []
    monkeypatch.setattr(emailer, "send_update_report", lambda to, rows: report.append(to))
    uq.queue_workspaces([ids[0]], requested_by="admin")
    row = uq.queue_rows(statuses=("queued",))[0]
    out = uq.run_row_sync(int(row["id"]))
    assert out["ok"] is False and out["rolled_back"] is True
    assert calls == ["n8nio/n8n:2.43.1", "n8nio/n8n:2.40.5"], "roll back to what it was"
    got = uq.get_row(int(row["id"]))
    assert got["status"] == "failed"
    assert "expected 2.43.1" in got["error"] and "rolled back" in got["error"]
    assert mails == [("reports@example.com", "uqws0")], "the admin hears about it at once"
    assert report == ["reports@example.com"], "and the batch report still goes out"


def test_the_customer_is_told_when_their_own_workspace_is_updated(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(backup_ops, "current_version",
                        lambda inst, allow_exec=False: {"version": "2.43.1", "source": "image-label"})
    sent = []
    monkeypatch.setattr(emailer, "send_workspace_updated",
                        lambda to, stack, domain, frm, to_v, note: sent.append((to, stack, to_v)))
    monkeypatch.setattr(emailer, "send_update_report", lambda to, rows: None)
    uq.queue_workspaces([ids[0]], requested_by="customer", requested_account=aid)
    row = uq.queue_rows(statuses=("queued",))[0]
    uq.run_row_sync(int(row["id"]))
    assert sent == [("uq@steprotech.com", "uqws0", "2.43.1")]


def test_an_admin_batch_report_is_sent_exactly_once(tmp_path, monkeypatch):
    _, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(backup_ops, "current_version",
                        lambda inst, allow_exec=False: {"version": "2.43.1", "source": "image-label"})
    reports = []
    monkeypatch.setattr(emailer, "send_update_report", lambda to, rows: reports.append(len(list(rows))))
    res = uq.queue_workspaces([ids[0], ids[1]], requested_by="admin")
    for row in uq.queue_rows(statuses=("queued",)):
        uq.run_row_sync(int(row["id"]))
    assert len(reports) == 1, "one report per batch, not one per workspace"
    assert reports[0] == 2
    uq._maybe_report(uq.get_row(int(uq.queue_rows(limit=1)[0]["id"])))
    assert len(reports) == 1, "never repeated"
    assert res["batch_id"]


# --------------------------------------------------- the customer's own button

def test_self_update_still_honours_the_plus_switch_for_a_permitted_account(tmp_path, monkeypatch):
    """The 0.1.85 rule survives as the SECOND restriction (0.1.88): with the switch left on
    'b', even a permitted customer may only self-update the Plus workspace."""
    aid, ids = _mk(tmp_path, monkeypatch, plans=("a", "b", "b", "b"))
    db.set_account_update(aid, True)          # the admin allowed this account
    items = {i["instance_id"]: i for i in uq.customer_state(aid)}
    assert items[ids[0]]["update_allowed"] is True
    assert items[ids[0]]["can_update"] is False
    assert "Workspace Plus" in items[ids[0]]["blocked_reason"]
    assert items[ids[1]]["can_update"] is True


def test_an_unpermitted_account_sees_no_update_option_even_on_plus(tmp_path, monkeypatch):
    """0.1.88 (Steward): the permission is the gate. Same Plus workspaces, no permission, so
    the customer is shown nothing at all and told nothing - exactly like backups."""
    aid, ids = _mk(tmp_path, monkeypatch, plans=("a", "b", "b", "b"))
    items = {i["instance_id"]: i for i in uq.customer_state(aid)}
    assert items[ids[1]]["update_allowed"] is False
    assert items[ids[1]]["can_update"] is False
    assert items[ids[1]]["blocked_reason"] == ""


def test_self_update_is_not_offered_when_there_is_nothing_to_do(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(backup_ops, "true_versions",
                        lambda insts, pc=None: {int(i["id"]): {"version": "2.43.1", "source": "image-label"}
                                                for i in insts})
    items = {i["instance_id"]: i for i in uq.customer_state(aid)}
    assert items[ids[0]]["needs_update"] is False
    assert items[ids[0]]["can_update"] is False


def test_a_customer_cannot_hammer_the_update_button(tmp_path, monkeypatch):
    aid, ids = _mk(tmp_path, monkeypatch)
    db.set_account_update(aid, True)          # permitted, so only the cooldown can refuse
    inst = db.get_instance(ids[0])
    qid = uq._insert_row(inst, "n8nio/n8io:2.43.1", "2.43.1", "customer",
                         requested_account=aid, priority=100)
    uq._finish_row(qid, "done", result_version="2.43.1")
    items = {i["instance_id"]: i for i in uq.customer_state(aid)}
    assert items[ids[0]]["can_update"] is False
    assert "recently" in items[ids[0]]["blocked_reason"]
    # after the cooldown they may ask again
    uq._finish_row(qid, "done", result_version="2.43.1")
    conn = db.get_conn()
    conn.execute("UPDATE update_queue SET finished_at = ? WHERE id = ?",
                 (int(time.time()) - 30 * 3600, qid))
    conn.commit()
    conn.close()
    items = {i["instance_id"]: i for i in uq.customer_state(aid)}
    assert items[ids[0]]["can_update"] is True


# ------------------------------------------------------------------ endpoints

def _admin_h():
    from app.security import create_access_token
    return {"Authorization": "Bearer " + create_access_token("admin")}


def _client_h(aid):
    from app.security import create_client_token
    return {"Authorization": "Bearer " + create_client_token(aid)}


def test_admin_updates_page_api_is_admin_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _mk(tmp_path, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/v1/admin/updates").status_code in (401, 403)
    r = c.get("/api/v1/admin/updates", headers=_admin_h())
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("latest_version", "interval_minutes", "hosts", "workspaces", "queue", "history",
                "report_to", "self_service_plan"):
        assert key in body, key
    assert body["interval_minutes"] == 5
    assert body["report_to"] == "reports@example.com"
    assert body["latest_version"] == "2.43.1"
    assert len(body["workspaces"]) == 4
    assert sorted(h["environment_id"] for h in body["hosts"]) == [4, 8]


def test_admin_can_queue_a_selection_and_everything_behind(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(uq, "worker_tick", lambda: {"started": []})
    c = TestClient(app)
    r = c.post("/api/v1/admin/updates/queue", headers=_admin_h(), json={"instance_ids": [ids[0]]})
    assert r.status_code == 200, r.text
    assert len(r.json()["queued"]) == 1
    r = c.post("/api/v1/admin/updates/queue", headers=_admin_h(), json={"all_behind": True})
    assert r.status_code == 200
    # the first one is already queued, so it is not doubled up
    assert len(r.json()["queued"]) == 3
    assert r.json()["skipped"][0]["reason"] == "already queued or updating"
    r = c.post("/api/v1/admin/updates/queue", headers=_admin_h(), json={})
    assert r.status_code == 400


def test_admin_can_cancel_a_batch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(uq, "worker_tick", lambda: {"started": []})
    c = TestClient(app)
    batch = c.post("/api/v1/admin/updates/queue", headers=_admin_h(),
                   json={"instance_ids": ids}).json()["batch_id"]
    r = c.post("/api/v1/admin/updates/cancel?batch_id=" + batch, headers=_admin_h())
    assert r.status_code == 200 and r.json()["canceled"] == 4


def test_customer_self_update_endpoint_queues_and_reports_position(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    aid, ids = _mk(tmp_path, monkeypatch)
    monkeypatch.setattr(uq, "worker_tick", lambda: {"started": []})
    c = TestClient(app)
    # gated
    assert c.get("/api/v1/me/updates").status_code in (401, 403)
    # 0.1.88: not permitted yet, so the API refuses and the row offers nothing
    r = c.post("/api/v1/me/instances/%d/update" % ids[0], headers=_client_h(aid))
    assert r.status_code == 403
    db.set_account_update(aid, True)
    r = c.get("/api/v1/me/updates", headers=_client_h(aid))
    assert r.status_code == 200, r.text
    assert r.json()["update_allowed"] is True
    items = {i["instance_id"]: i for i in r.json()["items"]}
    assert items[ids[0]]["can_update"] is True
    assert items[ids[0]]["latest_version"] == "2.43.1"
    # cannot update someone else's workspace, or one they do not own
    assert c.post("/api/v1/me/instances/9999/update", headers=_client_h(aid)).status_code == 404
    r = c.post("/api/v1/me/instances/%d/update" % ids[0], headers=_client_h(aid))
    assert r.status_code == 202, r.text
    assert r.json()["position"] == 1 and r.json()["status"] == "queued"
    assert r.json()["target_version"] == "2.43.1"
    # asking twice while queued is refused, not silently duplicated
    r2 = c.post("/api/v1/me/instances/%d/update" % ids[0], headers=_client_h(aid))
    assert r2.status_code == 409


def test_a_ghana_300_customer_cannot_self_update(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    aid, ids = _mk(tmp_path, monkeypatch, plans=("a", "a", "a", "a"))
    db.set_account_update(aid, True)          # permitted, but the switch is Plus-only
    monkeypatch.setattr(uq, "worker_tick", lambda: {"started": []})
    c = TestClient(app)
    r = c.post("/api/v1/me/instances/%d/update" % ids[0], headers=_client_h(aid))
    assert r.status_code == 403
    assert "Workspace Plus" in r.json()["detail"]
    assert uq.queue_rows() == []
