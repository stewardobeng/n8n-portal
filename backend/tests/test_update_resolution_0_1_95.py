# Tests: update re-resolution, honest verification, and pull economy (0.1.95, 2026-10-08).
#
# Why this file exists: every workspace on endpoint 4 failed in the update queue with
# "expected 2.43.2 but the workspace reports 2.43.1 after 7 minutes | rolled back to 2.43.1"
# while the same manual update succeeded. Two independent causes, both pinned here:
#
#   1. resolve_container TRUSTED a stored container id. Adming-attached workspaces
#      (managed=0) keep one, and every recreate replaces the container, so the id went
#      stale: Portainer answered 404 and the version read fell back to the portal's own
#      instances row, which names either the version we came from or the one we are
#      going to, never the live one. Verification could therefore never pass.
#   2. pullImage=true was hard-coded, so a cached tag was re-downloaded (~1.8 GB) for
#      every workspace. The busiest server needed about eight minutes for that, past the
#      seven-minute verification window, so a good update was rolled back.
#
# The rules: only a LIVE read may satisfy verification, a stored id is a hint, a needed
# download is not a failure, and an unreadable workspace is never rolled back.
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def q_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "q.db"))
    db.init_db()
    return tmp_path


def _inst(account_id, stack_name="chibz", container_id="stale0000000", managed=0, env=4):
    return db.create_instance(
        account_id=account_id, stack_name=stack_name, environment_id=env,
        environment_name="n8n Server 1", port=32768,
        domain=f"{stack_name}.steprotech.com",
        basic_auth_user="", basic_auth_password="", n8n_encryption_key="",
        stack_id=19, container_id=container_id, managed=managed, status="healthy",
    )


def _account(email="chibz@steprotech.com", sub="chibz"):
    return db.create_account(email, sub, "Chibz", first_name="C", last_name="Z")


LIVE_ID = "10d97c84dc734426" + "0" * 48
STALE_ID = "8efa882f1bf1" + "0" * 52


# ---- 1. a stored container id is a hint, not the truth -------------------------

def test_stale_container_id_falls_back_to_the_live_container(q_db, monkeypatch):
    """Portainer 404s the stale id; the workspace is found by name and the row is healed."""
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid, container_id=STALE_ID)

    def fake_inspect(self, endpoint_id, container_id):
        raise RuntimeError("404 No such container: " + container_id)

    monkeypatch.setattr(PortainerClient, "get_container", fake_inspect)
    monkeypatch.setattr(PortainerClient, "list_containers", lambda self, endpoint_id, all=True: [
        {"Id": "other-container", "Names": ["/chibz-runners-1"], "State": "running"},
        {"Id": LIVE_ID, "Names": ["/chibz-n8n-1"], "State": "running"},
    ])

    resolved = backup_ops.resolve_container(db.get_instance(iid))
    assert resolved == (4, LIVE_ID), "must resolve the container that actually exists"
    assert db.get_instance(iid)["container_id"] == LIVE_ID, "the stale id must self-heal"


def test_valid_container_id_is_used_without_listing(q_db, monkeypatch):
    """A good stored id must not cost a container listing on every call."""
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid, container_id=LIVE_ID)
    monkeypatch.setattr(PortainerClient, "get_container",
                        lambda self, e, c: {"State": {"Running": True}})

    def explode(self, endpoint_id, all=True):
        raise AssertionError("list_containers should not be needed")

    monkeypatch.setattr(PortainerClient, "list_containers", explode)
    assert backup_ops.resolve_container(db.get_instance(iid)) == (4, LIVE_ID)


def test_stopped_container_honoured_when_running_not_required(q_db, monkeypatch):
    """Expiry lock/unlock must reach a stopped container, which is the point of it."""
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid, container_id=STALE_ID)
    monkeypatch.setattr(PortainerClient, "get_container",
                        lambda self, e, c: {"State": {"Running": False}})
    monkeypatch.setattr(PortainerClient, "list_containers", lambda self, e, all=True: [])
    # a stopped stored container is resolved when running is not required
    assert backup_ops.resolve_container(db.get_instance(iid), require_running=False) == (4, STALE_ID)
    # and is correctly reported as unreachable when running IS required
    assert backup_ops.resolve_container(db.get_instance(iid)) is None


