"""Workspace update queue and its worker (0.1.85, Steward 2026-10-07).

The rules Steward set, and where each one lives:

  * one workspace updates at a time PER SERVER - the worker only ever holds one
    in-flight row per environment_id (three servers, so three streams);
  * after each update is verified stable that server rests the quiet gap
    (UPDATE_INTERVAL_MINUTES, default 5) before its next workspace starts, so a
    box never runs two n8n recreations at once and never chases them back to back;
  * an admin batch (priority 10) jumps ahead of customer requests (priority 100);
  * the admin's manual single update still runs immediately, but it RESERVES the
    same server slot, so the worker cannot start anything on that box while it runs;
  * a report is emailed when a batch finishes, immediately when something fails,
    and a customer is emailed when their own workspace was updated;
  * no safety backup before an update (his call: it would eat storage; external
    backup storage is a later project).

The queue is in SQLite so a portal restart cannot lose it, and the in-process
registry only exists to keep the workers from racing each other.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .. import db
from ..config import settings
from . import backup_ops, emailer, n8n_releases

log = logging.getLogger("n8n-portal.update-queue")

# Rows left 'running' by a killed process are re-queued after this long, but only
# when nothing in this process claims them.
STALE_RUNNING_SECONDS = 30 * 60
MAX_ATTEMPTS = 3
# Production never gives a workspace less than a minute to come back, but the floor
# is a module constant so tests can shorten it instead of sleeping.
VERIFY_FLOOR_SECONDS = 60

_RUNNING: dict[int, int] = {}          # environment_id -> queue row id
_RUNNING_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="update-queue")
_STARTED = False
_START_LOCK = threading.Lock()
_ENDPOINT_CACHE: dict = {"at": 0, "rows": []}


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------- targets

def release_tag_of(info) -> str:
    """The release tag out of `n8n_releases.latest_release()`.

    That service returns `{"latest": {"tag": "2.43.1", "published_at": ...}}` (or
    `None` when it could not resolve anything) - NOT a bare string. Unwrapping it
    wrongly is silent: the exception is swallowed and the queue simply refuses every
    update while the page reports nothing is behind, which is how this was caught in
    production on 2026-10-07. A plain string is still accepted so a future change to
    the service cannot break the queue again.
    """
    info = info or {}
    raw = info.get("latest")
    tag = raw.get("tag") if isinstance(raw, dict) else raw
    return str(tag or "").strip().lstrip("v")


def _image_for(version: str) -> str:
    return f"n8nio/n8n:{version}" if version else ""


def tag_to_version(image: str) -> str:
    """'n8nio/n8n:2.43.1' -> '2.43.1'; a floating tag yields ''."""
    if not image:
        return ""
    text = str(image).strip()
    # a digest reference ("...@sha256:abc") carries no version, and neither does a
    # bare image ID; only a real tag like "2.43.1" does.
    if "@sha256" in text or text.startswith("sha256:"):
        return ""
    tag = text.rsplit(":", 1)[-1].strip()
    if not tag or tag == "latest" or tag.startswith("sha256"):
        return ""
    return tag


def latest_target(force: bool = False) -> dict:
    """What an update should move to: {version, image, source}.

    source is 'release' when Docker Hub answered, 'unavailable' when it could not
    be resolved - and an update is refused rather than guessed at.
    """
    version = ""
    try:
        info = n8n_releases.latest_release(force=force) or {}
        version = release_tag_of(info)
        if not version:
            log.warning("latest release lookup returned no usable tag: %r", info.get("latest"))
    except Exception as e:  # noqa: BLE001 - never let reporting break an update
        log.warning("latest release lookup failed: %s", e)
    if not version:
        return {"version": "", "image": "", "source": "unavailable"}
    return {"version": version, "image": _image_for(version), "source": "release"}


# ------------------------------------------------------------------ host state

def _host_row(env: int):
    conn = db.get_conn()
    try:
        cur = conn.execute("SELECT * FROM update_host_state WHERE environment_id = ?", (env,))
        return cur.fetchone()
    finally:
        conn.close()


def host_state(env: int) -> dict:
    row = _host_row(env)
    running = None
    with _RUNNING_LOCK:
        running = _RUNNING.get(int(env))
    return {
        "environment_id": int(env),
        "next_allowed_at": int(row["next_allowed_at"]) if row else 0,
        "last_finished_at": int(row["last_finished_at"]) if row and row["last_finished_at"] else None,
        "busy_row": running,
    }


def _touch_host(env: int, *, next_allowed_at: int | None = None,
                last_finished_at: int | None = None, last_instance_id: int | None = None,
                busy_since: int | None = None) -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO update_host_state (environment_id, next_allowed_at) VALUES (?, 0) "
            "ON CONFLICT(environment_id) DO NOTHING",
            (int(env),),
        )
        sets, vals = [], []
        for key, val in (("next_allowed_at", next_allowed_at), ("last_finished_at", last_finished_at),
                         ("last_instance_id", last_instance_id), ("busy_since", busy_since)):
            if val is not None:
                sets.append(key + " = ?")
                vals.append(int(val))
        if sets:
            vals.append(int(env))
            conn.execute("UPDATE update_host_state SET " + ", ".join(sets) + " WHERE environment_id = ?", vals)
        conn.commit()
    finally:
        conn.close()


def host_ready_at(env: int) -> int:
    row = _host_row(env)
    return int(row["next_allowed_at"]) if row else 0


# ------------------------------------------------------------------ queue rows

def _row_get(row, key, default=None):
    try:
        return row[key]
    except Exception:  # noqa: BLE001
        return default


def queue_rows(statuses=None, batch_id=None, limit=None):
    q = "SELECT * FROM update_queue"
    args: list = []
    where = []
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        args.extend(statuses)
    if batch_id:
        where.append("batch_id = ?")
        args.append(batch_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY priority ASC, id ASC"
    if limit:
        q += " LIMIT " + str(int(limit))
    conn = db.get_conn()
    try:
        return conn.execute(q, args).fetchall()
    finally:
        conn.close()


def queue_history(limit: int = 60):
    """Finished rows, newest first by when they actually finished."""
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT * FROM update_queue WHERE status NOT IN ('queued','running') "
            "ORDER BY COALESCE(finished_at, started_at, created_at) DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
    finally:
        conn.close()


def queued_for_env(env: int):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT * FROM update_queue WHERE environment_id = ? AND status = 'queued' "
            "ORDER BY priority ASC, id ASC", (int(env),)).fetchall()
    finally:
        conn.close()


def _insert_row(inst, target_image: str, target_version: str, requested_by: str,
                requested_account=None, batch_id: str = "", priority: int = 100,
                status: str = "queued") -> int:
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO update_queue (instance_id, account_id, environment_id, stack_name, "
            "target_image, target_version, requested_by, requested_account, batch_id, status, "
            "priority, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(inst["id"]), int(inst["account_id"]), int(inst["environment_id"]),
             inst["stack_name"] or "", target_image, target_version, requested_by,
             requested_account, batch_id, status, int(priority), _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _finish_row(qid: int, status: str, *, error: str = "", note: str = "",
                result_version: str = "", from_image: str = "", from_version: str = "") -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE update_queue SET status = ?, finished_at = ?, error = ?, note = ?, "
            "result_version = ?, from_image = COALESCE(NULLIF(?, ''), from_image), "
            "from_version = COALESCE(NULLIF(?, ''), from_version) WHERE id = ?",
            (status, _now(), error, note, result_version, from_image, from_version, int(qid)),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_running(qid: int) -> None:
    conn = db.get_conn()
    try:
        conn.execute("UPDATE update_queue SET status = 'running', started_at = ?, attempts = attempts + 1 "
                     "WHERE id = ?", (_now(), int(qid)))
        conn.commit()
    finally:
        conn.close()


def get_row(qid: int):
    conn = db.get_conn()
    try:
        return conn.execute("SELECT * FROM update_queue WHERE id = ?", (int(qid),)).fetchone()
    finally:
        conn.close()


def _already_queued(instance_id: int) -> bool:
    conn = db.get_conn()
    try:
        cur = conn.execute("SELECT id FROM update_queue WHERE instance_id = ? AND status IN ('queued','running') "
                           "LIMIT 1", (int(instance_id),))
        return cur.fetchone() is not None
    finally:
        conn.close()


# ------------------------------------------------------------------- enqueue

def _skip_reason(inst) -> str:
    if inst is None:
        return "workspace no longer exists"
    if _row_get(inst, "locked", 0):
        return "access is locked (unpaid), update it after the subscription is restored"
    if _row_get(inst, "status", "") == "provisioning":
        return "still being provisioned"
    if _row_get(inst, "status", "") == "failed":
        return "the workspace is in a failed state, that needs looking at first"
    return ""


def queue_workspaces(instance_ids, *, requested_by: str = "admin", requested_account=None,
                     batch_id: str | None = None, target: dict | None = None,
                     priority: int | None = None) -> dict:
    """Queue the given workspaces. Returns {batch_id, queued, skipped, target}."""
    tgt = target or latest_target()
    if not tgt.get("image"):
        return {"ok": False, "error": "The latest n8n release could not be resolved right now, "
                                     "so nothing was queued.", "queued": [], "skipped": []}
    batch_id = batch_id or uuid.uuid4().hex[:12]
    prio = priority if priority is not None else (100 if requested_by == "customer" else 10)
    queued, skipped = [], []
    for iid in instance_ids:
        inst = db.get_instance(int(iid))
        reason = _skip_reason(inst)
        if reason:
            skipped.append({"instance_id": int(iid), "reason": reason})
            continue
        if _already_queued(int(iid)):
            skipped.append({"instance_id": int(iid), "reason": "already queued or updating"})
            continue
        qid = _insert_row(inst, tgt["image"], tgt["version"], requested_by,
                          requested_account=requested_account, batch_id=batch_id, priority=prio)
        queued.append({"instance_id": int(iid), "queue_id": qid,
                       "stack_name": inst["stack_name"], "target_version": tgt["version"]})
    return {"ok": bool(queued), "batch_id": batch_id, "target": tgt,
            "queued": queued, "skipped": skipped}


def queue_all_behind(*, requested_by: str = "admin", batch_id: str | None = None) -> dict:
    """Every workspace that is genuinely behind the latest release."""
    tgt = latest_target()
    behind = [w["instance_id"] for w in overview()["workspaces"] if w["needs_update"]]
    res = queue_workspaces(behind, requested_by=requested_by, batch_id=batch_id, target=tgt)
    res["considered"] = len(behind)
    return res


def cancel_batch(batch_id: str) -> int:
    conn = db.get_conn()
    try:
        cur = conn.execute("UPDATE update_queue SET status = 'canceled', finished_at = ? "
                           "WHERE batch_id = ? AND status = 'queued'", (_now(), batch_id))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def customer_last_request(instance_id: int):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT * FROM update_queue WHERE instance_id = ? AND requested_by = 'customer' "
            "ORDER BY id DESC LIMIT 1", (int(instance_id),)).fetchone()
    finally:
        conn.close()


def position_of(qid: int) -> dict:
    """Where a queued row sits in ITS SERVER's line (that is what the customer sees)."""
    row = get_row(qid)
    if not row:
        return {"position": None, "ahead": None, "eta_minutes": None}
    if row["status"] != "queued":
        return {"position": 0 if row["status"] == "running" else None,
                "status": row["status"], "eta_minutes": 0 if row["status"] == "running" else None}
    env = int(row["environment_id"])
    ahead = 0
    for other in queued_for_env(env):
        if int(other["id"]) == int(qid):
            break
        ahead += 1
    per = max(1, int(settings.update_interval_minutes)) + _typical_minutes()
    return {"position": ahead + 1, "ahead": ahead, "eta_minutes": (ahead + 1) * per,
            "status": "queued"}


