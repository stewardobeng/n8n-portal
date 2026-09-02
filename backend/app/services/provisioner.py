# Provisioner: the heart of the portal.
# Pipeline for one account:
#   1. validate/derive username + domain
#   2. pick environment (admin-configured landing env) + allocate next free port
#   3. build per-account env vars (unique encryption key, credentials)
#   4. create Portainer stack from canonical compose template
#   5. wait for container to become healthy/running
#   6. create NPM proxy host (domain -> env-ip:port)
#   7. request + attach Let's Encrypt cert via NPM
#   8. email the client their credentials
# On failure at any step: roll back created resources (stack, proxy host, cert).

import json
import re
import time
from datetime import datetime

from ..config import settings
from .. import db
from .portainer_client import PortainerClient
from .npm_client import NPMClient

USERNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,60}[a-z0-9])?$")
RESERVED_USERNAMES = {"admin", "www", "api", "portal", "app", "mail", "smtp",
                      "ftp", "test", "dev", "staging", "support", "webhook"}


class ProvisionError(Exception):
    """Raised when provisioning fails; carries a rollback-safe message."""


# ---------- naming helpers ----------

def derive_username_from_email(email: str) -> str:
    """Take the local part of the email, lowercase, collapse non-alnum to '-'.
    e.g. John.Doe+test@example.com -> john-doe-test
    """
    local = email.split("@")[0].lower()
    local = re.sub(r"[^a-z0-9]+", "-", local).strip("-")
    local = re.sub(r"-{2,}", "-", local)
    return local[:60]


def validate_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise ProvisionError(
            "Username must be 2-62 chars: lowercase letters, digits, hyphens (no leading/trailing hyphen)."
        )
    if username in RESERVED_USERNAMES:
        raise ProvisionError(f"Username '{username}' is reserved.")
    if db.get_account_by_username(username):
        raise ProvisionError(f"Username '{username}' is already taken.")
    if db.get_instance_by_stack_name(username):
        raise ProvisionError(f"Username '{username}' is already provisioned.")
    return username


def ensure_unique_username(username: str | None, email: str) -> str:
    """Use provided username if valid+free, else derive from email; ensure uniqueness."""
    if username and username.strip():
        return validate_username(username)
    base = derive_username_from_email(email)
    if not base:
        raise ProvisionError("Could not derive a username from the email.")
    candidate, i = base, 1
    while True:
        try:
            return validate_username(candidate)
        except ProvisionError:
            i += 1
            candidate = f"{base}-{i}"
            if i > 20:
                raise ProvisionError("Could not allocate a unique username.")


# ---------- port allocation ----------

def used_ports_all_sources(pc: PortainerClient, npm: NPMClient, endpoint_id: int,
                           forward_ip: str | None = None) -> list[int]:
    """Authoritative "ports in use" for an environment, merged from three sources:
      1. NPM proxy hosts pointing at this environment's IP  (survives stopped containers)
      2. Portainer stack env vars (N8N_PORT) on this endpoint (ground truth per tenant)
      3. Docker container published ports (running containers, belt-and-braces)
    Docker alone is NOT sufficient: exited containers report ports=[] (verified 2026-09-01),
    so a stopped tenant's port would look 'free' and collide on restart.
    """
    used: set[int] = set()

    # Source 1: NPM proxy hosts -> forward_host matches this env's IP
    if forward_ip:
        try:
            hosts = npm.list_proxy_hosts()
            for h in hosts:
                if h.get("forward_host") == forward_ip:
                    p = h.get("forward_port")
                    if p:
                        used.add(int(p))
        except Exception:
            pass  # NPM unavailable: fall back to other sources

    # Source 2: Portainer stacks on this endpoint -> N8N_PORT env var
    try:
        stacks = pc.list_stacks()
        for s in stacks:
            if s.get("EndpointId") != endpoint_id:
                continue
            for e in s.get("Env") or []:
                if e.get("name") == "N8N_PORT" and e.get("value"):
                    used.add(int(e["value"]))
    except Exception:
        pass

    # Source 3: Docker published ports (any state Docker reports)
    for p in pc.used_ports(endpoint_id):
        used.add(p)

    # Only tenant-range ports matter for allocation (excludes 9001 agent, 45876 beszel, etc.)
    return sorted(p for p in used if settings.port_range_start <= p <= settings.port_range_end)