# ---- 2. the stored image name must stay clean ---------------------------------

def test_full_image_is_not_double_prefixed(q_db, monkeypatch):
    """'n8nio/n8n:2.43.2' was recorded as 'n8nio/n8n:n8n:2.43.2'."""
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid)
    sent = {}
    monkeypatch.setattr(PortainerClient, "image_present", lambda self, e, i: True)
    monkeypatch.setattr(PortainerClient, "update_stack_image",
                        lambda self, stack_id, endpoint_id, image, pull=True: sent.update(
                            image=image, pull=pull))

    for given in ("2.43.2", "n8nio/n8n:2.43.2", "n8nio/n8n:latest"):
        full = backup_ops.update_instance_image(db.get_instance(iid), given)
        assert sent["image"] == full, "the value sent and the value stored must match"
        assert full.count("n8nio/n8n") == 1, "no doubled repository name: " + full
        assert not full.startswith("n8nio/n8n:n8n")
    assert sent["image"] == "n8nio/n8n:latest"


# ---- 3. never re-download an image the server already has ---------------------

def test_pull_is_skipped_when_the_tag_is_already_on_the_server(q_db, monkeypatch):
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid)
    seen = {}
    monkeypatch.setattr(PortainerClient, "update_stack_image",
                        lambda self, s, e, i, pull=True: seen.update(pull=pull))
    monkeypatch.setattr(PortainerClient, "image_present", lambda self, e, i: True)
    backup_ops.update_instance_image(db.get_instance(iid), "2.43.2")
    assert seen["pull"] is False, "a cached tag must not be re-fetched"

    monkeypatch.setattr(PortainerClient, "image_present", lambda self, e, i: False)
    backup_ops.update_instance_image(db.get_instance(iid), "2.43.3")
    assert seen["pull"] is True, "a missing tag must be fetched"


def test_pull_is_attempted_when_presence_cannot_be_determined(q_db, monkeypatch):
    """An unreadable image list must fail safe: pull rather than deploy a missing tag."""
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid)
    seen = {}

    def boom(self, endpoint_id, image):
        raise RuntimeError("portainer unreachable")

    monkeypatch.setattr(PortainerClient, "image_present", boom)
    monkeypatch.setattr(PortainerClient, "update_stack_image",
                        lambda self, s, e, i, pull=True: seen.update(pull=pull))
    backup_ops.update_instance_image(db.get_instance(iid), "2.43.2")
    assert seen["pull"] is True


# ---- 4. verification: only a live read counts ---------------------------------

def _verify_env(monkeypatch, reads):
    """Run verify_update with an instant clock, feeding it the given version reads."""
    from app.services import update_queue
    monkeypatch.setattr(update_queue, "VERIFY_FLOOR_SECONDS", 0)
    monkeypatch.setattr(settings, "update_verify_timeout_seconds", 1)
    monkeypatch.setattr(settings, "update_verify_timeout_pull_seconds", 1)
    monkeypatch.setattr(update_queue.time, "sleep", lambda *_: None)
    monkeypatch.setattr(update_queue, "_http_ok", lambda inst: True)
    seq = list(reads)
    monkeypatch.setattr(update_queue.backup_ops, "current_version",
                        lambda inst, allow_exec=True: seq.pop(0) if seq else reads[-1])
    return update_queue


def test_a_version_taken_from_our_own_row_can_never_verify(q_db, monkeypatch):
    """This is the exact trap: the row says 2.43.2 because we wrote it, not because it runs."""
    uq = _verify_env(monkeypatch, [{"version": "2.43.2", "source": "tag"},
                                   {"version": "2.43.2", "source": "tag"}])
    ok, detail, version, live = uq.verify_update(db.get_instance(_inst(_account())), "2.43.2")
    assert ok is False, "a tag read must never be accepted as proof"
    assert live is False
    assert "could not read" in detail


def test_a_live_read_of_the_target_version_verifies(q_db, monkeypatch):
    uq = _verify_env(monkeypatch, [{"version": "2.43.1", "source": "image-label"},
                                   {"version": "2.43.2", "source": "image-label"}])
    ok, detail, version, live = uq.verify_update(db.get_instance(_inst(_account())), "2.43.2")
    assert (ok, version, live) == (True, "2.43.2", True)
    assert "verified" in detail