def _typical_minutes() -> int:
    """Average minutes a queued update actually takes, from recent history."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT started_at, finished_at FROM update_queue WHERE status IN ('done','failed') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 20").fetchall()
    finally:
        conn.close()
    spans = [max(0, int(r["finished_at"]) - int(r["started_at"])) for r in rows if r["finished_at"]]
    if not spans:
        return 3
    return max(1, int(round(sum(spans) / len(spans) / 60.0)))


# ------------------------------------------------------------------ overview

def _env_name(env: int) -> str:
    try:
        if _ENDPOINT_CACHE["at"] + 300 < _now():
            from .portainer_client import PortainerClient
            _ENDPOINT_CACHE["rows"] = PortainerClient().list_endpoints() or []
            _ENDPOINT_CACHE["at"] = _now()
    except Exception:  # noqa: BLE001 - names are cosmetic here
        pass
    for e in _ENDPOINT_CACHE["rows"]:
        if int(e.get("Id", 0)) == int(env):
            return e.get("Name") or ("server " + str(env))
    return "server " + str(env)


def overview() -> dict:
    """Everything the admin Updates page needs, and what the customer view reuses."""
    tgt = latest_target()
    latest = tgt["version"]
    latest_known = bool(latest)
    insts = list(db.list_instances())
    accounts = {int(a["id"]): a for a in db.list_accounts()}
    try:
        versions = backup_ops.true_versions(insts)
    except Exception as e:  # noqa: BLE001 - the page must still render
        log.warning("version map failed: %s", e)
        versions = {}

    active = queue_rows(statuses=("queued", "running"))
    by_instance: dict[int, dict] = {}
    for r in active:
        prev = by_instance.get(int(r["instance_id"]))
        if prev is None or (r["status"] == "running" and prev["status"] != "running"):
            pos = position_of(int(r["id"])) if r["status"] == "queued" else {"position": 0, "eta_minutes": 0}
            by_instance[int(r["instance_id"])] = {
                "queue_id": int(r["id"]), "status": r["status"],
                "requested_by": r["requested_by"], "batch_id": r["batch_id"],
                "target_version": r["target_version"], "position": pos.get("position"),
                "eta_minutes": pos.get("eta_minutes"),
            }

    # newest first, so "last update" per workspace is really its latest one
    history = queue_history(60)
    last_by_instance: dict[int, dict] = {}
    for r in history:
        if r["status"] in ("done", "failed", "skipped", "canceled") and int(r["instance_id"]) not in last_by_instance:
            last_by_instance[int(r["instance_id"])] = {
                "status": r["status"], "at": r["finished_at"],
                "from_version": r["from_version"], "to_version": r["result_version"] or r["target_version"],
                "error": r["error"] or "", "note": r["note"] or "",
            }

    workspaces = []
    for inst in insts:
        iid = int(inst["id"])
        acc = accounts.get(int(inst["account_id"]))
        v = versions.get(iid) or {}
        cur_ver = (v.get("version") or "").strip()
        cur_src = v.get("source") or "unknown"
        stored = inst["image"] or ""
        floating = not tag_to_version(stored)
        # "needs update" is only ever claimed when we KNOW the running version and it
        # really is older (0.1.80 rule). An undetermined version is reported as
        # unknown instead, and the admin can still queue that workspace by hand.
        version_unknown = not cur_ver
        needs = bool(latest) and bool(cur_ver) and _semver_lt(cur_ver, latest)
        workspaces.append({
            "instance_id": iid,
            "account_id": int(inst["account_id"]),
            "account": (acc["display_name"] or acc["username"]) if acc else ("account #" + str(inst["account_id"])),
            "email": acc["email"] if acc else "",
            "stack_name": inst["stack_name"],
            "domain": inst["domain"],
            "plan": inst["plan"] or "a",
            "host": int(inst["environment_id"]),
            "host_name": inst["environment_name"] or _env_name(int(inst["environment_id"])),
            "status": inst["status"],
            "locked": bool(inst["locked"]),
            "current_version": cur_ver,
            "version_source": cur_src,
            "floating_tag": floating,
            "version_unknown": version_unknown,
            "latest_version": latest,
            "latest_known": latest_known,
            "needs_update": needs,
            "queued": by_instance.get(iid),
            "last": last_by_instance.get(iid),
        })
    workspaces.sort(key=lambda w: (not w["needs_update"], w["host"], w["stack_name"] or ""))

    hosts = []
    envs = sorted({int(i["environment_id"]) for i in insts})
    per_item = max(1, int(settings.update_interval_minutes)) + _typical_minutes()
    for env in envs:
        hs = host_state(env)
        q = queued_for_env(env)
        running_row = None
        for r in active:
            if int(r["environment_id"]) == env and r["status"] == "running":
                running_row = {
                    "queue_id": int(r["id"]), "instance_id": int(r["instance_id"]),
                    "stack_name": r["stack_name"], "requested_by": r["requested_by"],
                    "started_at": r["started_at"], "target_version": r["target_version"],
                    "elapsed_seconds": (_now() - int(r["started_at"])) if r["started_at"] else 0,
                }
                break
        hosts.append({
            "environment_id": env,
            "name": _env_name(env),
            "next_allowed_at": hs["next_allowed_at"],
            "running": running_row,
            "queued": len(q),
            "ahead_admin": sum(1 for r in q if int(r["priority"]) <= 10),
            "ahead_customer": sum(1 for r in q if int(r["priority"]) > 10),
            "eta_minutes": len(q) * per_item,
        })

    batches = {}
    for r in list(active) + list(history):
        if not r["batch_id"]:
            continue
        b = batches.setdefault(r["batch_id"], {"batch_id": r["batch_id"], "requested_by": r["requested_by"],
                                              "created_at": r["created_at"], "last_at": 0, "total": 0,
                                              "done": 0, "failed": 0, "open": 0})
        b["last_at"] = max(b["last_at"], r["finished_at"] or r["started_at"] or r["created_at"])
        b["total"] += 1
        if r["status"] == "done":
            b["done"] += 1
        elif r["status"] == "failed":
            b["failed"] += 1
        elif r["status"] in ("queued", "running"):
            b["open"] += 1
    batch_list = sorted(batches.values(), key=lambda b: -max(b["created_at"], b["last_at"]))

    return {
        "latest_version": latest,
        "latest_known": latest_known,
        # when the release check itself failed there is nothing to compare, and calling a
        # workspace "up to date" would be a claim we cannot back (the 0.1.80 rule again)
        "compare_note": "" if latest_known else (
            "The latest n8n release could not be read from Docker Hub just now, so no "
            "workspace can be compared and nothing can be queued until it reads again."),
        "latest_source": tgt["source"],
        "interval_minutes": int(settings.update_interval_minutes),
        "typical_minutes": _typical_minutes(),
        "worker_enabled": bool(settings.update_worker_enabled),
        "report_to": settings.update_report_to,
        "self_service_plan": settings.update_self_service_plan,
        "hosts": hosts,
        "workspaces": workspaces,
        "queue": [{"queue_id": int(r["id"]), "instance_id": int(r["instance_id"]),
                   "stack_name": r["stack_name"], "host": int(r["environment_id"]),
                   "requested_by": r["requested_by"], "status": r["status"],
                   "priority": int(r["priority"]), "batch_id": r["batch_id"],
                   "target_version": r["target_version"], "created_at": r["created_at"],
                   "position": (position_of(int(r["id"])).get("position") if r["status"] == "queued" else 0)}
                  for r in active],
        "history": [{"queue_id": int(r["id"]), "instance_id": int(r["instance_id"]),
                     "stack_name": r["stack_name"], "host": int(r["environment_id"]),
                     "status": r["status"], "requested_by": r["requested_by"],
                     "from_version": r["from_version"], "to_version": r["result_version"] or r["target_version"],
                     "error": r["error"] or "", "note": r["note"] or "",
                     "started_at": r["started_at"], "finished_at": r["finished_at"]}
                    for r in history if r["status"] not in ("queued", "running")],
        "batches": batch_list[:20],
    }


def _semver_lt(a: str, b: str) -> bool:
    def parts(x):
        out = []
        for chunk in str(x).split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)
    try:
        return parts(a) < parts(b)
    except Exception:  # noqa: BLE001
        return False


def account_update_allowed(account_id: int) -> bool:
    """The admin's per-account self-update permission (accounts.update_enabled, 0.1.88).

    Same shape as the backup permission: OFF by default, so a customer only sees update
    controls once the admin allows them for that account."""
    acct = db.get_account(int(account_id))
    if not acct:
        return False
    return bool(db._row_get(acct, "update_enabled", 0))


_SELF_SERVICE_OFF = ("", "none", "off", "0", "no")
_SELF_SERVICE_ANY = ("any", "all", "*")


def self_service_mode() -> str:
    """The normalized update switch: 'none' (kill), 'any' (no plan limit) or a plan key."""
    return (settings.update_self_service_plan or "any").strip().lower()


def self_service_open(plan: str) -> bool:
    """Whether the switch lets this workspace's package self-update at all.

    The per-account permission is checked separately and always applies first, so this is
    only the SECOND restriction."""
    mode = self_service_mode()
    if mode in _SELF_SERVICE_OFF:
        return False
    if mode in _SELF_SERVICE_ANY:
        return True
    return plan == mode


def self_service_limited_to_plan() -> bool:
    """True only when the switch names a specific plan, so a plan mismatch is worth
    explaining to the customer (a global 'none' is not: they simply see nothing)."""
    mode = self_service_mode()
    return mode not in _SELF_SERVICE_OFF + _SELF_SERVICE_ANY


def customer_state(account_id: int) -> list:
    """For the customer's own page: version, whether they may self-update, and
    their place in the line."""
    insts = [i for i in db.list_instances(int(account_id))]
    allowed = account_update_allowed(account_id)
    data = overview()
    by_id = {w["instance_id"]: w for w in data["workspaces"]}
    out = []
    for inst in insts:
        iid = int(inst["id"])
        w = by_id.get(iid) or {}
        last = customer_last_request(iid)
        can = (allowed and self_service_open(w.get("plan"))
               and bool(w.get("needs_update")) and not w.get("queued")
               and not w.get("locked") and w.get("status") not in ("provisioning", "failed"))
        blocked = ""
        unavailable = not w.get("latest_known", True)
        if unavailable:
            # the release check failed, so there is nothing to compare against and this
            # row must not read as "on the latest version"
            can = False
            blocked = ""
        elif not allowed or not self_service_open(w.get("plan")):
            # Not permitted, or the switch is off: the customer sees NO update UI at all,
            # exactly like the backup permission, and there is nothing to explain. Only a
            # plan-specific switch mismatch is worth a sentence.
            can = False
            blocked = ("self-update is part of Workspace Plus"
                       if allowed and self_service_limited_to_plan() else "")
        elif not w.get("needs_update"):
            blocked = ""
        elif w.get("queued"):
            blocked = "already in the line"
        elif last and last["finished_at"] and (_now() - int(last["finished_at"])) < \
                settings.update_customer_cooldown_hours * 3600:
            can = False
            blocked = "you updated this workspace recently"
        out.append({
            "instance_id": iid,
            "stack_name": inst["stack_name"],
            "domain": inst["domain"],
            "plan": w.get("plan") or inst["plan"] or "a",
            "update_allowed": allowed,
            "current_version": w.get("current_version") or "",
            "latest_version": w.get("latest_version") or "",
            "needs_update": bool(w.get("needs_update")),
            "floating_tag": bool(w.get("floating_tag")),
            "can_update": bool(can),
            "blocked_reason": blocked,
            "updates_unavailable": bool(unavailable),
            "queued": w.get("queued"),
            "last": w.get("last"),
        })
    return out


# -------------------------------------------------------------------- worker

def _claim_for_env(env: int, now: int):
    """Take the best queued row for this server, atomically."""
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "SELECT id FROM update_queue WHERE environment_id = ? AND status = 'queued' "
            "ORDER BY priority ASC, id ASC LIMIT 1", (int(env),))
        row = cur.fetchone()
        if not row:
            return None
        qid = int(row["id"])
        upd = conn.execute("UPDATE update_queue SET status = 'running', started_at = ?, "
                           "attempts = attempts + 1 WHERE id = ? AND status = 'queued'",
                           (now, qid))
        conn.commit()
        if not upd.rowcount:
            return None
        return qid
    finally:
        conn.close()


def worker_tick() -> dict:
    """One pass: rescue stale rows, then start one update per free server."""
    if not settings.update_worker_enabled:
        return {"started": [], "skipped": "worker disabled"}
    now = _now()
    # rows whose process died mid-update: put them back in line (bounded)
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE update_queue SET status = 'failed', finished_at = ?, "
            "error = 'the portal restarted while this update was running' "
            "WHERE status = 'running' AND attempts >= ? AND started_at < ?",
            (now, MAX_ATTEMPTS, now - STALE_RUNNING_SECONDS))
        conn.execute(
            "UPDATE update_queue SET status = 'queued', started_at = NULL "
            "WHERE status = 'running' AND attempts < ? AND started_at < ?",
            (MAX_ATTEMPTS, now - STALE_RUNNING_SECONDS))
        conn.commit()
    finally:
        conn.close()

    started = []
    envs = sorted({int(r["environment_id"]) for r in queue_rows(statuses=("queued",))})
    for env in envs:
        with _RUNNING_LOCK:
            if env in _RUNNING:
                continue
        if now < host_ready_at(env):
            continue
        qid = _claim_for_env(env, now)
        if not qid:
            continue
        with _RUNNING_LOCK:
            _RUNNING[env] = qid
        _touch_host(env, busy_since=now)
        _EXECUTOR.submit(_run_claimed, env, qid)
        started.append({"host": env, "queue_id": qid})
    return {"started": started}


def run_row_sync(qid: int) -> dict:
    """Process one claimed row and then start its server's quiet gap.

    The worker calls exactly this, so the pacing, the verification and the reporting
    are the same whether an update came from the queue or from a direct call.
    """
    row = get_row(qid)
    env = int(row["environment_id"]) if row else 0
    try:
        return process_row(qid)
    finally:
        if env:
            _after_item(env, get_row(qid))


def _run_claimed(env: int, qid: int) -> None:
    try:
        run_row_sync(qid)
    except Exception as e:  # noqa: BLE001 - a crash must not wedge the server
        log.error("update queue row %s crashed: %s", qid, e)
        try:
            _finish_row(qid, "failed", error=f"unexpected error: {e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(int(env), None)


def _after_item(env: int, row) -> None:
    """Start this server's quiet gap once the workspace is verified stable."""
    _touch_host(env, next_allowed_at=_now() + max(1, int(settings.update_interval_minutes)) * 60,
                last_finished_at=_now(),
                last_instance_id=int(row["instance_id"]) if row else None,
                busy_since=0)
    _maybe_report(row)