def next_free_port(client: PortainerClient, npm: NPMClient | None = None,
                   endpoint_id: int = 0, forward_ip: str | None = None) -> int:
    """Find the highest used port on the environment (all sources), return +1."""
    if npm and endpoint_id:
        used = used_ports_all_sources(client, npm, endpoint_id, forward_ip)
    else:
        used = [p for p in client.used_ports(endpoint_id)
                if settings.port_range_start <= p <= settings.port_range_end]
    highest = max(used) if used else settings.port_range_start - 1
    candidate = highest + 1
    if candidate > settings.port_range_end:
        raise ProvisionError(f"Environment {endpoint_id} is out of ports (range ends at {settings.port_range_end}).")
    return candidate


# ---------- env building ----------

def build_stack_env(username: str, port: int, encryption_key: str,
                    basic_auth_user: str, basic_auth_password: str) -> list[dict]:
    """Per-account env vars — the .env template from Steward, filled per tenant."""
    domain = f"{username}.{settings.base_domain}"
    webhook = f"https://{domain}/"
    return [
        {"name": "N8N_PORT", "value": str(port)},
        {"name": "N8N_HOST", "value": domain},
        {"name": "N8N_PROTOCOL", "value": "https"},
        {"name": "WEBHOOK_URL", "value": webhook},
        {"name": "N8N_EDITOR_BASE_URL", "value": webhook},
        {"name": "N8N_ENCRYPTION_KEY", "value": encryption_key},
        {"name": "DB_TYPE", "value": "sqlite"},
        {"name": "DB_SQLITE_VACUUM_ON_STARTUP", "value": "true"},
        {"name": "GENERIC_TIMEZONE", "value": settings.default_timezone},
        {"name": "N8N_PROXY_HOPS", "value": "1"},
        {"name": "N8N_EMAIL_MODE", "value": settings.n8n_email_mode},
        {"name": "N8N_SMTP_HOST", "value": settings.n8n_smtp_host},
        {"name": "N8N_SMTP_PORT", "value": str(settings.n8n_smtp_port)},
        {"name": "N8N_SMTP_USER", "value": settings.n8n_smtp_user},
        {"name": "N8N_SMTP_PASS", "value": settings.n8n_smtp_pass},
        {"name": "N8N_SMTP_SENDER", "value": settings.n8n_smtp_sender},
        {"name": "N8N_SMTP_SSL", "value": settings.n8n_smtp_ssl},
        {"name": "N8N_SMTP_STARTTLS", "value": settings.n8n_smtp_starttls},
        # Basic auth (door lock) — set to the client's email + generated password
        {"name": "N8N_BASIC_AUTH_ACTIVE", "value": "true"},
        {"name": "N8N_BASIC_AUTH_USER", "value": basic_auth_user},
        {"name": "N8N_BASIC_AUTH_PASSWORD", "value": basic_auth_password},
    ]


def load_compose_template() -> str:
    with open(settings.compose_template_path, "r") as f:
        return f.read()


# ---------- environment selection ----------

def _count_n8n_running(client: PortainerClient, endpoint_id: int) -> int:
    """Running n8n containers on an environment (load metric for placement)."""
    try:
        containers = client.list_containers(endpoint_id, all=True)
    except Exception:
        return 0
    n = 0
    for c in containers:
        if c.get("State") == "running" and "n8n" in (c.get("Image") or ""):
            n += 1
    return n


def resolve_landing_environment(client: PortainerClient) -> tuple[int, str]:
    """Admin-configured landing env (comma-separated ids in DB setting
    'landing_environments', e.g. '8,4,9'). Placement rule (Steward 2026-09-02):
    * exactly one id -> that environment is the source of truth, always used;
    * several ids -> the system auto-decides: least-loaded (fewest running n8n
      containers) among the healthy ones; ties fall back to config order.
    The UI presents the n8n servers (1..N) with checkboxes; single vs multi
    selection maps directly onto these two behaviours."""
    raw = db.get_setting("landing_environments", default="8")  # default: n8n-cloud 2
    order = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    if not order:
        raise ProvisionError("No landing environment configured.")
    endpoints = {e["Id"]: e for e in client.list_endpoints()}
    healthy = [(eid, endpoints[eid]) for eid in order
               if eid in endpoints and endpoints[eid].get("Status") == 1]
    if not healthy:
        raise ProvisionError("No landing environment is reachable.")
    if len(healthy) == 1:
        eid, ep = healthy[0]
        return eid, ep.get("Name", f"env-{eid}")
    # auto-decide: least loaded
    scored = [(eid, _count_n8n_running(client, eid)) for eid, _ in healthy]
    scored.sort(key=lambda x: (x[1], order.index(x[0])))
    eid = scored[0][0]
    return eid, endpoints[eid].get("Name", f"env-{eid}")


# ---------- rollback helpers ----------

