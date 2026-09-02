# Admin-assisted operations (Steward 2026-09-02):
#   1. environment registry: display numbering (1..N over the n8n servers),
#      full server names, live health + storage per environment.
#   2. admin add-user: create a pending/unpaid portal account with an
#      auto-generated password, emailed to the person.
#   3. unlinked-stack discovery: n8n stacks running (or stopped) on the n8n
#      servers that are not yet bound to any portal account.
#   4. attach: bind an existing n8n stack to a portal account as its instance
#      (no password change, no restart of anything).
#   5. mark-paid: set custom subscription dates on an account (backdating
#      supported) without going through a payment gateway.
#
# Rules kept from the frozen backend:
#   * username: derived from email, dots -> hyphens, unique.
#   * lock = stop the stack/container; unlock = start it.
#   * the sweep only ever stops accounts whose subscription_status is
#     active/past_due/unpaid with a past paid_until, so pending accounts
#     (no dates) are never auto-stopped.
#   * passwords are never stored plaintext; only PBKDF2 hashes.
#
# Environment display numbering: the control host (env 3 "local", unix socket)
# is EXCLUDED from the n8n server pool. The remaining environments are numbered
# 1..N by ascending Portainer endpoint Id (verified live 2026-09-02: 4, 8, 9 ->
# n8n Server 1/2/3). When a server is deleted later, the numbering re-packs
# (1,2,3 from whatever remains), which is exactly the reset Steward asked for.

import logging
import time
import datetime

from .. import db
from ..config import settings
from . import provisioner
from . import billing
from .portainer_client import PortainerClient
from .npm_client import NPMClient
from .emailer import send_admin_welcome_credentials, EmailError

log = logging.getLogger("n8n-portal")

N8N_IMAGE_HINTS = ("n8nio/n8n", "n8n/n8n", "n8nio/n8n:")