# --------------------------------------------------------------- the work

def process_row(qid: int) -> dict:
    """Update one workspace: change the tag, redeploy, verify, roll back if broken."""
    row = get_row(qid)
    if not row:
        return {"ok": False, "error": "row vanished"}
    inst = db.get_instance(int(row["instance_id"]))
    env = int(row["environment_id"])
    reason = _skip_reason(inst)
    if reason:
        _finish_row(qid, "skipped", note=reason)
        return {"ok": False, "skipped": reason}

    from_image = ""
    from_version = ""
    try:
        from_image = backup_ops.current_image(inst)
        from_version = (backup_ops.current_version(inst, allow_exec=True) or {}).get("version") or ""
    except Exception as e:  # noqa: BLE001 - the snapshot is best effort
        log.warning("pre-update snapshot failed for %s: %s", row["stack_name"], e)

    target_image = row["target_image"]
    target_version = row["target_version"] or tag_to_version(target_image)
    if from_image and from_image == target_image and from_version == target_version:
        _finish_row(qid, "done", note="already on " + target_version, result_version=target_version,
                    from_image=from_image, from_version=from_version)
        return {"ok": True, "note": "already current"}

    try:
        backup_ops.update_instance_image(inst, target_image)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        _finish_row(qid, "failed", error=msg[:500], from_image=from_image, from_version=from_version)
        log.error("update failed for %s: %s", row["stack_name"], msg)
        return {"ok": False, "error": msg}

    ok, detail, result_version = verify_update(inst, target_version, from_image=from_image,
                                               from_version=from_version)
    if not ok:
        rolled = _rollback(inst, from_image)
        _finish_row(qid, "failed",
                    error=detail[:400] + (" | rolled back to " + (from_version or from_image) if rolled
                                          else " | rollback also failed, this workspace needs attention"),
                    note="verification failed", from_image=from_image, from_version=from_version)
        return {"ok": False, "error": detail, "rolled_back": rolled}

    _finish_row(qid, "done", note=detail, result_version=result_version,
                from_image=from_image, from_version=from_version)
    if row["requested_by"] == "customer":
        _email_customer_done(row, inst, from_version, result_version, detail)
    return {"ok": True, "version": result_version, "note": detail}