def _rollback_stack(pc: PortainerClient, stack_id: int, env_id: int,
                    username: str = "") -> None:
    try:
        pc.delete_stack(stack_id, env_id)
    except Exception:
        pass
    # Remove the named data volume too, otherwise a re-provision mounts the stale
    # n8n DB (owner already set / unknown password) and breaks owner setup
    # (verified 2026-09-01 with gamma). Best-effort: ignore failures.
    if username:
        pc.delete_volume(env_id, f"{username}_n8n_data")


def _rollback_npm(npm: NPMClient, host_id: int | None, cert_id: int | None) -> None:
    if host_id:
        try:
            npm.delete_proxy_host(host_id)
        except Exception:
            pass
    # certs attached to a deleted host are cleaned by NPM; leave orphaned certs
    # (they are harmless and reusable) — do not delete to avoid breaking other hosts.


# ---------- n8n owner auto-creation ----------

def _n8n_ready(origin: str, timeout: int = 150) -> bool:
    """Wait until n8n's API is genuinely up (healthz returns real JSON), not the
    boot-time SPA shell which answers 200 for everything."""
    import urllib.request
    import urllib.error

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{origin}/healthz", timeout=5) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200 and "ok" in body.lower():
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def create_n8n_owner(forward_ip: str, port: int, email: str, password: str,
                     first_name: str = "N8n", last_name: str = "User",
                     timeout: int = 150) -> None:
    """Auto-create the n8n OWNER account so the emailed credentials work on first
    login (no first-run setup page). n8n's endpoint is POST /rest/owner/setup with
    {email, firstName, lastName, password}; it requires a NON-EMPTY lastName
    (400 'Last name is required') and a password meeting n8n's policy
    (>=1 uppercase, lowercase, digit — verified 2026-09-01).
    Uses the container's origin directly (env-ip:port), not the public URL, so it
    works regardless of DNS/cert state. Waits for real API readiness first, then
    retries; success only on a JSON response carrying a user id (a bare 200 from
    the boot-time SPA shell is NOT success — verified 2026-09-01)."""
    import base64
    import urllib.request
    import urllib.error

    origin = f"http://{forward_ip}:{port}"
    if not _n8n_ready(origin, timeout=timeout):
        raise ProvisionError("n8n did not become ready in time for owner setup.")

    b64 = base64.b64encode(f"{email}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = json.dumps({
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "password": password,
    }).encode()

    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        req = urllib.request.Request(f"{origin}/rest/owner/setup", data=payload,
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200 and '"id"' in body and '"email"' in body:
                    return  # genuine owner-created JSON
                last_err = f"HTTP {resp.status} non-JSON: {body[:80]}"
        except urllib.error.HTTPError as e:
            # 404 = n8n still booting (route not up yet); 409/400 = owner exists or bad
            if e.code == 409:
                raise ProvisionError("Owner setup already done on this instance.")
            if e.code == 400:
                raise ProvisionError(f"Owner setup rejected: {e.read().decode()[:200]}")
            last_err = f"HTTP {e.code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(5)
    raise ProvisionError(f"Timed out waiting for n8n owner setup: {last_err}")


def change_n8n_password(forward_ip: str, port: int, email: str, current_password: str,
                        new_password: str, timeout: int = 60, use_public: bool = False,
                        domain: str = "") -> None:
    """Change the n8n owner password (admin reset flow). n8n v1.x endpoint is
    PATCH /rest/me/password {currentPassword, newPassword} with a logged-in
    cookie session. IMPORTANT (verified live 2026-09-01): this endpoint enforces
    the N8N_HOST Host-header match, so it MUST be reached via the PUBLIC origin
    (https://{domain}); calling http://{forward_ip}:{port} directly returns
    401 Unauthorized even with valid basic-auth + session, while /rest/login
    tolerates the direct IP. Raises ProvisionError on failure."""
    import http.cookiejar
    import urllib.request
    import urllib.error

    origin = f"https://{domain}" if use_public and domain else f"http://{forward_ip}:{port}"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def _call(method, path, body):
        req = urllib.request.Request(
            f"{origin}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:200]

    deadline = time.time() + timeout
    while time.time() < deadline:
        st, body = _call("POST", "/rest/login",
                         {"emailOrLdapLoginId": email, "password": current_password})
        if st == 200 and '"data"' in body:
            break
        time.sleep(5)
    else:
        raise ProvisionError(f"n8n login failed before password change: {body[:120]}")

    st, body = _call("PATCH", "/rest/me/password",
                     {"currentPassword": current_password, "newPassword": new_password})
    if st != 200 or '"success":true' not in body:
        raise ProvisionError(f"n8n password change failed (HTTP {st}): {body[:160]}")

    # verify the new password actually logs in
    st, body = _call("POST", "/rest/login",
                     {"emailOrLdapLoginId": email, "password": new_password})
    if st != 200 or '"data"' not in body:
        raise ProvisionError("New password verified false after change.")


def verify_n8n_login(forward_ip: str, port: int, email: str, password: str,
                     timeout: int = 60) -> bool:
    """Confirm the owner credentials actually authenticate (POST /rest/login).
    Only a JSON response with 'data' counts; boot-time SPA 200s do not."""
    import base64
    import urllib.request
    import urllib.error

    origin = f"http://{forward_ip}:{port}"
    b64 = base64.b64encode(f"{email}:{password}".encode()).decode()
    payload = json.dumps({"emailOrLdapLoginId": email, "password": password}).encode()
    req = urllib.request.Request(f"{origin}/rest/login", data=payload, method="POST",
        headers={"Authorization": f"Basic {b64}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status == 200 and '"data"' in body
    except Exception:
        return False


# ---------- orchestration ----------

def validate_password_policy(password: str) -> None:
    """n8n's owner-setup password policy (verified live 2026-09-01):
    min 8 chars, >=1 uppercase, >=1 lowercase, >=1 digit."""
    if len(password) < 8:
        raise ProvisionError("Password must be at least 8 characters.")
    if not any(c.isupper() for c in password):
        raise ProvisionError("Password must contain at least one uppercase letter.")
    if not any(c.islower() for c in password):
        raise ProvisionError("Password must contain at least one lowercase letter.")
    if not any(c.isdigit() for c in password):
        raise ProvisionError("Password must contain at least one digit.")


def provision_account(account_id: int, password: str | None = None) -> dict:
    account = db.get_account(account_id)
    if not account:
        raise ProvisionError("Account not found.")
    # Quota gate (Steward 2026-09-01): one instance per account by default; the
    # admin can raise the quota. count live instances vs the account's quota.
    quota = account["quota"] if "quota" in account.keys() else settings.default_quota
    live_count = db.count_instances(account_id)
    if live_count >= quota:
        raise ProvisionError(
            f"Instance quota reached ({live_count}/{quota}). "
            "Contact an administrator to increase your quota."
        )
    if account["status"] == "provisioned" and live_count > 0 and quota == 1:
        raise ProvisionError("Account already provisioned.")

    # Retry safety: clear stale instance rows from earlier failed attempts so a
    # re-provision doesn't die on the stack_name UNIQUE constraint (verified
    # 2026-09-01 with gamma: first attempt failed, second hit
    # 'UNIQUE constraint failed: instances.stack_name').
    for old in db.list_instances(account_id):
        if old["status"] != "healthy":
            db.delete_instance(old["id"])

    email = account["email"]
    username = account["username"]
    # Multi-instance accounts (quota > 1): suffix subsequent workspaces
    # username-2, username-3 ... so stack_name/domain stay unique.
    if live_count > 0:
        username = f"{username}-{live_count + 1}"
    first_name = account["first_name"] or username
    last_name = account["last_name"] or "User"

    # The client-chosen password (passed in-memory from the API, never stored
    # plaintext). Fall back to a generated one only if not provided.
    basic_auth_password = password or db.new_password(length=16)
    if password:
        validate_password_policy(basic_auth_password)

    pc = PortainerClient()
    npm = NPMClient()

    # 1. environment + port
    env_id, env_name = resolve_landing_environment(pc)
    ep = pc.get_endpoint(env_id)
    forward_ip = ep.get("PublicURL") or ep.get("URL", "").replace("tcp://", "").split(":")[0]
    if not forward_ip:
        raise ProvisionError("Could not resolve forward IP for environment.")
    port = next_free_port(pc, npm, env_id, forward_ip)

    # 2. credentials / keys
    encryption_key = db.new_key(prefix="", nbytes=32)  # 64 hex chars, unique
    basic_auth_user = email

    # 3. persist instance row FIRST (so we can roll back + track)
    domain = f"{username}.{settings.base_domain}"
    instance_id = db.create_instance(
        account_id=account_id,
        stack_name=username,
        environment_id=env_id,
        environment_name=env_name,
        port=port,
        domain=domain,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        n8n_encryption_key=encryption_key,
    )

    stack_id: int | None = None
    npm_host_id: int | None = None
    cert_id: int | None = None
    try:
        # 4. create stack via Portainer
        env_vars = build_stack_env(
            username, port, encryption_key, basic_auth_user, basic_auth_password
        )
        stack = pc.create_standalone_stack_string(
            endpoint_id=env_id,
            name=username,
            compose_content=load_compose_template(),
            env_vars=env_vars,
        )
        stack_id = stack.get("Id")
        db.update_instance(instance_id, stack_id=stack_id)

        # 5. wait for the stack container to start (up to ~120s)
        _wait_for_stack(pc, env_id, username, timeout=150)

        # 6. NPM proxy host
        host = npm.create_proxy_host(domain, forward_ip, port)
        npm_host_id = host.get("id")
        db.update_instance(instance_id, npm_host_id=npm_host_id)

        # 7. Let's Encrypt cert via NPM, then attach to host
        cert = npm.request_certificate([domain], name=f"{username} ({domain})")
        cert_id = cert.get("id")
        db.update_instance(instance_id, certificate_id=cert_id)
        _wait_for_cert(npm, cert_id, timeout=120)
        _attach_cert_to_host(npm, npm_host_id, cert_id)

        # 7b. Auto-create the n8n OWNER account so the emailed credentials work
        # immediately (no first-run setup page); verify login actually works.
        create_n8n_owner(forward_ip, port, basic_auth_user, basic_auth_password,
                         first_name=first_name, last_name=last_name)
        if not verify_n8n_login(forward_ip, port, basic_auth_user, basic_auth_password):
            raise ProvisionError("Owner created but login verification failed.")

        # 8. mark healthy
        db.update_instance(instance_id, status="healthy")
        db.set_account_status(account_id, "provisioned")

        return {
            "instance_id": instance_id,
            "username": username,
            "domain": domain,
            "port": port,
            "environment": env_name,
            "environment_id": env_id,
            "stack_id": stack_id,
            "npm_host_id": npm_host_id,
            "certificate_id": cert_id,
            "basic_auth_user": basic_auth_user,
            "basic_auth_password": basic_auth_password,  # shown/emailed ONCE
            "encryption_key": encryption_key,            # kept for record; never re-shown
        }

    except Exception as e:
        db.mark_instance_failed(instance_id, str(e))
        _rollback_stack(pc, stack_id, env_id, username=username) if stack_id else None
        _rollback_npm(npm, npm_host_id, cert_id)
        raise ProvisionError(f"Provisioning failed and was rolled back: {e}") from e


def _wait_for_stack(pc: PortainerClient, env_id: int, name: str, timeout: int = 150) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        containers = pc.list_containers(env_id, all=True)
        for c in containers:
            cnames = [n.lstrip("/") for n in c.get("Names", [])]
            if any(name in n for n in cnames):
                state = c.get("State", "")
                if state in ("running", "restarting"):
                    return
        time.sleep(5)
    raise ProvisionError(f"Timed out waiting for stack '{name}' container to start.")


def _wait_for_cert(npm: NPMClient, cert_id: int, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cert = npm.get_certificate(cert_id)
        # NPM certs: status in meta, or expires_on set once issued
        meta = cert.get("meta") or {}
        if meta.get("status") == "issued" or cert.get("expires_on"):
            return
        if "error" in str(meta).lower():
            raise ProvisionError(f"Cert issuance failed: {meta}")
        time.sleep(5)
    raise ProvisionError("Timed out waiting for certificate issuance.")


def _attach_cert_to_host(npm: NPMClient, host_id: int, cert_id: int) -> None:
    hosts = {h["id"]: h for h in npm.list_proxy_hosts()}
    host = hosts.get(host_id)
    if not host:
        raise ProvisionError("Proxy host vanished before cert attach.")
    # Send a clean, minimal update — this NPM version rejects fields copied
    # straight from GET (id, created_on, owner_user_id, ...).
    npm.update_proxy_host(
        host_id,
        domain_names=host["domain_names"],
        forward_scheme=host.get("forward_scheme", "http"),
        forward_host=host["forward_host"],
        forward_port=host["forward_port"],
        access_list_id=host.get("access_list_id", 0),
        certificate_id=cert_id,
        ssl_forced=True,  # NPM ignores ssl_forced at create (no cert yet); force on attach
        caching_enabled=host.get("caching_enabled", True),
        block_exploits=host.get("block_exploits", True),
        advanced_config=host.get("advanced_config", ""),
        meta={"nginx_online": True, "nginx_err": None},
        allow_websocket_upgrade=host.get("allow_websocket_upgrade", True),
        http2_support=host.get("http2_support", False),
        hsts_enabled=host.get("hsts_enabled", False),
        hsts_subdomains=host.get("hsts_subdomains", False),
        locations=host.get("locations", []),
        enabled=host.get("enabled", True),
        trust_forwarded_proto=host.get("trust_forwarded_proto", False),
    )