def _row_get(row, key, default=None):
    """sqlite3.Row helper: default when the column is missing (older DBs)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


class AdminOpsError(Exception):
    pass


# ---------- environment registry / health ----------

def _n8n_server_endpoints(pc: PortainerClient) -> list[dict]:
    """Portainer endpoints that are n8n servers: exclude the control host
    (unix-socket / 'local') — only TCP agent endpoints count."""
    out = []
    for ep in pc.list_endpoints():
        url = (ep.get("URL") or "").lower()
        if url.startswith("unix://") or (ep.get("Name") or "").lower() in ("local", "control host"):
            continue
        out.append(ep)
    out.sort(key=lambda e: int(e.get("Id", 0)))
    return out


def _env_forward_ip(ep: dict) -> str:
    ip = ep.get("PublicURL") or (ep.get("URL") or "").replace("tcp://", "").split(":")[0]
    return ip or ""


def _display_number(index: int) -> str:
    return f"n8n Server {index}"


def environment_overview(pc: PortainerClient | None = None,
                         npm: NPMClient | None = None) -> list[dict]:
    """Admin 'environments' cards: display number, full server name, IP,
    reachability, running n8n stacks, portal-linked count, unlinked stacks,
    storage used by n8n data volumes."""
    pc = pc or PortainerClient()
    npm = npm or NPMClient()
    servers = _n8n_server_endpoints(pc)

    # portal-linked instance counts per environment
    linked: dict[int, int] = {}
    for inst in db.list_instances():
        linked[inst["environment_id"]] = linked.get(inst["environment_id"], 0) + 1

    overview = []
    for i, ep in enumerate(servers, start=1):
        eid = int(ep.get("Id", 0))
        ip = _env_forward_ip(ep)
        reachable = int(ep.get("Status", 0)) == 1

        running = 0
        stacks_seen: set[str] = set()
        volumes_bytes = 0
        try:
            containers = pc.list_containers(eid, all=True)
            for c in containers:
                name = (c.get("Names") or [""])[0].lstrip("/")
                image = c.get("Image") or ""
                if not any(h in image for h in N8N_IMAGE_HINTS):
                    continue
                # stack name = compose project label, else container prefix
                labels = c.get("Labels") or {}
                stack = labels.get("com.docker.compose.project", "")
                if not stack:
                    stack = name.rsplit("-", 1)[0]
                stacks_seen.add(stack)
                if c.get("State") == "running":
                    running += 1
        except Exception as e:
            log.warning("env %s container scan failed: %s", eid, e)

        try:
            df = pc._req("GET", f"/endpoints/{eid}/docker/system/df").json()
            for v in df.get("Volumes") or []:
                if (v.get("Name") or "").endswith("_n8n_data"):
                    usage = v.get("UsageData") or {}
                    volumes_bytes += int(usage.get("Size") or 0)
        except Exception as e:
            log.warning("env %s df failed: %s", eid, e)

        overview.append({
            "display_no": i,
            "display_name": _display_number(i),
            "endpoint_id": eid,
            "name": ep.get("Name", f"env-{eid}"),
            "ip": ip,
            "reachable": reachable,
            "running_n8n": running,
            "total_n8n_stacks": len(stacks_seen),
            "linked_accounts": linked.get(eid, 0),
            "unlinked_stacks": len(stacks_seen) - linked.get(eid, 0),
            "storage_bytes": volumes_bytes,
        })
    return overview


# ---------- admin add-user ----------

def admin_create_account(email: str, first_name: str, last_name: str) -> dict:
    """Create a pending portal account with an auto-generated password and email
    the credentials. Returns {account_id, email, username, password_once}."""
    email = (email or "").strip().lower()
    if not email:
        raise AdminOpsError("Email is required.")
    if db.get_account_by_email(email):
        raise AdminOpsError("An account with this email already exists.")
    username = provisioner.ensure_unique_username(None, email)
    password = db.new_password(length=12)
    try:
        provisioner.validate_password_policy(password)
    except provisioner.ProvisionError:
        password = db.new_password(length=14)
    from ..security import hash_password
    account_id = db.create_account(
        email, username, display_name=f"{first_name} {last_name}".strip(),
        first_name=first_name, last_name=last_name,
        password_hash=hash_password(password),
    )
    try:
        send_admin_welcome_credentials(email, email, username, password)
    except EmailError as e:
        log.warning("admin-welcome email failed for %s: %s", email, e)
        # account still created; password returned once for the admin to share
    return {
        "account_id": account_id,
        "email": email,
        "username": username,
        "password_once": password,
    }


# ---------- unlinked stack discovery ----------

def _container_stack_name(c: dict) -> str:
    labels = c.get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    if project:
        return project
    name = (c.get("Names") or [""])[0].lstrip("/")
    # container typically <stack>-n8n-1
    for sep in ("-n8n-", "_n8n_"):
        if sep in name:
            return name.split(sep)[0]
    return name


def discover_unlinked_stacks(pc: PortainerClient | None = None,
                             npm: NPMClient | None = None) -> list[dict]:
    """n8n stacks (running or stopped) on the n8n servers not yet bound to a
    portal account. Attach candidates."""
    pc = pc or PortainerClient()
    npm = npm or NPMClient()
    servers = _n8n_server_endpoints(pc)

    linked_names = {inst["stack_name"] for inst in db.list_instances()}

    # NPM hosts keyed by (forward_ip, forward_port) -> domain, for those that
    # point at n8n servers (domain discovery for stopped containers).
    try:
        npm_hosts = npm.list_proxy_hosts()
    except Exception as e:
        log.warning("NPM host list failed: %s", e)
        npm_hosts = []

    found: list[dict] = []
    for ep in servers:
        eid = int(ep.get("Id", 0))
        ip = _env_forward_ip(ep)
        try:
            containers = pc.list_containers(eid, all=True)
        except Exception as e:
            log.warning("unlinked scan env %s failed: %s", eid, e)
            continue
        per_stack: dict[str, dict] = {}
        for c in containers:
            image = c.get("Image") or ""
            if not any(h in image for h in N8N_IMAGE_HINTS):
                continue
            stack = _container_stack_name(c)
            if stack in linked_names:
                continue
            cid = c.get("Id", "")
            if cid not in per_stack or c.get("State") == "running":
                ports = [p for p in (c.get("Ports") or []) if p.get("PublicPort")]
                per_stack[stack] = {
                    "environment_id": eid,
                    "stack_name": stack,
                    "container_id": cid,
                    "running": c.get("State") == "running",
                    "port": int(ports[0]["PublicPort"]) if ports else 0,
                    "domain": "",
                }
        for stack, info in per_stack.items():
            # try to recover domain from NPM hosts pointing at this env+port
            if info["port"]:
                for h in npm_hosts:
                    if (h.get("forward_host") == ip
                            and int(h.get("forward_port") or 0) == info["port"]):
                        names = h.get("domain_names") or []
                        if names:
                            info["domain"] = names[0]
                        break
            found.append(info)
    found.sort(key=lambda s: (s["environment_id"], s["stack_name"]))
    return found


# ---------- NPM created_on subscription anchor (2026-09-02) ----------

def npm_subscription_anchor(account_id: int) -> dict | None:
    """Source-of-truth anchor for a pre-existing workspace: the NPM proxy
    host's created_on is when the service actually began (Steward: "when you go
    there you will see for each account when it was created there is a date...
    the proxy itself"). Returns the start date and the expiry exactly one year
    later, so mark-paid can preload real dates instead of guessing from today."""
    inst = db.get_active_instance(account_id)
    if not inst:
        return None
    npm_host_id = None
    try:
        npm_host_id = inst["npm_host_id"]
    except (KeyError, IndexError):
        npm_host_id = None
    if not npm_host_id:
        # fall back to matching by domain from the NPM list (id only)
        try:
            npm = NPMClient()
            for h in npm.list_proxy_hosts():
                names = h.get("domain_names") or []
                domain = inst["domain"].rstrip("/")
                if domain in [n.rstrip("/") for n in names]:
                    npm_host_id = h.get("id")
                    break
        except Exception:
            return None
    if not npm_host_id:
        return None
    try:
        host = NPMClient().get_proxy_host(int(npm_host_id))
        created_on = host.get("created_on") or ""
    except Exception as e:
        log.warning("npm anchor: host %s lookup failed: %s", npm_host_id, e)
        return None
    if not created_on:
        return None
    # created_on format: 'YYYY-MM-DD HH:MM:SS' (NPM stores local server time)
    try:
        start = datetime.datetime.strptime(created_on[:10], "%Y-%m-%d")
    except ValueError:
        return None
    # expiry = exactly one calendar year later; Feb 29 clamps to Feb 28 when
    # the next year is not a leap year (same rule banks use for anniversaries)
    try:
        expiry = start.replace(year=start.year + 1)
    except ValueError:
        expiry = datetime.datetime(start.year + 1, 2, 28)
    return {
        "domain": inst["domain"],
        "npm_host_id": int(npm_host_id),
        "created_on": created_on,
        "start": start.strftime("%Y-%m-%d"),
        "expiry": expiry.strftime("%Y-%m-%d"),
    }


# ---------- admin suspend / archive (2026-09-02) ----------

def suspend_account(account_id: int) -> dict:
    """Admin suspension (soft, reversible): account_state='suspended', the
    attached workspace is STOPPED immediately (billing.lock_instance — absolute
    lock semantics, nothing reachable), portal login blocked while suspended.
    Subscription/paid dates are preserved; unsuspend does not auto-start the
    workspace (admin unlocks/renews explicitly)."""
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")
    if account["account_state"] == "archived":
        raise AdminOpsError("Account is archived. Restore it first.")
    stopped = billing.lock_instance(account_id)
    db.set_account_state(account_id, "suspended")
    log.info("account %s suspended (workspace stopped: %s)", account_id, stopped)
    return {
        "account_id": account_id,
        "account_state": "suspended",
        "workspace_stopped": bool(stopped),
    }


def unsuspend_account(account_id: int) -> dict:
    """Restore a suspended account to active. The workspace is NOT auto-started:
    access resumes only when the admin unlocks it or a renewal/marked-paid
    restarts it (deliberate: suspension may be for non-payment, so restarting
    on unsuspend alone would bypass the payment gate)."""
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")
    if account["account_state"] != "suspended":
        raise AdminOpsError("Account is not suspended.")
    db.set_account_state(account_id, "active")
    log.info("account %s unsuspended (workspace left as-is)", account_id)
    return {"account_id": account_id, "account_state": "active"}


def archive_account(account_id: int) -> dict:
    """Soft-delete: account_state='archived'. Workspace stopped immediately and
    stays off. The row is kept (nothing permanently deleted) and hidden from
    admin lists; restore brings it back as suspended (safe: stopped workspace,
    admin reviews before unlocking)."""
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")
    if account["account_state"] == "archived":
        raise AdminOpsError("Account is already archived.")
    stopped = billing.lock_instance(account_id)
    db.set_account_state(account_id, "archived")
    log.info("account %s archived (workspace stopped: %s)", account_id, stopped)
    return {
        "account_id": account_id,
        "account_state": "archived",
        "workspace_stopped": bool(stopped),
    }


def restore_account(account_id: int) -> dict:
    """Bring an archived account back, landing it in suspended (never straight
    to active: the workspace stays stopped until the admin explicitly unlocks
    or renews it)."""
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")
    if account["account_state"] != "archived":
        raise AdminOpsError("Account is not archived.")
    db.set_account_state(account_id, "suspended")
    log.info("account %s restored from archive (suspended)", account_id)
    return {"account_id": account_id, "account_state": "suspended"}


# ---------- attach ----------

def attach_instance(account_id: int, environment_id: int, stack_name: str,
                    port: int, domain: str = "",
                    pc: PortainerClient | None = None) -> dict:
    """Bind an existing n8n stack on an n8n server to a portal account.
    Verifies the stack really exists (running or stopped), links it as the
    account's instance, and sets nothing paid. Password untouched."""
    pc = pc or PortainerClient()
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")

    # quota gate mirrors the provisioner
    quota = account["quota"] if "quota" in account.keys() else settings.default_quota
    live = db.count_instances(account_id)
    if live >= quota:
        raise AdminOpsError(f"Instance quota reached ({live}/{quota}).")

    # stack must exist on this environment (running or stopped)
    try:
        containers = pc.list_containers(environment_id, all=True)
    except Exception as e:
        raise AdminOpsError(f"Cannot reach environment {environment_id}: {e}")
    container = None
    actual_port = port
    for c in containers:
        image = c.get("Image") or ""
        if not any(h in image for h in N8N_IMAGE_HINTS):
            continue
        if _container_stack_name(c) == stack_name:
            container = c
            ports = [p for p in (c.get("Ports") or []) if p.get("PublicPort")]
            if ports and not actual_port:
                actual_port = int(ports[0]["PublicPort"])
            break
    if not container:
        raise AdminOpsError(
            f"No n8n stack '{stack_name}' found on this environment "
            "(it must exist before attach).")

    existing = db.get_instance_by_stack_name(stack_name)
    if existing:
        raise AdminOpsError(f"Stack '{stack_name}' is already attached to another account.")

    if not domain:
        # derive: try NPM for the port on this env, else the username domain
        ep = pc.get_endpoint(environment_id)
        forward_ip = _env_forward_ip(ep)
        npm = NPMClient()
        try:
            for h in npm.list_proxy_hosts():
                if (h.get("forward_host") == forward_ip
                        and int(h.get("forward_port") or 0) == actual_port):
                    names = h.get("domain_names") or []
                    if names:
                        domain = names[0]
                        break
        except Exception:
            pass
    if not domain:
        domain = f"{stack_name}.{settings.base_domain}"
        log.info("attach: domain not found in NPM for %s:%s, assuming %s",
                 stack_name, actual_port, domain)

    # Capture the NPM proxy-host id (if one exists for the domain) so later
    # mark-paid can anchor the subscription start on the proxy host's created_on
    # (Steward 2026-09-02: NPM proxy creation date = source of truth for when
    # the service began; expiry = created_on + exactly one year).
    npm_host_id: int | None = None
    try:
        npm = NPMClient()
        for h in npm.list_proxy_hosts():
            names = h.get("domain_names") or []
            if domain in names or domain.rstrip("/") in [n.rstrip("/") for n in names]:
                npm_host_id = h.get("id")
                break
    except Exception:
        pass

    # environment display label: find the server number in the n8n pool
    env_label = f"env-{environment_id}"
    for i, srv in enumerate(_n8n_server_endpoints(pc), start=1):
        if int(srv.get("Id", 0)) == environment_id:
            env_label = _display_number(i)
            break

    instance_id = db.create_instance(
        account_id=account_id,
        stack_name=stack_name,
        environment_id=environment_id,
        environment_name=env_label,
        port=actual_port,
        domain=domain,
        basic_auth_user="",          # untouched: admin-attached stacks keep their
        basic_auth_password="",      # existing owner password (Steward 2026-09-02)
        n8n_encryption_key="",
        stack_id=None,               # may not be a Portainer-managed stack
        container_id=container.get("Id", ""),
        managed=0,
        status="healthy",
    )
    if npm_host_id:
        db.update_instance(instance_id, npm_host_id=npm_host_id)
    # Record the container's actual state: stopped stack -> locked=1 so the
    # unlock path (start) is the one that brings it back; running -> locked=0.
    running_now = bool(container and container.get("State") == "running")
    db.update_instance(instance_id, locked=0 if running_now else 1)
    # account status: provisioned once it owns an instance; still unpaid until
    # the admin marks it paid.
    if account["status"] in ("pending", "failed"):
        db.set_account_status(account_id, "provisioned")
    log.info("attached stack %s -> account %s (instance %s, running=%s)",
             stack_name, account_id, instance_id, running_now)
    return {
        "instance_id": instance_id,
        "account_id": account_id,
        "stack_name": stack_name,
        "port": actual_port,
        "domain": domain,
        "running": running_now,
    }