def verify_update(inst, target_version: str, *, from_image: str = "", from_version: str = "") -> tuple:
    """Is the workspace it was asked to be, and is it alive? (0.1.85)

    Container running + the version the container really runs must both match. The
    HTTP probe is recorded but not fatal: a workspace whose version answered cannot
    be written off as broken just because the portal could not reach its domain.
    """
    deadline = _now() + max(int(VERIFY_FLOOR_SECONDS), int(settings.update_verify_timeout_seconds))
    last = {}
    while _now() < deadline:
        try:
            ver = backup_ops.current_version(inst, allow_exec=True) or {}
        except Exception as e:  # noqa: BLE001
            ver = {"version": "", "source": "unknown", "error": str(e)}
        last = ver
        if (ver.get("version") or "") == target_version:
            http = _http_ok(inst)
            note = "verified running " + target_version
            if not http:
                note += " (the portal could not reach its web address after the restart, worth a look)"
            return True, note, target_version
        time.sleep(8)
    seen = last.get("version") or "unknown"
    return False, ("expected " + target_version + " but the workspace reports " + seen +
                   " after " + str(max(1, int(settings.update_verify_timeout_seconds) // 60)) + " minutes"), seen


def _http_ok(inst) -> bool:
    """Best-effort: does n8n answer on the workspace's own address?"""
    import urllib.error
    import urllib.request
    domain = inst["domain"] or ""
    if not domain:
        return False
    for url in (f"https://{domain}/healthz", f"http://{domain}/healthz"):
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200 and "ok" in body.lower():
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _rollback(inst, from_image: str) -> bool:
    if not from_image:
        return False
    try:
        backup_ops.update_instance_image(inst, from_image)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("rollback to %s failed for %s: %s", from_image, inst["stack_name"], e)
        return False


# ------------------------------------------------- admin manual (no waiting)

def reserve_manual(inst, image: str, version: str) -> int:
    """An admin update by hand: runs at once, but holds this server's slot so the
    worker cannot pile a second update onto the same box."""
    qid = _insert_row(inst, image, version, "admin-manual", priority=0, status="running")
    _mark_running(qid)
    with _RUNNING_LOCK:
        _RUNNING[int(inst["environment_id"])] = qid
    _touch_host(int(inst["environment_id"]), busy_since=_now())
    return qid


def complete_manual(qid: int, ok: bool, *, error: str = "", note: str = "",
                    result_version: str = "", from_image: str = "", from_version: str = "") -> None:
    row = get_row(qid)
    _finish_row(qid, "done" if ok else "failed", error=error, note=note,
                result_version=result_version, from_image=from_image, from_version=from_version)
    if row:
        env = int(row["environment_id"])
        with _RUNNING_LOCK:
            _RUNNING.pop(env, None)
        if row["requested_by"] == "admin-manual":
            _touch_host(env, next_allowed_at=_now() + max(1, int(settings.update_interval_minutes)) * 60,
                        last_finished_at=_now(), busy_since=0)


# ----------------------------------------------------------------- reporting

def _report_sent(batch_id: str, kind: str) -> bool:
    conn = db.get_conn()
    try:
        cur = conn.execute("SELECT id FROM update_reports WHERE batch_id = ? AND kind = ?",
                           (batch_id, kind))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _mark_reported(batch_id: str, kind: str, summary: str = "") -> None:
    conn = db.get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO update_reports (batch_id, kind, sent_at, summary) "
                     "VALUES (?,?,?,?)", (batch_id, kind, _now(), summary[:500]))
        conn.commit()
    finally:
        conn.close()


def _batch_rows(batch_id: str):
    """Rows of one batch in the order they ran (the report reads chronologically)."""
    rows = queue_rows(batch_id=batch_id, limit=500)
    return sorted(rows, key=lambda r: (r["started_at"] or r["created_at"], r["id"]))


def _maybe_report(row) -> None:
    """Batch report when the last row of a batch finishes; failure mail at once."""
    if row is None:
        return
    if row["status"] == "failed" and row["batch_id"]:
        _send_failure_mail(row)
    batch_id = row["batch_id"]
    if not batch_id:
        return
    rows = _batch_rows(batch_id)
    if any(r["status"] in ("queued", "running") for r in rows):
        return
    if _report_sent(batch_id, "batch"):
        return
    try:
        emailer.send_update_report(settings.update_report_to, rows)
        _mark_reported(batch_id, "batch")
        log.info("update batch %s report sent (%d rows)", batch_id, len(rows))
    except Exception as e:  # noqa: BLE001
        log.error("update batch report failed: %s", e)


def _send_failure_mail(row) -> None:
    batch_id = row["batch_id"] or ("row-" + str(row["id"]))
    if _report_sent(batch_id, "failure"):
        return
    try:
        emailer.send_update_failure(settings.update_report_to, row)
        _mark_reported(batch_id, "failure")
    except Exception as e:  # noqa: BLE001
        log.error("update failure mail failed: %s", e)


def _email_customer_done(row, inst, from_version: str, to_version: str, note: str) -> None:
    try:
        acc = db.get_account(int(row["account_id"]))
    except Exception:  # noqa: BLE001
        acc = None
    if not acc or not acc["email"]:
        return
    try:
        emailer.send_workspace_updated(acc["email"], inst["stack_name"], inst["domain"],
                                       from_version, to_version, note)
    except Exception as e:  # noqa: BLE001
        log.error("customer update mail failed: %s", e)


def send_report_now(batch_id: str) -> bool:
    """Force a batch report (used by tests and by an admin 'send report' action)."""
    rows = _batch_rows(batch_id)
    if not rows:
        return False
    emailer.send_update_report(settings.update_report_to, rows)
    _mark_reported(batch_id, "batch")
    return True


# -------------------------------------------------------------------- startup

def start_worker(interval_seconds: int = 10) -> None:
    """One daemon thread supervises; each server's update runs on the executor."""
    global _STARTED
    if not settings.update_worker_enabled:
        log.info("update worker disabled by settings")
        return
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True

    def _loop():
        while True:
            try:
                worker_tick()
            except Exception as e:  # noqa: BLE001
                log.warning("update worker tick failed: %s", e)
            time.sleep(interval_seconds)

    threading.Thread(target=_loop, name="update-queue-supervisor", daemon=True).start()
    log.info("update worker started (one workspace per server, %s minute gap)",
             settings.update_interval_minutes)