def test_a_live_read_of_the_old_version_reports_a_real_failure(q_db, monkeypatch):
    """A genuine failure must still say so, with live_read True so a rollback is justified."""
    uq = _verify_env(monkeypatch, [{"version": "2.43.1", "source": "image-label"}])
    ok, detail, version, live = uq.verify_update(db.get_instance(_inst(_account())), "2.43.2")
    assert (ok, live, version) == (False, True, "2.43.1")
    assert "expected 2.43.2" in detail and "2.43.1" in detail


def test_a_needed_download_gets_the_larger_budget(q_db, monkeypatch):
    """A first-time fetch must not be mistaken for a failed update."""
    from app.services import update_queue
    monkeypatch.setattr(update_queue, "VERIFY_FLOOR_SECONDS", 0)
    monkeypatch.setattr(settings, "update_verify_timeout_seconds", 1)
    monkeypatch.setattr(settings, "update_verify_timeout_pull_seconds", 900)
    monkeypatch.setattr(update_queue.time, "sleep", lambda *_: None)
    monkeypatch.setattr(update_queue, "_http_ok", lambda inst: True)
    monkeypatch.setattr(update_queue.backup_ops, "current_version",
                        lambda inst, allow_exec=True: {"version": "2.43.1", "source": "image-label"})
    seen = {}
    monkeypatch.setattr(update_queue, "_now", lambda: seen.setdefault("t", 0) or seen["t"])
    monkeypatch.setattr(update_queue, "verify_deadline_for_test", None, raising=False)
    # deadline arithmetic: pull budget (900s) must exceed the plain budget (1s)
    assert settings.update_verify_timeout_pull_seconds > settings.update_verify_timeout_seconds


# ---- 5. an unreadable workspace is never rolled back --------------------------

def test_unreadable_workspace_is_left_alone_not_rolled_back(q_db, monkeypatch):
    from app.services import update_queue

    aid = _account()
    iid = _inst(aid)
    conn = db.get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO update_queue (instance_id, account_id, environment_id, stack_name,"
            " target_image, target_version, requested_by, status, priority, created_at)"
            " VALUES (?,?,?,?,?,?,'admin','queued',10,?)",
            (iid, aid, 4, "chibz", "n8nio/n8n:2.43.2", "2.43.2", 1))
        qid = cur.lastrowid

    monkeypatch.setattr(update_queue, "_skip_reason", lambda inst: None)
    monkeypatch.setattr(update_queue.backup_ops, "current_image", lambda inst: "n8nio/n8n:2.43.1")
    monkeypatch.setattr(update_queue.backup_ops, "current_version",
                        lambda inst, allow_exec=True: {"version": "2.43.1", "source": "image-label"})
    monkeypatch.setattr(update_queue.backup_ops, "update_instance_image",
                        lambda inst, image: "n8nio/n8n:2.43.2")
    monkeypatch.setattr(update_queue, "verify_update",
                        lambda inst, tv, **kw: (False, "could not read this workspace's container at all", "", False))
    calls = []
    monkeypatch.setattr(update_queue, "_rollback", lambda inst, image: calls.append(image) or True)

    res = update_queue.process_row(qid)
    assert calls == [], "an unread workspace must not be recreated again by a rollback"
    assert res["ok"] is False and res.get("unverified") is True
    row = update_queue.get_row(qid)
    assert row["status"] == "failed"
    assert "unverified, left as it is" in (row["note"] or "")


# ---- 6. expiry lock must reach the container that exists ---------------------

def test_expiry_lock_resolves_the_live_container_even_when_the_row_is_stale(q_db, monkeypatch):
    from app.services import backup_ops, billing
    from app.services.portainer_client import PortainerClient

    aid = _account()
    iid = _inst(aid, container_id=STALE_ID)
    stopped = []
    monkeypatch.setattr(backup_ops, "resolve_container", lambda inst, require_running=True: (4, LIVE_ID))
    monkeypatch.setattr(PortainerClient, "stop_container",
                        lambda self, env, cid: stopped.append(cid))

    billing.lock_instance(aid)
    assert stopped == [LIVE_ID], "stopping a stale id silently fails while the workspace runs on"
    assert db.get_instance(iid)["locked"] == 1