# ---------- mark paid (custom dates) ----------

def mark_paid(account_id: int, paid_until: int, paid_from: int | None = None,
              start_if_running: bool = True) -> dict:
    """Admin marks an account paid/subscribed with custom dates (backdating OK).
    Sets subscription_status=active + paid_until (the sweep then enforces it:
    past dates lock on the next sweep run — that is how 'already expiring' works).
    If an attached instance is currently stopped and the expiry is in the future,
    it is started so the user gets access (mirror of a renewal)."""
    account = db.get_account(account_id)
    if not account:
        raise AdminOpsError("Account not found.")
    now = int(time.time())

    # NPM created_on anchor (Steward 2026-09-02): when no dates are given AND
    # the account has never been marked paid, the proxy-host creation date is
    # the source of truth — start = created_on, expiry = + one calendar year.
    if paid_from is None and paid_until <= 0:
        anchor = npm_subscription_anchor(account_id)
        if anchor and anchor["start"] and anchor["expiry"]:
            paid_from = int(time.mktime(
                datetime.datetime.strptime(anchor["start"], "%Y-%m-%d").timetuple()))
            paid_until = int(time.mktime(
                datetime.datetime.strptime(anchor["expiry"], "%Y-%m-%d").timetuple()))
            log.info("mark-paid: anchored dates from NPM created_on %s -> %s",
                     anchor["start"], anchor["expiry"])

    if paid_until <= 0:
        raise AdminOpsError("Expiry date is required.")
    if paid_until <= now:
        # backdating an already-expired subscription: record as expired/unpaid;
        # the sweep locks the instance on its next pass.
        db.update_subscription_status(account_id, "unpaid", paid_until, paid_from)
        db.set_account_status(account_id, "provisioned")
        # stop instance now so the state is truthful immediately
        _ensure_stopped(account_id)
        return {
            "account_id": account_id,
            "subscription_status": "unpaid",
            "paid_until": paid_until,
            "note": "Expiry is in the past; instance stopped. Renew with a future date to restart.",
        }

    db.update_subscription_status(account_id, "active", paid_until, paid_from)
    db.set_account_status(account_id, "provisioned")
    # Admin lifecycle outranks billing: a suspended/archived account keeps its
    # workspace off even when marked paid (money recorded, access NOT granted).
    if start_if_running and _row_get(account, "account_state", "active") == "active":
        _ensure_started(account_id)
    elif start_if_running:
        log.info("mark-paid: account %s is %s; workspace left stopped",
                 account_id, _row_get(account, "account_state", "active"))
    return {
        "account_id": account_id,
        "subscription_status": "active",
        "paid_from": paid_from,
        "paid_until": paid_until,
    }


def _stop_for_account(account_id: int):
    """Container/stack-level stop helper (see billing for the lock engine)."""
    from . import billing
    return billing.lock_instance(account_id)


def _start_for_account(account_id: int):
    from . import billing
    return billing.unlock_instance(account_id)


def _ensure_stopped(account_id: int) -> bool:
    inst = db.get_active_instance(account_id)
    if not inst:
        return False
    if inst["locked"]:
        return True
    return bool(_stop_for_account(account_id))


def _ensure_started(account_id: int) -> bool:
    inst = db.get_active_instance(account_id)
    if not inst:
        return False
    if not inst["locked"]:
        return True
    return bool(_start_for_account(account_id))
