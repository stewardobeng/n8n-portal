# FastAPI app — API surface for the n8n self-service portal (backend v1).
# Routes:
#   POST /api/v1/accounts          — client self-service signup (email + optional username)
#   POST /api/v1/accounts/{id}/provision — trigger provisioning (async task)
#   GET  /api/v1/accounts/{id}     — account + instance status
#   POST /api/v1/instances/{id}/reset-password — regenerate basic-auth password
#   GET  /api/v1/environments      — list Portainer environments (for UI later)
# Admin (bearer JWT):
#   GET/PUT /api/v1/admin/settings — landing environment config
#   GET  /api/v1/admin/accounts    — list all accounts

import logging
import re
import time as _time
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (FastAPI, HTTPException, Depends, BackgroundTasks, Request, Header,
                     Body)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .config import settings
from . import db
from .security import _decode_subject as _sec_decode_subject


def _decode_subject_is_admin(authorization: str | None) -> bool:
    try:
        return _sec_decode_subject(authorization) == "admin"
    except Exception:
        return False
from .services import provisioner, billing, access_gate, admin_ops, backup_ops
from .services import account_security, passkeys, n8n_releases, python_runner, ai_sandbox, sms
from .services import update_queue
from .services import plan_specs
from .services import plan_switch
from .services import security_controls as sc
from .services.admin_ops import AdminOpsError
from .services.portainer_client import PortainerClient
from .services.npm_client import NPMClient
from .services.emailer import (send_welcome_credentials, send_reset_password,
                               send_access_token, send_access_approved, send_email,
                               EmailError)
from .security import (create_access_token, verify_admin,
                       verify_admin_password, verify_password, hash_password,
                       create_client_token, verify_client, create_mfa_token,
                       verify_mfa_token, verify_impersonation,
                       create_impersonation_token, authorize_owner_or_admin)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("n8n-portal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # seed default landing environment if unset
    if db.get_setting("landing_environments") is None:
        db.set_setting("landing_environments", "8")
    # AUTO-EXPIRY background task (Steward 2026-09-01): every 15 minutes, stop
    # instances whose subscription has expired. Renewal restarts them.
    expiry_task = asyncio.create_task(_expiry_loop())
    # WORKSPACE UPDATE QUEUE (0.1.85): one update at a time per server, a quiet gap
    # between them, admin batches ahead of customer requests. Runs in this process
    # so it survives a closed browser; queue state is in SQLite so it survives us.
    update_queue.start_worker()
    try:
        yield
    finally:
        expiry_task.cancel()


async def _expiry_loop(interval_seconds: int = 15 * 60) -> None:
    while True:
        try:
            await asyncio.to_thread(billing.sweep_expired)
        except Exception as e:
            log.warning("expiry sweep failed: %s", e)
        # Renewal reminders (0.1.76): with no Paystack plan there is no automatic
        # renewal, so this loop also sends the reminders that are due. It runs at
        # most once a day and only when RENEWAL_REMINDERS_ENABLED is true; the
        # service enforces both, so a disabled switch is inert here.
        try:
            from .services import renewal
            res = await asyncio.to_thread(renewal.maybe_send_reminders)
            if res.get("sent"):
                log.info("renewal reminders sent: %s", res)
        except Exception as e:
            log.warning("renewal reminders failed: %s", e)
        await asyncio.sleep(interval_seconds)


app = FastAPI(title="n8n Self-Service Portal", version="0.1.0", lifespan=lifespan)


# ---------- security headers middleware (Steward 2026-09-03) ----------
# No security headers were set before (proxy only sent Server/X-Served-By).
# The SPA loads external app.js/styles.css + Google Fonts, uses inline style
# attrs and data: (QR) images, and hits the same-origin /api — so the CSP below
# is scoped to allow those while blocking inline <script> and unknown origins.

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # HSTS only ever sent over HTTPS (nginx terminates TLS; the app sees HTTP).
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-XSS-Protection": "1; mode=block",
}


@app.middleware("http")
async def security_headers_and_ban(request: Request, call_next):
    """Set hardened response headers on every response and reject banned IPs."""
    # IP-ban gate (defense-in-depth on top of the per-route checks)
    try:
        if sc.is_banned(sc.client_ip(request)):
            from fastapi import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "Access temporarily blocked."})
    except Exception:
        pass
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# ---------- schemas ----------

class AccountCreate(BaseModel):
    email: EmailStr
    username: Optional[str] = Field(default=None, max_length=62)
    display_name: Optional[str] = ""
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)
    access_token: Optional[str] = Field(default=None, max_length=16)


class AccountOut(BaseModel):
    id: int
    email: str
    username: str
    display_name: str
    status: str
    quota: int = 1
    quota_a: int = 1   # per-plan workspace allowances (0.1.68)
    quota_b: int = 1
    subscription_status: str = "none"
    paid_until: Optional[int] = None
    paid_from: Optional[int] = None
    account_state: str = "active"  # active | suspended | archived
    backup_enabled: bool = False   # customer may self-service backup (admin-set)
    update_enabled: bool = False   # customer may self-update a workspace (admin-set, 0.1.88)
    plan: str = "a"                 # subscribed tier: a = GHS 300, b = GHS 500 (Plus)
    first_name: str = ""            # 0.1.74: the sidebar chip shows only this
    created_at: int
    provisioned_at: Optional[int] = None


class AdminSetAccountStateIn(BaseModel):
    # action: suspend | unsuspend | archive | restore
    action: str = Field(..., pattern="^(suspend|unsuspend|archive|restore)$")


class AdminAddUserIn(BaseModel):
    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)


class AdminAttachIn(BaseModel):
    environment_id: int
    stack_name: str = Field(..., min_length=1, max_length=64)
    port: int = Field(0, ge=0, le=65535)
    domain: str = ""


class AdminMarkPaidIn(BaseModel):
    paid_until: Optional[int] = None  # epoch seconds (expiry date); omit to auto-anchor on NPM created_on
    paid_from: Optional[int] = None  # epoch seconds (subscription start; backdating)
    plan: Optional[str] = None  # 'a' | 'b': which package was paid for (0.1.68)


class AdminExtendIn(BaseModel):
    years: int = Field(..., ge=1, le=10)  # free renewal: +N years from current expiry


class ProvisionOut(BaseModel):
    account_id: int
    instance_id: int
    status: str
    domain: str
    port: int
    environment: str
    environment_id: int
    basic_auth_user: str
    basic_auth_password: str


class ProvisionRequest(BaseModel):
    # password is only required when the caller chooses it; the admin quick-
    # provision path (and owner fallback) can leave it out (generated instead).
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)
    # Per-workspace identity for extra workspaces (Steward 2026-09-03): the
    # customer fills the same form used for the first workspace, with a unique
    # username that becomes the stack name + domain.
    username: Optional[str] = Field(default=None, max_length=62)
    owner_email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(default=None, max_length=50)
    last_name: Optional[str] = Field(default=None, max_length=50)
    # Which package this workspace is provisioned as (0.1.70, Steward):
    # 'a' GHS 300 | 'b' GHS 500 Plus. Omit = the account's subscription tier
    # (the customer self-service flow always omits this; provisioning follows
    # the paid package automatically). Admin-only: enforced in the route.
    plan: Optional[str] = Field(default=None, pattern="^[ab]$")


class InstanceStatusOut(BaseModel):
    id: int
    account_id: int
    stack_name: str
    stack_id: Optional[int]
    environment_id: int
    environment_name: str
    port: int
    domain: str
    status: str
    locked: int = 0
    managed: int = 1
    image: str = ""
    python_runner: int = 0
    ai_sandbox: int = 0   # 0.1.103: shared n8n AI sandbox attached to this workspace
    plan: str = "a"   # this workspace's package: a GHS 300 | b GHS 500 Plus (0.1.70)
    error: Optional[str]
    created_at: int


class AdminSettingsIn(BaseModel):
    # comma-separated env ids in fallback order, e.g. "8,4,9"; optional so a
    # caller can update ONLY the payments switch without touching placement.
    landing_environments: Optional[str] = None
    payments_open: Optional[bool] = None
    # Admin profile + SMS delivery (0.1.90, Steward 2026-10-07). Every field is optional
    # so one card can save without touching the others. sms_api_key is write-only: it goes
    # in and only a masked confirmation comes back.
    admin_email: Optional[str] = None
    admin_phone: Optional[str] = None
    sms_api_key: Optional[str] = None
    sms_sender_id: Optional[str] = None
    sms_otp_type: Optional[str] = None


class AdminLoginIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)


class AdminLoginOut(BaseModel):
    token: str
    expires_hours: int
    mfa: Optional[dict] = None  # present when admin 2FA is required


class AccessCheckIn(BaseModel):
    email: EmailStr


class AccessCheckOut(BaseModel):
    action: str  # login | requested | waiting | token | denied
    email: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class LoginOut(BaseModel):
    token: str
    account: AccountOut
    mfa: Optional[dict] = None  # present when 2FA is required: {challenge, methods}


class TokenVerifyIn(BaseModel):
    email: EmailStr
    token: str = Field(..., min_length=1, max_length=16)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(..., min_length=10, max_length=128)
    password: str = Field(..., min_length=8, max_length=128)


class Login2FAIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class MFAVerifyIn(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=6, max_length=10)
    # MFA challenge minted by /auth/login after the password was accepted. Binds
    # the code to the password-authenticated step so a code alone can never mint a
    # session token (mirrors the passkey-MFA / admin flows). Audit vuln-0015.
    challenge: str = Field(..., min_length=10, max_length=512)


class TOTPSetupIn(BaseModel):
    # code to verify (validating the secret against the authenticator)
    code: str = Field(..., min_length=6, max_length=10)


class OTPEnableIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=10)


class PasskeyVerifyIn(BaseModel):
    # The full WebAuthn credential JSON (registration or authentication response)
    # as produced by navigator.credentials.create()/get() and JSON-serialized.
    credential: dict = Field(...)


class AdminMFASendOTPIn(BaseModel):
    # Which channel to send the admin code on. Optional so the old call (no body) still
    # means "email", exactly as it did before 0.1.90.
    channel: Optional[str] = None
    challenge: Optional[str] = None      # required for SMS: proves the password step ran


class AdminMFAVerifyIn(BaseModel):
    challenge: str = Field(..., min_length=10, max_length=512)
    code: str = Field(..., min_length=6, max_length=10)


class MFAPasskeyStartIn(BaseModel):
    # The MFA challenge (proves the password was accepted) — passkey is the second
    # factor offered at the post-password step (Steward 2026-09-03).
    challenge: str = Field(..., min_length=10, max_length=512)


class MFAPasskeyVerifyIn(BaseModel):
    challenge: str = Field(..., min_length=10, max_length=512)
    credential: dict = Field(...)


class AccessRequestOut(BaseModel):
    id: int
    email: str
    status: str
    created_at: int
    token_sent_at: Optional[int] = None
    token_expires_at: Optional[int] = None


class CheckoutIn(BaseModel):
    plan: str = "a"  # a = GHS 300 tier, b = GHS 500 (Plus) tier


class CheckoutOut(BaseModel):
    gateway: str
    url: str
    reference: Optional[str] = None


class PlanOut(BaseModel):
    name: str
    currency: str
    amount_minor: int
    interval: str
    gateway: str


# ---------- helpers ----------

def _row_get(row, key, default=None):
    try:
        v = row[key]
        return v if v is not None else default
    except (KeyError, IndexError, TypeError):
        return default


def _account_to_out(a) -> AccountOut:
    dn = (a["display_name"] or "").strip()
    if not dn:
        # 0.1.72: legacy accounts signed up before display names were defaulted
        # still render the person's real name, not the email-derived username.
        dn = f"{_row_get(a, 'first_name', '') or ''} {_row_get(a, 'last_name', '') or ''}".strip()
    return AccountOut(
        id=a["id"], email=a["email"], username=a["username"],
        display_name=dn, status=a["status"],
        quota=_row_get(a, "quota", 1),
        quota_a=db.account_quota_for(a, "a"),
        quota_b=db.account_quota_for(a, "b"),
        subscription_status=_row_get(a, "subscription_status", "none"),
        paid_until=_row_get(a, "paid_until"),
        paid_from=_row_get(a, "paid_from"),
        account_state=_row_get(a, "account_state", "active"),
        backup_enabled=bool(_row_get(a, "backup_enabled", 0)),
        update_enabled=bool(_row_get(a, "update_enabled", 0)),
        plan=_row_get(a, "plan", "a") or "a",
        first_name=(_row_get(a, "first_name", "") or "").strip(),
        created_at=a["created_at"], provisioned_at=a["provisioned_at"],
    )


def _instance_to_out(i) -> InstanceStatusOut:
    return InstanceStatusOut(
        id=i["id"], account_id=i["account_id"], stack_name=i["stack_name"],
        stack_id=i["stack_id"], environment_id=i["environment_id"],
        environment_name=i["environment_name"], port=i["port"], domain=i["domain"],
        status=i["status"], locked=_row_get(i, "locked", 0), error=i["error"],
        managed=_row_get(i, "managed", 1),
        image=_row_get(i, "image", ""),
        python_runner=_row_get(i, "python_runner", 0),
        ai_sandbox=_row_get(i, "ai_sandbox", 0),
        plan=_row_get(i, "plan", "a") or "a",
        created_at=i["created_at"],
    )


def _backup_to_out(b):
    import datetime as _dt
    return {
        "id": b["id"],
        "account_id": b["account_id"],
        "instance_id": b["instance_id"],
        "kind": b["kind"],
        "filename": b["filename"],
        "size_bytes": b["size_bytes"],
        "status": b["status"],
        "error": b["error"],
        "created_at": b["created_at"],
        "created_iso": _dt.datetime.utcfromtimestamp(b["created_at"]).isoformat() + "Z",
    }


def _get_owned_instance(account_id: int, instance_id: int):
    """Fetch an instance row and ensure it belongs to the signed-in account."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    if inst["account_id"] != account_id:
        raise HTTPException(403, "Not your instance.")
    return inst


def _require_backup(account_id: int) -> None:
    """Backup is admin-gated (2026-09-03): a customer can only self-service
    backups when the admin enabled it for their account; otherwise the backup UI
    is hidden AND the API refuses (defense in depth)."""
    acct = db.get_account(account_id)
    if acct and bool(_row_get(acct, "backup_enabled", 0)):
        return
    raise HTTPException(403, "Backup is not enabled for this account. Contact support.")


def _require_update(account_id: int) -> None:
    """Self-update is admin-gated exactly like backups (0.1.88, Steward 2026-10-07): a
    customer may only ask for a workspace update when the admin enabled it for their
    account; otherwise the update UI is hidden AND the API refuses (defense in depth)."""
    acct = db.get_account(account_id)
    if acct and bool(_row_get(acct, "update_enabled", 0)):
        return
    raise HTTPException(403, "Self-update is not enabled for this account. Contact support.")


# Restarting is safe and cheap, but a workspace restarted in a loop never gets to stay
# up. Customers are paced; the admin is not (Steward, 2026-10-08: the option is for both).
RESTART_COOLDOWN_SECONDS = 120


def _restart_instance(inst: dict, *, actor: str) -> dict:
    """Restart one workspace container in place, for the admin or its own customer.

    Not an update: the image is untouched, so this cannot change a version or roll
    anything back. The server refuses while an update is queued or running for the same
    workspace, because restarting in the middle of one would fight that update.
    """
    iid = int(inst["id"])
    try:
        conn = db.get_conn()
        try:
            busy = conn.execute(
                "SELECT 1 FROM update_queue WHERE instance_id = ? AND status IN ('queued','running')"
                " LIMIT 1", (iid,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the queue table is best effort here
        busy = None
    if busy:
        raise HTTPException(409, "This workspace is being updated right now. Restart it after that finishes.")

    key = f"restart_at:{iid}"
    if actor == "customer":
        try:
            last = int(db.get_setting(key) or 0)
        except (TypeError, ValueError):
            last = 0
        wait = RESTART_COOLDOWN_SECONDS - (int(_time.time()) - last)
        if last and wait > 0:
            raise HTTPException(429, "This workspace was restarted a moment ago. Try again in %d seconds." % wait)
    try:
        container = backup_ops.restart_workspace(inst)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "The workspace could not be restarted because " + str(e)[:180] + ".")
    db.set_setting(key, str(int(_time.time())))
    return {"ok": True, "instance_id": iid, "container": container[:12], "status": "restarting",
            "message": "Restarting now. It is usually back in about half a minute."}


def _run_backup(backup_id: int, instance: dict, kind: str) -> None:
    """Background task wrapper for a backup job (called via BackgroundTasks)."""
    try:
        if kind == "workflows":
            backup_ops.run_workflows_backup(backup_id, instance, kind="workflows")
        elif kind == "credentials":
            backup_ops.run_workflows_backup(backup_id, instance, kind="credentials")
        else:
            backup_ops.run_full_backup(backup_id, instance)
    except Exception as e:
        log.error("backup %s crashed: %s", backup_id, e)
        db.fail_backup(backup_id, str(e))


def _serve_backup(b):
    """Build a FileResponse for a ready backup (or 409/400 if not usable)."""
    if b["status"] != "ready":
        raise HTTPException(409, f"Backup is {b['status']}; try again once ready.")
    import os
    p = os.path.join(settings.backup_dir, b["filename"])
    # lookup the actual file: <backup_dir>/<stack>/<ts>/<filename> — we stored
    # only the basename; resolve via the instance's stack dir.
    inst = db.get_instance(b["instance_id"])
    if inst:
        # scan the per-instance folders for the newest matching file
        base = settings.backup_dir
        import glob
        matches = glob.glob(os.path.join(base, inst["stack_name"], "*", b["filename"]))
        if matches:
            p = max(matches, key=os.path.getmtime)
    if not os.path.exists(p):
        raise HTTPException(404, "Backup file no longer exists.")
    media = ("application/gzip" if b["filename"].endswith(".gz") else "application/json")
    return FileResponse(p, media_type=media, filename=b["filename"])


def _run_provision(account_id: int, password: str | None = None,
                   workspace: dict | None = None,
                   plan: str | None = None) -> None:
    """Background task wrapper — provisioning runs async so the API returns fast."""
    try:
        result = provisioner.provision_account(account_id, password=password,
                                               workspace=workspace, plan=plan)
        try:
            send_welcome_credentials(
                to=result["basic_auth_user"],
                username=result["username"],
                domain=result["domain"],
                port=result["port"],
                basic_auth_user=result["basic_auth_user"],
                basic_auth_password=result["basic_auth_password"],
            )
        except EmailError as e:
            log.warning(f"Welcome email failed for account {account_id}: {e}")
    except Exception as e:
        log.error(f"Provision failed for account {account_id}: {e}")


# ---------- access gate + login (admin-gated onboarding) ----------

@app.post("/api/v1/auth/check", response_model=AccessCheckOut)
def access_check(payload: AccessCheckIn, request: Request):
    """Email-only first page. Branch: login | requested | waiting | token."""
    ip = sc.client_ip(request)
    if sc.check_rate(ip, "check"):
        raise HTTPException(429, "Too many requests. Slow down and try again shortly.")
    return AccessCheckOut(**access_gate.check_email(str(payload.email)))


@app.post("/api/v1/auth/login", response_model=LoginOut)
def client_login(payload: LoginIn, request: Request):
    """Returning-user login: email + password -> portal session token.
    Suspended/archived accounts are blocked (admin lifecycle, 2026-09-02).
    If the account has 2FA enabled, password is checked and an MFA challenge is
    returned (no token yet) — the client must pass the second factor (2026-09-02).
    SECURITY (2026-09-03): per-IP rate limited + failed attempts recorded and
    auto-banned; login errors are uniform (no account-existence oracle)."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many login attempts. Try again in a minute.")
    email = str(payload.email).lower()
    account = db.get_account_by_email(email)
    # Uniform error for unknown account AND wrong password (no enumeration).
    if not account or not verify_password(payload.password, account["password_hash"] or ""):
        sc.record_failed(ip, "login_fail", account["id"] if account else None,
                         "bad credentials")
        raise HTTPException(401, "Invalid email or password.")
    if _row_get(account, "account_state", "active") != "active":
        sc.record_failed(ip, "login_fail", account["id"], "non-active account")
        raise HTTPException(
            403,
            "This account is " + _row_get(account, "account_state", "active") +
            ". Contact support for help.",
        )
    sc.record_ok(ip, "login_ok", account["id"])
    # 2FA gate: authenticator TOTP +/or email OTP
    meth = account_security.enabled_methods(account["id"])
    if meth:
        # mint a short-lived MFA challenge token (sub acc:<id> mfa) holding which
        # methods are allowed; the final token is issued after the code passes.
        challenge = create_mfa_token(account["id"], [m["method"] for m in meth])
        return LoginOut(
            token="__mfa__",
            account=_account_to_out(account),
            mfa={"challenge": challenge, "methods": meth},
        )
    return LoginOut(
        token=create_client_token(account["id"]),
        account=_account_to_out(account),
    )


@app.post("/api/v1/auth/mfa-verify", response_model=LoginOut)
def client_mfa_verify(payload: MFAVerifyIn, request: Request):
    """Second factor for a 2FA account: TOTP code from the authenticator app OR
    the emailed one-time code. Returns the real portal session token.

    AUDIT FIX (2026-09-03, vuln-0015): the MFA challenge issued by /auth/login
    after a correct password was NOT verified here, so a single valid code could
    mint a session token without the password. Now require + verify the challenge
    (proves the password step), bind it to this account, and only accept a code
    kind (totp/email) the challenge allows — mirroring the passkey-MFA / admin
    flows. A code alone can never mint a token."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "mfa"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    # Bind to the password step: the challenge (sub acc:<id>, claim mfa) must be
    # valid and match this account.
    try:
        challenge_account_id, challenge_methods = verify_mfa_token(payload.challenge)
    except HTTPException:
        sc.record_failed(ip, "mfa_fail", None, "invalid/expired challenge")
        raise HTTPException(401, "Invalid or expired sign-in challenge. Sign in again.")
    email = str(payload.email).lower()
    account = db.get_account_by_email(email)
    if not account:
        raise HTTPException(401, "Invalid email or password.")
    if challenge_account_id != account["id"]:
        sc.record_failed(ip, "mfa_fail", account["id"], "challenge/account mismatch")
        raise HTTPException(401, "Invalid or expired sign-in challenge. Sign in again.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is " +
                            _row_get(account, "account_state", "active") +
                            ". Contact support for help.")
    code = payload.code.strip()
    methods = account_security.enabled_methods(account["id"])
    if not methods:
        raise HTTPException(409, "This account does not require a second factor.")
    # Only a code kind the challenge allowed (totp/email) may be used.
    allowed = [m for m in methods if m["method"] in challenge_methods]
    if not any(m["method"] in ("totp", "email") for m in allowed):
        sc.record_failed(ip, "mfa_fail", account["id"], "method not in challenge")
        raise HTTPException(409, "This account does not allow a code as the second factor.")
    ok = False
    if any(m["method"] == "totp" for m in allowed):
        ok = account_security.totp_verify_code(account["id"], code)
    if not ok and any(m["method"] == "email" for m in allowed):
        ok = account_security.email_otp_verify(account["id"], code)
    if not ok:
        sc.record_failed(ip, "mfa_fail", account["id"])
        raise HTTPException(401, "That code is invalid or has expired.")
    sc.record_ok(ip, "login_ok", account["id"])
    return LoginOut(
        token=create_client_token(account["id"]),
        account=_account_to_out(account),
    )


class MFASendOTPIn(BaseModel):
    email: EmailStr
    # MFA challenge proves the password step (vuln-0015): prevent mailbox spam /
    # guessing by someone who has not completed the password step.
    challenge: str = Field(..., min_length=10, max_length=512)


@app.post("/api/v1/auth/mfa-send-otp", response_model=dict)
def client_mfa_send_otp(payload: MFASendOTPIn, request: Request):
    """Resend the emailed one-time code for a 2FA account (after the password
    step). SECURITY (2026-09-03): per-IP limited to avoid email-bombing a mailbox.
    AUDIT FIX (2026-09-03, vuln-0015): require the MFA challenge so an emailed
    code can only be requested once the password step is proven."""
    ip = sc.client_ip(request)
    if sc.check_rate(ip, "forgot"):
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    # The password step must have been completed (challenge valid + matches).
    try:
        challenge_account_id, _ = verify_mfa_token(payload.challenge)
    except HTTPException:
        raise HTTPException(401, "Invalid or expired sign-in challenge. Sign in again.")
    account = db.get_account_by_email(str(payload.email).lower())
    if not account:
        raise HTTPException(401, "Invalid email or password.")
    if challenge_account_id != account["id"]:
        raise HTTPException(401, "Invalid or expired sign-in challenge. Sign in again.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is " +
                            _row_get(account, "account_state", "active") +
                            ". Contact support for help.")
    if not _row_get(account, "email_2fa", 0):
        raise HTTPException(409, "Email 2FA is not enabled for this account.")
    return account_security.email_otp_send(account["id"])


@app.post("/api/v1/auth/passkey/mfa/start", response_model=dict)
def client_passkey_mfa_start(payload: MFAPasskeyStartIn, request: Request):
    """Passkey as a SECOND factor, after the password was accepted. The MFA
    challenge (a short-lived JWT proving the password) is required so a passkey
    alone can never mint a token — the password must come first.
    (Steward 2026-09-03: passkey is a 2FA option alongside authenticator/email.)"""
    ip = sc.client_ip(request)
    if sc.is_banned(ip) or sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    account_id, methods = verify_mfa_token(payload.challenge)
    if "passkey" not in methods:
        raise HTTPException(403, "This account does not allow a passkey as the second factor.")
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(401, "Account not found.")
    try:
        return passkeys.authentication_options("account", account_id)
    except passkeys.PasskeyError:
        raise HTTPException(401, "No passkey registered for this account.")


@app.post("/api/v1/auth/passkey/mfa/verify", response_model=LoginOut)
def client_passkey_mfa_verify(payload: MFAPasskeyVerifyIn, request: Request):
    """Finishes the second factor with a passkey. Requires the MFA challenge
    (password already proven) + a valid WebAuthn assertion, then issues the token."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "mfa"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    account_id, methods = verify_mfa_token(payload.challenge)
    if "passkey" not in methods:
        raise HTTPException(403, "This account does not allow a passkey as the second factor.")
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(401, "Account not found.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is not active. Contact support.")
    try:
        passkeys.verify_authentication("account", account_id, payload.credential)
    except passkeys.PasskeyError as e:
        sc.record_failed(ip, "mfa_fail", account_id)
        raise HTTPException(401, str(e))
    sc.record_ok(ip, "login_ok", account_id)
    return LoginOut(token=create_client_token(account_id), account=_account_to_out(account))


@app.post("/api/v1/auth/forgot-password", response_model=dict)
def forgot_password(payload: ForgotPasswordIn, request: Request):
    """Email a single-use reset link. Always returns 200 so attackers cannot
    enumerate which emails have accounts. Mail failures are logged, not surfaced
    (the response must not reveal whether an account or SMTP problem occurred).
    SECURITY (2026-09-03): per-IP limited to avoid email-bombing a mailbox."""
    ip = sc.client_ip(request)
    if sc.check_rate(ip, "forgot"):
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    try:
        token = account_security.request_password_reset(str(payload.email))
    except EmailError as e:
        log.error("password reset mail failed for %s: %s", str(payload.email), e)
        token = None
    if token:
        log.info("password reset issued for %s", str(payload.email))
    else:
        log.info("password reset requested for unknown/non-active %s", str(payload.email))
    return {"sent": True}


@app.post("/api/v1/auth/reset-password", response_model=dict)
def reset_password(payload: ResetPasswordIn, request: Request):
    """Consume a reset token and set a new portal password.
    SECURITY (2026-09-03): per-IP limited to blunt token-guessing."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip) or sc.check_rate(ip, "global"):
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    try:
        account_security.reset_password(payload.token, payload.password)
    except account_security.SecurityError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/v1/auth/verify-token", response_model=AccessCheckOut)
def verify_access_token(payload: TokenVerifyIn, request: Request):
    """Visitor enters email + admin-issued token; verified -> registration opens.
    SECURITY (2026-09-03): the XXXX-XXXX access code is the signup gate, so
    per-IP + per-email limiting is essential to stop brute-forcing codes."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "verify"):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not access_gate.verify_token(str(payload.email), payload.token):
        sc.record_failed(ip, "verify_fail", None, "bad access token")
        raise HTTPException(401, "Invalid or expired access token.")
    return AccessCheckOut(action="verified", email=str(payload.email).lower())


@app.post("/api/v1/auth/impersonate-end", response_model=dict)
def impersonate_end(request: Request, account_id: int = Depends(verify_impersonation)):
    """Explicit end of an admin impersonation session (2026-09-03). Audits the
    exit on the security trail; the token also self-expires after
    IMPERSONATION_TTL_MINUTES, so an exit is best-effort bookkeeping only."""
    db.record_auth_event(sc.client_ip(request), "impersonate_end", account_id,
                         detail="impersonation ended")
    return {"ended": True, "account_id": account_id}


@app.get("/api/v1/me", dependencies=[Depends(verify_client)])
def me(account_id: int = Depends(verify_client)):
    """Current signed-in account + instances (portal dashboard).
    Suspended/archived accounts are cut off even with a valid session token."""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(
            403,
            "This account is " + _row_get(account, "account_state", "active") +
            ". Contact support for help.",
        )
    instances = db.list_instances(account_id)
    current_plan = (_row_get(account, "plan", "a") or "a")
    return {
        "account": _account_to_out(account).model_dump(),
        "instances": [_instance_to_out(i).model_dump() for i in instances],
        # 0.1.99: what the dashboard needs to show the package and the upgrade button state.
        # A request is an ASK (the admin contacts them), not a checkout: upgrading by card is
        # not built yet, and claiming otherwise on screen would be a lie.
        "plan": {
            "current": current_plan,
            "label": plan_switch.plan_label(current_plan),
            "can_request_upgrade": current_plan != "b",
            "target_plan": plan_switch.other_plan(current_plan),
            "target_label": plan_switch.plan_label(plan_switch.other_plan(current_plan)),
        },
        "upgrade_request": db.open_upgrade_request(account_id),
    }


@app.get("/api/v1/me/security", dependencies=[Depends(verify_client)])
def my_security(account_id: int = Depends(verify_client)):
    """Current 2FA setup state for the signed-in account (customer / admin)."""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    return {
        "methods": account_security.enabled_methods(account_id),
        "totp_enabled": bool(_row_get(account, "totp_enabled", 0)),
        "email_2fa": bool(_row_get(account, "email_2fa", 0)),
        "has_totp_secret": bool(db._get_totp_secret(account_id)),
        "passkeys": passkeys.list_passkeys_for("account", account_id),
        "passkey_enabled": bool(passkeys.list_passkeys_for("account", account_id)),
    }


@app.post("/api/v1/me/security/totp/setup", dependencies=[Depends(verify_client)])
def my_totp_setup(account_id: int = Depends(verify_client)):
    """Start authenticator-app 2FA: returns the otpauth URI + secret for a QR."""
    return account_security.totp_setup(account_id)


@app.post("/api/v1/me/security/totp/enable", dependencies=[Depends(verify_client)])
def my_totp_enable(payload: TOTPSetupIn, account_id: int = Depends(verify_client)):
    """Verify the code from the authenticator app to enable TOTP 2FA."""
    if not account_security.totp_verify_and_enable(account_id, payload.code.strip()):
        raise HTTPException(400, "That authenticator code is invalid. Try again.")
    return {"ok": True}


@app.post("/api/v1/me/security/totp/disable", dependencies=[Depends(verify_client)])
def my_totp_disable(account_id: int = Depends(verify_client)):
    db.set_account_totp_enabled(account_id, False)
    db.set_account_totp_secret(account_id, "")
    return {"ok": True}


@app.post("/api/v1/me/security/email/send", dependencies=[Depends(verify_client)])
def my_email_otp_send(account_id: int = Depends(verify_client)):
    """Send a 6-digit code to the account email to confirm email 2FA setup."""
    return account_security.email_otp_send(account_id)


@app.post("/api/v1/me/security/email/enable", dependencies=[Depends(verify_client)])
def my_email_enable(payload: OTPEnableIn, account_id: int = Depends(verify_client)):
    """Verify the emailed code to enable email 2FA."""
    if not account_security.email_otp_verify(account_id, payload.code.strip()):
        raise HTTPException(400, "That code is invalid or has expired.")
    db.set_account_email_2fa(account_id, True)
    return {"ok": True}


@app.post("/api/v1/me/security/email/disable", dependencies=[Depends(verify_client)])
def my_email_disable(account_id: int = Depends(verify_client)):
    db.set_account_email_2fa(account_id, False)
    return {"ok": True}


# ---------- client passkeys (WebAuthn, Steward 2026-09-03) ----------

@app.post("/api/v1/me/security/passkey/register", dependencies=[Depends(verify_client)])
def my_passkey_register_start(account_id: int = Depends(verify_client)):
    """PublicKey create options for a new passkey. The challenge is stored; the
    browser runs navigator.credentials.create() then POSTs the response to /verify."""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    try:
        return passkeys.registration_options("account", account_id, account["email"])
    except Exception as e:
        raise HTTPException(400, f"Could not start passkey setup: {e}")


@app.post("/api/v1/me/security/passkey/verify", dependencies=[Depends(verify_client)])
def my_passkey_register_verify(payload: PasskeyVerifyIn, account_id: int = Depends(verify_client)):
    """Verify the navigator.credentials.create() response and store the credential."""
    try:
        return passkeys.verify_registration("account", account_id, payload.credential)
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/me/security/passkeys", dependencies=[Depends(verify_client)])
def my_passkey_list(account_id: int = Depends(verify_client)):
    return {"passkeys": passkeys.list_passkeys_for("account", account_id)}


@app.delete("/api/v1/me/security/passkeys/{credential_id}", dependencies=[Depends(verify_client)])
def my_passkey_delete(credential_id: str, account_id: int = Depends(verify_client)):
    if not passkeys.delete_passkey_for("account", account_id, credential_id):
        raise HTTPException(404, "Passkey not found.")
    return {"ok": True}


# ---------- passkey sign-in (WebAuthn, first-factor) ----------

@app.post("/api/v1/auth/passkey/login/start")
def client_passkey_login_start(payload: AccessCheckIn, request: Request):
    """PublicKey get options for passwordless sign-in. Returns the credential-
    allowlist options so the browser can run navigator.credentials.get()."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip) or sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.")
    account = db.get_account_by_email(str(payload.email).lower())
    if not account:
        # Do not reveal whether the account exists; ask them to use the email flow.
        raise HTTPException(401, "No passkey available for that email.")
    if db._row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is not active. Contact support.")
    try:
        return passkeys.authentication_options("account", account["id"])
    except passkeys.PasskeyError:
        raise HTTPException(401, "No passkey registered for that email.")


@app.post("/api/v1/auth/passkey/login/verify")
def client_passkey_login_verify(payload: PasskeyVerifyIn, request: Request):
    """Verify the navigator.credentials.get() assertion and issue a session token."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    try:
        res = passkeys.verify_authentication("account", None, payload.credential)
    except passkeys.PasskeyError as e:
        sc.record_failed(ip, "login_fail", None, f"passkey verify: {e}")
        raise HTTPException(401, str(e))
    account_id = res.get("account_id")
    account = db.get_account(account_id) if account_id else None
    if not account:
        raise HTTPException(401, "Account not found.")
    if db._row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is not active. Contact support.")
    # Passkey is the primary factor here; if the account also has 2FA enabled, the
    # standard MFA gate still applies (passkey alone does not issue the token).
    meth = account_security.enabled_methods(account_id)
    if meth:
        challenge = create_mfa_token(account_id, [m["method"] for m in meth])
        sc.record_ok(ip, "login_ok", account_id)
        return LoginOut(token="__mfa__", account=_account_to_out(account),
                        mfa={"challenge": challenge, "methods": meth})
    sc.record_ok(ip, "login_ok", account_id)
    return LoginOut(token=create_client_token(account_id), account=_account_to_out(account))


# ---------- client backups (Steward 2026-09-03) ----------

@app.get("/api/v1/me/backups", dependencies=[Depends(verify_client)])
def my_backups(account_id: int = Depends(verify_client)):
    """List the signed-in account's workspace backups (newest first)."""
    _require_backup(account_id)
    rows = db.list_backups(account_id)
    return {"backups": [_backup_to_out(r) for r in rows]}


@app.post("/api/v1/me/instances/{instance_id}/backup",
          dependencies=[Depends(verify_client)], status_code=201)
def my_create_backup(instance_id: int, kind: str = "full",
                     background: BackgroundTasks = None,
                     account_id: int = Depends(verify_client)):
    """Trigger a backup of one of the signed-in account's instances.
    kind = 'full' (entire ~/.n8n dir incl. db) | 'workflows' | 'credentials'."""
    _require_backup(account_id)
    inst = _get_owned_instance(account_id, instance_id)
    if kind not in ("full", "workflows", "credentials"):
        raise HTTPException(400, "kind must be 'full', 'workflows' or 'credentials'.")
    filename = {"full": "n8n-data.tar.gz", "workflows": "workflows.json",
                "credentials": "credentials.json"}[kind]
    bid = db.create_backup(account_id, instance_id, kind, filename)
    if background:
        background.add_task(_run_backup, bid, dict(inst), kind)
    return {"id": bid, "status": "creating"}


@app.get("/api/v1/me/updates", dependencies=[Depends(verify_client)])
def my_updates(account_id: int = Depends(verify_client)):
    """Version and self-update state for the signed-in account's own workspaces.

    `can_update` is true only on the self-service package and only when the
    workspace is genuinely behind, so the button never promises a no-op.
    """
    return {"latest_version": update_queue.latest_target()["version"],
            "update_allowed": update_queue.account_update_allowed(account_id),
            "items": update_queue.customer_state(account_id)}


@app.post("/api/v1/me/instances/{instance_id}/update", status_code=202,
          dependencies=[Depends(verify_client)])
def my_request_update(instance_id: int, account_id: int = Depends(verify_client)):
    """Ask for a self-update. ALWAYS queued: even if ten people click together, the
    worker updates one workspace per server with a quiet gap between them, and this
    response tells the customer where they are in their server's line."""
    _require_update(account_id)
    inst = _get_owned_instance(account_id, instance_id)
    info = {i["instance_id"]: i for i in update_queue.customer_state(account_id)}.get(int(inst["id"]))
    if not info:
        raise HTTPException(404, "Workspace not found.")
    if info.get("queued"):
        raise HTTPException(409, "This workspace is already in the update line.")
    if not info.get("can_update"):
        raise HTTPException(403, info.get("blocked_reason") or "This workspace cannot self-update.")
    res = update_queue.queue_workspaces([int(inst["id"])], requested_by="customer",
                                       requested_account=account_id, priority=100)
    if not res.get("ok"):
        why = res.get("error") or (res.get("skipped") or [{}])[0].get("reason") or "Could not queue the update."
        raise HTTPException(503, why)
    row = res["queued"][0]
    pos = update_queue.position_of(row["queue_id"])
    return {"ok": True, "queue_id": row["queue_id"], "target_version": row["target_version"],
            "position": pos.get("position"), "eta_minutes": pos.get("eta_minutes"),
            "status": "queued"}


@app.post("/api/v1/me/instances/{instance_id}/restart", status_code=202,
          dependencies=[Depends(verify_client)])
def my_restart_instance(instance_id: int, account_id: int = Depends(verify_client)):
    """Restart my own workspace: same version, same data, back in about half a minute.

    Available to every customer on their own workspace (Steward, 2026-10-08). Paced, so
    it cannot be used to keep a workspace down, and refused mid-update.
    """
    inst = _get_owned_instance(account_id, instance_id)
    return _restart_instance(inst, actor="customer")


@app.get("/api/v1/me/backups/{backup_id}/download", dependencies=[Depends(verify_client)])
def my_download_backup(backup_id: int, account_id: int = Depends(verify_client)):
    """Download a backup file for the signed-in account's own instance."""
    _require_backup(account_id)
    b = db.get_backup(backup_id)
    if not b:
        raise HTTPException(404, "Backup not found.")
    if b["account_id"] != account_id:
        raise HTTPException(403, "Not your backup.")
    return _serve_backup(b)


# ---------- client endpoints ----------

@app.post("/api/v1/accounts", response_model=dict, status_code=201)
def create_account(payload: AccountCreate, request: Request):
    """Registration is GATED: the email must carry an admin-approved access request
    (Steward 2026-09-01). Returns the account + a portal session token.
    SECURITY (2026-09-03): per-IP rate limited (access-token guessing / spam)."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "register"):
        raise HTTPException(429, "Too many registrations. Try again later.")
    email = str(payload.email).lower()
    existing = db.get_account_by_email(email)
    if existing:
        raise HTTPException(409, "An account with this email already exists.")
    # gate (0.1.77, Steward): an ADMIN APPROVAL is the gate, not a code. The email
    # must carry an approved access request; nothing is generated, emailed or
    # typed. 'token_sent' rows predate this change and were already approved, so
    # they register exactly the same way. payload.access_token is still accepted
    # (older clients send it) and deliberately ignored.
    req = db.get_access_request(email)
    if not req or req["status"] not in ("approved", "token_sent"):
        raise HTTPException(403, "Access required. Request access from an administrator first.")
    username = provisioner.ensure_unique_username(payload.username, email)
    try:
        provisioner.validate_password_policy(payload.password)
    except provisioner.ProvisionError as e:
        raise HTTPException(422, str(e))
    # Display name (0.1.72, Steward): the person's REAL name must be the
    # account's main name - never the email-derived username. Default it from
    # the signup name fields when the form did not send one.
    display_name = (payload.display_name or "").strip() or \
        f"{payload.first_name} {payload.last_name}".strip()
    account_id = db.create_account(
        email, username, display_name,
        first_name=payload.first_name, last_name=payload.last_name,
        password_hash=hash_password(payload.password),
    )
    access_gate.consume(email)
    return {
        "account": _account_to_out(db.get_account(account_id)).model_dump(),
        "token": create_client_token(account_id),
    }


@app.post("/api/v1/accounts/{account_id}/provision")
def provision(account_id: int, background: BackgroundTasks,
              authorization: str | None = Header(default=None, alias="Authorization"),
              payload: Optional[ProvisionRequest] = None):
    """SECURITY FIX (2026-09-03): require owner-or-admin — previously any caller
    could trigger provisioning on an arbitrary account id."""
    authorize_owner_or_admin(authorization, account_id)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    # Per-workspace identity (extra workspace form): validate BEFORE the quota
    # gate so a bad username/password never starts a job. The username must be
    # globally unique (accounts + portal stacks); it becomes the stack + domain.
    workspace = None
    if payload and (payload.username or payload.owner_email
                    or payload.first_name or payload.last_name):
        workspace = {}
        if payload.username:
            try:
                workspace["username"] = provisioner.validate_username(
                    payload.username,
                    allow_account_id=account["id"] if db.count_instances(account["id"]) == 0 else None)
            except provisioner.ProvisionError as e:
                raise HTTPException(422, str(e))
        if payload.owner_email:
            workspace["owner_email"] = str(payload.owner_email).lower()
        if payload.first_name and payload.first_name.strip():
            workspace["first_name"] = payload.first_name.strip()
        if payload.last_name and payload.last_name.strip():
            workspace["last_name"] = payload.last_name.strip()
        if payload.password:
            try:
                provisioner.validate_password_policy(payload.password)
            except provisioner.ProvisionError as e:
                raise HTTPException(422, str(e))
    # Quota gate, PER PACKAGE (0.1.73): the fast 409 must count the workspace
    # against the package it will actually be provisioned as. Reading the legacy
    # single quota here blocked an account that holds BOTH packages from using
    # its second allowance (the provisioner's own gate is per-package, so the
    # route was the stricter, wrong one).
    target_plan = "a"
    if payload and payload.plan in ("a", "b"):
        target_plan = payload.plan            # admin package choice (checked below)
    elif "plan" in account.keys() and (account["plan"] or "a") == "b":
        target_plan = "b"
    quota = db.account_quota_for(account, target_plan)
    live = db.count_instances_by_plan(account_id, target_plan)
    if live >= quota:
        package = "Plus" if target_plan == "b" else "standard"
        raise HTTPException(409, f"Workspace quota reached on the {package} package ({live}/{quota}). Contact an administrator to increase your allowance.")
    # PAYMENT GATE: instances are only created after the annual subscription is
    # active (webhook sets subscription_status=active on charge.success).
    if billing.gateway() in ("paystack", "stripe") and account["subscription_status"] != "active":
        raise HTTPException(402, "Payment required: subscribe before provisioning.")
    # ADMIN PACKAGE CHOICE (0.1.70, Steward): an admin may pick which package
    # the workspace is provisioned as. Choosing a DIFFERENT package than the
    # account's subscription moves the subscription to that package (the two
    # must never disagree). Customers cannot pass this field.
    plan_override = None
    if payload and payload.plan:
        _is_admin = _decode_subject_is_admin(authorization)
        if not _is_admin:
            raise HTTPException(403, "Only an administrator can choose the package.")
        current = (account["plan"] or "a") if "plan" in account.keys() else "a"
        if payload.plan != current:
            db.set_account_plan(account_id, payload.plan)
            log.info("Account %s subscription moved %s -> %s by admin (workspace provision)", account_id, current, payload.plan)
        plan_override = payload.plan
    password = payload.password if payload else None
    background.add_task(_run_provision, account_id, password, workspace, plan_override)
    return {"status": "provisioning_started", "account_id": account_id}


@app.get("/api/v1/accounts/{account_id}", response_model=dict)
def account_status(account_id: int,
                   authorization: str | None = Header(default=None, alias="Authorization")):
    """SECURITY FIX (2026-09-03): require owner-or-admin — previously any caller
    could enumerate any account's details + instances by id (IDOR)."""
    authorize_owner_or_admin(authorization, account_id)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    instances = db.list_instances(account_id)
    current_plan = (db._row_get(account, "plan", "a") or "a")
    pending = db.open_upgrade_request(account_id)
    return {
        "account": _account_to_out(account).model_dump(),
        "instances": [_instance_to_out(i).model_dump() for i in instances],
        # The customer sees which package they hold, whether a request is already open, and
        # what the next package is called. Payment for an upgrade is not built yet, so this is
        # an ask, not a checkout: the admin contacts them.
        "plan": {
            "current": current_plan,
            "label": plan_switch.plan_label(current_plan),
            "can_request_upgrade": current_plan != "b",
            "target_plan": plan_switch.other_plan(current_plan),
            "target_label": plan_switch.plan_label(plan_switch.other_plan(current_plan)),
        },
        "upgrade_request": pending,
    }


@app.get("/api/v1/username-available", dependencies=[Depends(verify_client)])
def check_username_available(username: str = "", account_id: int = Depends(verify_client)):
    """Live uniqueness check for a NEW workspace username (extra workspace
    form). A username reserved on the caller's own account row (registration
    placeholder) is reported available so it can be claimed at provisioning
    time (2026-09-05 onboarding flow)."""
    uname = (username or "").strip().lower()
    available, message = provisioner.username_available(uname)
    if not available:
        own = db.get_account(account_id)
        if own and db.count_instances(account_id) == 0 \
                and str(db._row_get(own, "username", "") or "").lower() == uname:
            return {"available": True, "username": uname,
                    "message": "That is your reserved workspace name. You can use it when you create your workspace."}
    return {"available": available, "username": uname, "message": message}


# ---------- billing ----------

@app.get("/api/v1/plan", response_model=PlanOut)
def plan_info():
    return PlanOut(
        name=settings.plan_name,
        currency=settings.plan_currency,
        amount_minor=settings.plan_amount_minor,
        interval="annually",
        gateway=billing.gateway(),
    )


@app.get("/api/v1/plans")
def plans():
    """Both annual packages (Steward 2026-09-01): GHS 300 and GHS 500 Plus.

    Each entry carries `key` ('a' | 'b') so the UI labels a package from the API
    rather than guessing off the price, plus `specs` (0.1.73) built by
    plan_specs from the ACTUAL stack template, so the billing page shows the
    resources a workspace really gets.
    """
    plans_out = [{
        "key": "a",
        "name": settings.plan_name,
        "currency": settings.plan_currency,
        "amount_minor": settings.plan_amount_minor,
        "interval": "annually",
        "active": True,
        "specs": plan_specs.specs_for_plan("a"),
    }]
    plans_out.append({
        "key": "b",
        "name": settings.plan_b_name,
        "currency": settings.plan_currency,
        "amount_minor": settings.plan_b_amount_minor,
        "interval": "annually",
        "active": settings.plan_b_active,
        "specs": plan_specs.specs_for_plan("b"),
    })
    return {"plans": plans_out, "gateway": billing.gateway(),
            "payments_open": billing.payments_open()}


@app.post("/api/v1/accounts/{account_id}/checkout", response_model=CheckoutOut)
def create_checkout(account_id: int,
                    authorization: Optional[str] = Header(default=None, alias="Authorization"),
                    payload: Optional[CheckoutIn] = None):
    """SECURITY FIX (2026-09-03): require owner-or-admin — previously any caller
    could start a checkout on an arbitrary account id."""
    authorize_owner_or_admin(authorization, account_id)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    if account["status"] == "provisioned" and account["subscription_status"] == "active":
        raise HTTPException(409, "Already subscribed and provisioned.")
    # Payments master switch (Steward 2026-09-03): while onboarding users who
    # must not pay, the admin holds payments and nobody can start a checkout.
    if not billing.payments_open():
        raise HTTPException(
            403,
            "Payments are currently on hold. Contact support to arrange a subscription.",
        )
    plan = (payload.plan if payload else "a")
    if plan not in ("a", "b"):
        raise HTTPException(400, "Unknown plan. Choose plan a or plan b.")
    if plan == "b" and not settings.plan_b_active:
        raise HTTPException(403, "That plan is not available yet.")
    db.set_account_plan(account_id, plan)
    try:
        return billing.create_checkout(account_id, account["email"], plan=plan)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/v1/webhook/paystack")
async def paystack_webhook(request: Request,
                           x_router_secret: Optional[str] = Header(default=None)):
    """Paystack webhook endpoint (normally delivered via our webhook router).
    Signature: x-paystack-signature (HMAC-SHA512). When PAYSTACK_ROUTER_SECRET
    is configured, the x-router-secret header must ALSO match, so only our
    router can deliver events even if the Paystack signature is replayed."""
    payload = await request.body()
    signature = request.headers.get("x-paystack-signature")
    expected_rs = settings.paystack_router_secret
    if expected_rs:
        from .security import _constant_time_eq
        if not x_router_secret or not _constant_time_eq(x_router_secret, expected_rs):
            log.warning("Rejected Paystack webhook without valid router secret")
            raise HTTPException(401, "Invalid router secret.")
    try:
        result = billing.handle_webhook("paystack", payload, signature)
    except Exception as e:
        raise HTTPException(400, str(e))
    return result


@app.post("/api/v1/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        result = billing.handle_webhook("stripe", payload, signature)
    except Exception as e:
        raise HTTPException(400, str(e))
    return result


@app.post("/api/v1/webhook/mock")
async def mock_webhook(request: Request,
                       authorization: str | None = Header(default=None, alias="Authorization")):
    """E2E helper: POST {"mock": true, "type": "charge.success",
    "data": {"metadata": {"account_id": "5"}}} etc. Only active when
    PAYMENT_GATEWAY=mock.

    SECURITY FIX (2026-09-03, Steward): previously open to anyone — with
    PAYMENT_GATEWAY=mock an arbitrary caller could mark ANY account paid and
    unlock its workspace (a free-provisioning / undo-lock bypass). Now the
    caller must authenticate: either the admin, or the client whose account_id
    matches the one being marked paid. The frontend's mock checkout sends the
    client token (api() attaches it), so the legit flow still works.

    SECOND SECURITY FIX (2026-09-03, audit vuln-0014): the gate was applied
    AFTER billing.handle_webhook() already mutated subscription/instance state,
    so a caller that failed authz still got the side effect (check-after-act
    TOCTOU); and events addressed only by customer.email/email skipped the gate
    entirely (returning 200 unauthenticated). The owner-or-admin gate now runs
    BEFORE any state change and is unconditional: the target account is resolved
    exactly as the mock handler resolves it (metadata.account_id, else
    customer.email / email), a known account is required, and the caller must be
    the owner or admin. Anonymous posts cannot change any state."""
    if settings.payment_gateway != "mock":
        raise HTTPException(404, "Mock gateway is not active.")
    payload = await request.body()
    # Owner-or-admin gate FIRST: billing.handle_webhook mutates subscription and
    # instance state, so the caller must be authenticated before any side effect.
    import json as _json
    try:
        ev = _json.loads(payload or b"{}")
    except Exception:
        raise HTTPException(400, "Invalid mock webhook JSON.")
    data = ev.get("data") or {}
    meta = data.get("metadata") or {}
    target = None
    if meta.get("account_id"):
        try:
            target = int(meta["account_id"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid account_id in mock webhook.")
    else:
        email = (data.get("customer") or {}).get("email") or data.get("email")
        if email:
            acc = db.get_account_by_email(str(email))
            target = acc["id"] if acc else None
    if target is None:
        raise HTTPException(400, "Mock webhook must reference a known account.")
    authorize_owner_or_admin(authorization, target)
    try:
        event = billing.handle_webhook("mock", payload, None)
    except Exception as e:
        raise HTTPException(400, str(e))
    return event


@app.get("/api/v1/environments", dependencies=[Depends(verify_admin)])
def environments():
    """Portainer environment list. SECURITY FIX (2026-09-03): now admin-only —
    previously unauthenticated, leaking every server name + IP + status."""
    pc = PortainerClient()
    return pc.list_endpoints()


# ---------- admin endpoints ----------

@app.post("/api/v1/admin/login", response_model=AdminLoginOut)
def admin_login(payload: AdminLoginIn, request: Request):
    """SECURITY (2026-09-03): per-IP rate limited + failed attempts recorded &
    auto-banned (admin password is a single high-value target)."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many login attempts. Try again in a minute.")
    if not verify_admin_password(payload.password):
        sc.record_failed(ip, "login_fail", None, "admin bad password")
        raise HTTPException(401, "Invalid admin password.")
    sc.record_ok(ip, "login_ok", None)
    # Admin 2FA gate. 0.1.90: if 2FA is switched on but no channel can actually
    # deliver (e.g. only SMS is on and the phone number was removed), refuse rather
    # than quietly signing the admin in without a second factor.
    state_2fa = account_security.admin_2fa_state()
    methods = state_2fa["methods"]
    if not methods and (db.get_setting("admin_2fa", "") or "").strip():
        raise HTTPException(
            409, "Admin 2FA is switched on but no usable method is configured. "
                 "Add your phone number or email in Settings.")
    if methods:
        return AdminLoginOut(
            token="__mfa__",
            expires_hours=settings.jwt_expiry_hours,
            mfa={"challenge": create_mfa_token(0, [m["method"] for m in methods]),
                 "methods": methods},
        )
    return AdminLoginOut(
        token=create_access_token("admin"),
        expires_hours=settings.jwt_expiry_hours,
    )


@app.post("/api/v1/admin/mfa-verify", response_model=AdminLoginOut)
def admin_mfa_verify(payload: AdminMFAVerifyIn, request: Request):
    """Admin second factor after the password. Verifies the MFA challenge token
    (proves the password was correct) + the code, then issues the admin JWT.
    SECURITY (2026-09-03): per-IP limited + failures recorded/auto-banned."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "mfa"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    account_id, methods = verify_mfa_token(payload.challenge)
    if account_id != 0:
        raise HTTPException(401, "Invalid admin MFA challenge.")
    enabled = account_security.admin_2fa_state()["methods"]
    if not enabled:
        raise HTTPException(409, "Admin 2FA is not enabled.")
    ok = False
    if any(m["method"] == "totp" for m in enabled):
        ok = account_security.admin_totp_verify_code(payload.code)
    if not ok and any(m["method"] in ("email", "sms") for m in enabled):
        # One code slot for the admin, whichever channel delivered it (0.1.90).
        ok = account_security.admin_otp_verify(payload.code)
    if not ok:
        sc.record_failed(ip, "mfa_fail", None)
        raise HTTPException(401, "That code is invalid or has expired.")
    sc.record_ok(ip, "login_ok", None)
    return AdminLoginOut(
        token=create_access_token("admin"),
        expires_hours=settings.jwt_expiry_hours,
    )


@app.post("/api/v1/admin/mfa-send-otp", response_model=dict)
def admin_mfa_send_otp(request: Request, payload: Optional[AdminMFASendOTPIn] = Body(None)):
    """Send the admin one-time code for the second-factor step, by email or SMS.

    Steward 2026-09-03: the admin has a real post-password MFA flow.
    Steward 2026-10-07: SMS joined it, so the channel is now chosen here. An SMS costs
    real credits, so that channel additionally demands the MFA challenge - proof the
    password step actually happened - instead of being an open tap on the admin's phone.
    """
    ip = sc.client_ip(request)
    if sc.check_rate(ip, "forgot"):
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    channel = ((payload.channel if payload else None) or "email").strip().lower()
    if channel not in ("email", "sms"):
        raise HTTPException(422, "Unknown code channel.")
    enabled = account_security.admin_2fa_state()["methods"]
    if not any(m["method"] == channel for m in enabled):
        raise HTTPException(409, "%s 2FA is not enabled for the admin."
                            % ("SMS" if channel == "sms" else "Email"))
    if channel == "sms":
        challenge = (payload.challenge or "").strip() if payload else ""
        if not challenge:
            raise HTTPException(400, "Sign in again to receive an SMS code.")
        try:
            acc_id, _methods = verify_mfa_token(challenge)
        except Exception:  # noqa: BLE001 - any bad token is the same answer to the caller
            acc_id = None
        if acc_id != 0:
            raise HTTPException(401, "Invalid admin MFA challenge.")
    try:
        return account_security.admin_send_otp(channel)
    except account_security.SecurityError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/admin/security", dependencies=[Depends(verify_admin)])
def admin_security_state():
    state = account_security.admin_2fa_state()
    state["passkeys"] = passkeys.list_passkeys_for("admin")
    state["passkey_enabled"] = bool(passkeys.list_passkeys_for("admin"))
    return state


@app.post("/api/v1/admin/security/totp/setup", dependencies=[Depends(verify_admin)])
def admin_totp_setup():
    return account_security.admin_totp_setup()


@app.post("/api/v1/admin/security/totp/enable", dependencies=[Depends(verify_admin)])
def admin_totp_enable(payload: TOTPSetupIn):
    if not account_security.admin_totp_verify_enable(payload.code.strip()):
        raise HTTPException(400, "That authenticator code is invalid. Try again.")
    return {"ok": True}


@app.post("/api/v1/admin/security/totp/disable", dependencies=[Depends(verify_admin)])
def admin_totp_disable():
    account_security.admin_totp_disable()
    return {"ok": True}


@app.post("/api/v1/admin/security/email/send", dependencies=[Depends(verify_admin)])
def admin_email_otp_send():
    return account_security.admin_email_send_otp()


@app.post("/api/v1/admin/security/email/enable", dependencies=[Depends(verify_admin)])
def admin_email_enable(payload: OTPEnableIn):
    if not account_security.admin_email_otp_verify(payload.code.strip()):
        raise HTTPException(400, "That code is invalid or has expired.")
    account_security.admin_email_enable()
    return {"ok": True}


@app.post("/api/v1/admin/security/email/disable", dependencies=[Depends(verify_admin)])
def admin_email_disable():
    cur = db.get_setting("admin_2fa", "") or ""
    db.set_setting("admin_2fa", ",".join(x for x in cur.split(",") if x != "email"))
    return {"ok": True}


# ---------- admin SMS (mNotify) 0.1.90, Steward 2026-10-07 --------------------

@app.post("/api/v1/admin/security/sms/send", dependencies=[Depends(verify_admin)])
def admin_sms_otp_send():
    """Send a code to the admin phone: used both to switch SMS 2FA on and to test it."""
    try:
        return account_security.admin_sms_send_otp()
    except account_security.SecurityError as e:
        raise HTTPException(400, str(e))


@app.post("/api/v1/admin/security/sms/enable", dependencies=[Depends(verify_admin)])
def admin_sms_enable(payload: OTPEnableIn):
    """Turn SMS on only after a code that arrived by SMS has been proven."""
    if not account_security.admin_otp_verify(payload.code.strip()):
        raise HTTPException(400, "That code is invalid or has expired.")
    try:
        account_security.admin_sms_enable()
    except account_security.SecurityError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/v1/admin/security/sms/disable", dependencies=[Depends(verify_admin)])
def admin_sms_disable():
    account_security.admin_sms_disable()
    return {"ok": True}


@app.post("/api/v1/admin/sms/test", dependencies=[Depends(verify_admin)])
def admin_sms_test():
    """A plain test message to the admin phone (not a code).

    Deliberately does NOT touch the one-time-code slot: testing your mNotify setup must
    not invalidate a code you are halfway through using at sign-in.
    """
    phone = account_security.admin_phone()
    if not account_security.admin_phone_ok():
        raise HTTPException(400, "Save your phone number first.")
    try:
        info = sms.send_sms(phone, "SteProTECH portal test message. Your mNotify setup "
                                   "works. No action needed.")
    except sms.SmsError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **info}


@app.get("/api/v1/admin/sms/delivery", dependencies=[Depends(verify_admin)])
def admin_sms_delivery(campaign_id: str = ""):
    """What actually happened to a message we sent (0.1.92, Steward: "is there a way to verify").

    The API answering 200 and a credit being deducted says nothing about delivery, so the
    Settings card asks mNotify's own report for the verdict after a test send.
    """
    return sms.delivery_status(campaign_id)


@app.get("/api/v1/admin/sms/status", dependencies=[Depends(verify_admin)])
def admin_sms_status():
    """Sender-ID approval + purpose + SMS credits, so an empty wallet is visible before it bites."""
    out = {"configured": sms.configured(), "sender_id": sms.sender_id(),
           "otp_typing": sms.otp_typing()}
    if not out["configured"]:
        out["error"] = "No mNotify API key saved yet."
        return out
    try:
        snd = sms.check_sender()
        out["sender"] = snd
        # A sender registered for OTP traffic and plain sending (or the reverse) is exactly the
        # mismatch that silently dropped his messages, so name it rather than let it hide.
        if snd.get("otp_purpose") and not out["otp_typing"]:
            out["purpose_mismatch"] = ("This Sender ID is registered for OTP traffic while plain "
                                       "sending is on. That combination has delivered on this "
                                       "account, so treat this as a note: if texts ever stop "
                                       "arriving, switch the route on (and fund the main wallet) "
                                       "or use a sender registered for ordinary notifications.")
        elif (not snd.get("otp_purpose")) and out["otp_typing"]:
            out["purpose_mismatch"] = ("The OTP route is on while this Sender ID is registered for "
                                       "ordinary notifications. If texts stop arriving, turn the "
                                       "route off or use the sender registered for OTP traffic.")
    except sms.SmsError as e:
        out["sender_error"] = str(e)
    try:
        out["balance"] = sms.sms_balance()
    except sms.SmsError as e:
        out["balance_error"] = str(e)
    return out


# ---------- admin passkeys (WebAuthn, Steward 2026-09-03) ----------

@app.post("/api/v1/admin/security/passkey/register", dependencies=[Depends(verify_admin)])
def admin_passkey_register_start():
    """Admin passkey registration start (single shared admin, scope='admin')."""
    try:
        return passkeys.registration_options("admin", None, "admin@steprotech.com")
    except Exception as e:
        raise HTTPException(400, f"Could not start passkey setup: {e}")


@app.post("/api/v1/admin/security/passkey/verify", dependencies=[Depends(verify_admin)])
def admin_passkey_register_verify(payload: PasskeyVerifyIn):
    try:
        return passkeys.verify_registration("admin", None, payload.credential)
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/admin/security/passkeys", dependencies=[Depends(verify_admin)])
def admin_passkey_list():
    return {"passkeys": passkeys.list_passkeys_for("admin")}


@app.post("/api/v1/admin/passkey/mfa/start", response_model=dict)
def admin_passkey_mfa_start(payload: MFAPasskeyStartIn, request: Request):
    """Admin passkey as a SECOND factor, after the password was accepted. The
    MFA challenge (account_id 0) is required so a passkey alone cannot mint the
    admin token. (Steward 2026-09-03: passkey is a 2FA option for the admin.)"""
    ip = sc.client_ip(request)
    if sc.is_banned(ip) or sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    account_id, methods = verify_mfa_token(payload.challenge)
    if account_id != 0 or "passkey" not in methods:
        raise HTTPException(403, "This admin does not allow a passkey as the second factor.")
    try:
        return passkeys.authentication_options("admin", None)
    except passkeys.PasskeyError:
        raise HTTPException(401, "No admin passkey registered.")


@app.post("/api/v1/admin/passkey/mfa/verify", response_model=AdminLoginOut)
def admin_passkey_mfa_verify(payload: MFAPasskeyVerifyIn, request: Request):
    """Finishes the admin second factor with a passkey. Requires the MFA
    challenge (password already proven) + a valid assertion, then issues the
    admin JWT."""
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    if sc.check_rate(ip, "mfa"):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    account_id, methods = verify_mfa_token(payload.challenge)
    if account_id != 0 or "passkey" not in methods:
        raise HTTPException(403, "This admin does not allow a passkey as the second factor.")
    try:
        passkeys.verify_authentication("admin", None, payload.credential)
    except passkeys.PasskeyError as e:
        sc.record_failed(ip, "mfa_fail", None)
        raise HTTPException(401, str(e))
    sc.record_ok(ip, "login_ok", None)
    return AdminLoginOut(token=create_access_token("admin"),
                         expires_hours=settings.jwt_expiry_hours)


@app.delete("/api/v1/admin/security/passkeys/{credential_id}", dependencies=[Depends(verify_admin)])
def admin_passkey_delete(credential_id: str):
    if not passkeys.delete_passkey_for("admin", None, credential_id):
        raise HTTPException(404, "Passkey not found.")
    return {"ok": True}


# Admin passkey sign-in (first-factor, no password). Only usable if admin has a
# passkey registered. If admin 2FA is enabled, the MFA gate still applies.
@app.post("/api/v1/admin/passkey/login/start")
def admin_passkey_login_start(request: Request):
    ip = sc.client_ip(request)
    if sc.is_banned(ip) or sc.check_rate(ip, "login"):
        raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.")
    try:
        return passkeys.authentication_options("admin", None)
    except passkeys.PasskeyError:
        raise HTTPException(401, "No admin passkey registered.")


@app.post("/api/v1/admin/passkey/login/verify", response_model=AdminLoginOut)
def admin_passkey_login_verify(payload: PasskeyVerifyIn, request: Request):
    ip = sc.client_ip(request)
    if sc.is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")
    try:
        passkeys.verify_authentication("admin", None, payload.credential)
    except passkeys.PasskeyError as e:
        sc.record_failed(ip, "login_fail", None, f"admin passkey verify: {e}")
        raise HTTPException(401, str(e))
    sc.record_ok(ip, "login_ok", None)
    methods = account_security.admin_2fa_state()["methods"]
    if methods:
        return AdminLoginOut(
            token="__mfa__",
            expires_hours=settings.jwt_expiry_hours,
            mfa={"challenge": create_mfa_token(0, [m["method"] for m in methods]),
                 "methods": methods},
        )
    return AdminLoginOut(token=create_access_token("admin"),
                         expires_hours=settings.jwt_expiry_hours)


@app.get("/api/v1/admin/settings", dependencies=[Depends(verify_admin)])
def get_admin_settings():
    out = {
        "landing_environments": db.get_setting("landing_environments", default="8"),
        "payments_open": billing.payments_open(),
        # Admin profile + SMS delivery (0.1.90). The mNotify key is write-only: only
        # whether it is stored and its last four characters ever come back.
        "admin_email": account_security.admin_email(),
        "admin_phone": account_security.admin_phone(),
        "admin_phone_pretty": sms.pretty_phone(account_security.admin_phone()),
        "sms_sender_id": sms.sender_id(),
        "sms_otp_type": "otp" if sms.otp_typing() else "",
        "sms_configured": sms.configured(),
        "admin_2fa": db.get_setting("admin_2fa", "") or "",
    }
    out.update(sms.masked_key())
    return out


@app.put("/api/v1/admin/settings", dependencies=[Depends(verify_admin)])
def put_admin_settings(payload: AdminSettingsIn):
    out = {}
    if payload.landing_environments is not None:
        ids = [x.strip() for x in payload.landing_environments.split(",")
               if x.strip().isdigit()]
        if not ids:
            raise HTTPException(422, "Provide at least one environment id.")
        db.set_setting("landing_environments", ",".join(ids))
        out["landing_environments"] = ",".join(ids)
    if payload.payments_open is not None:
        billing.set_payments_open(payload.payments_open)
        out["payments_open"] = billing.payments_open()
    # ---- admin profile (0.1.90) -------------------------------------------------
    if payload.admin_email is not None:
        addr = payload.admin_email.strip()
        if addr and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", addr):
            raise HTTPException(422, "That email address does not look right.")
        db.set_setting(account_security.ADMIN_EMAIL_SETTING, addr)
        out["admin_email"] = account_security.admin_email()
    if payload.admin_phone is not None:
        raw = payload.admin_phone.strip()
        if not raw:
            db.set_setting(account_security.ADMIN_PHONE_SETTING, "")
            out["admin_phone"] = ""
        else:
            local = sms.normalise_phone(raw)
            if not local:
                raise HTTPException(422, "That phone number does not look like a Ghana "
                                         "number. Use 024 123 4567 or +233 24 123 4567.")
            db.set_setting(account_security.ADMIN_PHONE_SETTING, local)
            out["admin_phone"] = local
            out["admin_phone_pretty"] = sms.pretty_phone(local)
    # ---- SMS delivery (0.1.90) --------------------------------------------------
    if payload.sms_api_key is not None:
        db.set_setting(sms.SMS_KEY, payload.sms_api_key.strip())
        out.update(sms.masked_key())          # never the key itself
    if payload.sms_sender_id is not None:
        try:
            name = sms.validate_sender(payload.sms_sender_id)
        except sms.SmsError as e:
            raise HTTPException(422, str(e))
        db.set_setting(sms.SMS_SENDER, name)
        out["sms_sender_id"] = sms.sender_id()
    if payload.sms_otp_type is not None:
        mode = "otp" if str(payload.sms_otp_type).strip().lower() in ("otp", "1", "true", "on") \
            else ""
        db.set_setting(sms.SMS_OTP_TYPE, mode)
        out["sms_otp_type"] = mode
    if not out:
        raise HTTPException(422, "Nothing to update.")
    return out


@app.get("/api/v1/admin/accounts", dependencies=[Depends(verify_admin)])
def admin_accounts(include_archived: int = 0):
    """All accounts. Archived are excluded by default; pass include_archived=1
    to list them too (the admin Archive page uses this for restore)."""
    out = []
    for a in db.list_accounts():
        state = _row_get(a, "account_state", "active")
        if state == "archived" and not include_archived:
            continue
        out.append(_account_to_out(a).model_dump())
    return out


@app.get("/api/v1/admin/access-requests", dependencies=[Depends(verify_admin)])
def admin_access_requests():
    """Pending/issued onboarding requests — the admin dashboard's 'who wants in' list."""
    out = []
    for r in db.list_access_requests():
        out.append(AccessRequestOut(
            id=r["id"], email=r["email"], status=r["status"],
            created_at=r["created_at"],
            token_sent_at=r["token_sent_at"],
            token_expires_at=r["token_expires_at"],
        ).model_dump())
    return out


@app.post("/api/v1/admin/access-requests/{request_id}/approve", dependencies=[Depends(verify_admin)])
def admin_approve_request(request_id: int):
    """Admin action (0.1.77, Steward): approve the request outright, with no code.

    The person is emailed that their access is approved and asked to create their
    account; registration then signs them in. `email_sent` is returned so the
    dashboard can say plainly when the notice did not go out."""
    try:
        access_gate.approve_request(request_id)
    except access_gate.AccessGateError as e:
        raise HTTPException(409, str(e))
    req = db.get_access_request_by_id(request_id)
    email = req["email"] if req else ""
    email_sent = False
    if email:
        try:
            send_access_approved(email)
            email_sent = True
        except EmailError as e:
            log.warning(f"Approval email failed for {email}: {e}")
    return {"request_id": request_id, "email": email,
            "status": "approved", "email_sent": email_sent}


@app.post("/api/v1/admin/access-requests/{request_id}/token", dependencies=[Depends(verify_admin)])
def admin_issue_token(request_id: int):
    """LEGACY ALIAS (so an already-open dashboard keeps working): issuing a code
    was replaced by approval in 0.1.77 and both now do the same thing."""
    return admin_approve_request(request_id)


@app.post("/api/v1/admin/access-requests/{request_id}/deny", dependencies=[Depends(verify_admin)])
def admin_deny_request(request_id: int):
    """Admin action: decline a request. Sets a terminal 'denied' state so the
    person cannot register, and clears any issued code. Delete the request to
    let them try again later. Steward 2026-09-03."""
    try:
        access_gate.deny_request(request_id)
    except access_gate.AccessGateError as e:
        raise HTTPException(409, str(e))
    return {"request_id": request_id, "status": "denied"}


@app.delete("/api/v1/admin/access-requests/{request_id}", dependencies=[Depends(verify_admin)])
def admin_delete_request(request_id: int):
    """Admin action: permanently remove a request row so it is no longer listed.
    If that email enters the portal again, a fresh request is created."""
    req = db.get_access_request_by_id(request_id)
    if not req:
        raise HTTPException(404, "Access request not found.")
    db.delete_access_request(request_id)
    return {"request_id": request_id, "deleted": True}


@app.post("/api/v1/admin/billing/sweep", dependencies=[Depends(verify_admin)])
def admin_sweep():
    """Lock any past-due accounts past their grace deadline (run periodically)."""
    try:
        result = billing.sweep_past_due()
    except Exception as e:
        raise HTTPException(502, str(e))
    return result


# ---------- renewal reminders + login-free renewal links (0.1.76) ----------

def _renewal_page(title: str, message: str) -> str:
    """Plain, branded page for an expired or unknown renewal link."""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title}</title></head>
    <body style="font-family:Arial,sans-serif;background:#f9fafb;margin:0;padding:48px 16px">
      <div style="max-width:520px;margin:auto;background:#ffffff;border:1px solid #e5e7eb;
                  border-radius:10px;padding:28px">
        <h2 style="color:#1f2937;margin-top:0">{title}</h2>
        <p style="color:#374151;line-height:1.5">{message}</p>
        <p><a href="{settings.portal_base_url}" style="color:#1f2937">Go to the portal</a></p>
      </div>
    </body></html>"""


@app.get("/r/{token}", include_in_schema=False)
def renewal_link(token: str):
    """A reminder email's link: start a payment for that account, no sign-in.

    The token is an unguessable 60-day key bound to one account and package. It
    can only create an unpaid checkout for that same account; it cannot sign in,
    read data, change anything, or reach another account.
    """
    from .services import renewal, paystack as paystack_svc

    row = renewal.resolve_link(token)
    if not row:
        return HTMLResponse(_renewal_page(
            "This renewal link has expired",
            "Renewal links stay valid for 60 days. Please sign in to your dashboard to "
            "renew, or reply to our email and we will send you a fresh link."),
            status_code=410)
    account = db.get_account(int(row["account_id"]))
    if not account or (account["account_state"] or "active") != "active":
        return HTMLResponse(_renewal_page(
            "This account cannot renew here",
            "Please get in touch with us and we will help you directly."),
            status_code=410)
    try:
        out = paystack_svc.initialize_checkout(int(account["id"]), account["email"],
                                               row["plan"])
    except Exception as e:
        log.error("renewal link: checkout failed for account %s: %s", account["id"], e)
        return HTMLResponse(_renewal_page(
            "We could not open the payment page",
            "Please try the link again in a minute, or renew from your dashboard at "
            f"{settings.portal_base_url}."), status_code=502)
    db.mark_renewal_link_used(token, int(_time.time()))
    log.info("renewal link used for account %s (package %s)", account["id"], row["plan"])
    return RedirectResponse(out["authorization_url"], status_code=302)


@app.get("/api/v1/admin/billing/renewal-notices", dependencies=[Depends(verify_admin)])
def renewal_notices(account_id: Optional[int] = None):
    """Every reminder email, rendered, WITHOUT sending anything.

    Sending is off until Steward approves the wording; this is how the copy is
    reviewed against a real account (or a sample when no id is given).
    """
    from .services import renewal
    try:
        return renewal.preview(account_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@app.post("/api/v1/admin/billing/renewal-notices/send", dependencies=[Depends(verify_admin)])
def renewal_notices_send(account_id: int, stage: str):
    """Send ONE reminder to a chosen account (the deliberate live test)."""
    from .services import renewal
    try:
        return renewal.send_for_account(account_id, stage)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        raise HTTPException(502, detail=f"send failed: {e}")


@app.post("/api/v1/admin/accounts/{account_id}/lock", dependencies=[Depends(verify_admin)])
def admin_lock(account_id: int):
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    ok = billing.lock_instance(account_id)
    if not ok:
        raise HTTPException(409, "No healthy instance to lock.")
    db.update_subscription_status(account_id, "locked")
    return {"status": "locked", "account_id": account_id}


@app.post("/api/v1/admin/accounts/{account_id}/unlock", dependencies=[Depends(verify_admin)])
def admin_unlock(account_id: int):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    ok = billing.unlock_instance(account_id)
    if not ok:
        raise HTTPException(409, "No healthy instance to unlock.")
    db.update_subscription_status(account_id, "active")
    return {"status": "unlocked", "account_id": account_id}


class QuotaIn(BaseModel):
    quota: int = Field(..., ge=1, le=50)
    plan: Optional[str] = None  # 'a' | 'b': which package's allowance (0.1.68); omit = legacy single quota


@app.put("/api/v1/admin/accounts/{account_id}/quota", dependencies=[Depends(verify_admin)])
def admin_set_quota(account_id: int, payload: QuotaIn):
    """Admin raises/lowers how many n8n instances this account may provision.
    0.1.68: payload.plan targets a specific package's allowance (a|b); without
    it the LEGACY single quota is set (kept for API compatibility)."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    plan = (payload.plan or "").strip().lower()
    if plan:
        if plan not in ("a", "b"):
            raise HTTPException(422, "Plan must be 'a' or 'b'.")
        db.set_account_quota_for(account_id, plan, payload.quota)
        return {"account_id": account_id, "plan": plan, "quota": payload.quota}
    # 0.1.73: db.set_account_quota also raises the account's CURRENT package
    # allowance - the value the provisioning gate actually reads - so a legacy
    # write can no longer raise a number nothing consults.
    db.set_account_quota(account_id, payload.quota)
    current = db._row_get(db.get_account(account_id), "plan", "a") or "a"
    return {"account_id": account_id, "quota": payload.quota,
            "plan": "b" if current == "b" else "a"}


@app.get("/api/v1/admin/accounts/{account_id}/quotas", dependencies=[Depends(verify_admin)])
def admin_get_quotas(account_id: int):
    """Per-plan workspace allowances for the quota editor."""
    a = db.get_account(account_id)
    if not a:
        raise HTTPException(404, "Account not found.")
    return {"account_id": account_id,
            "plan": db._row_get(a, "plan", "a") or "a",
            "quota_a": db.account_quota_for(a, "a"),
            "quota_b": db.account_quota_for(a, "b")}


class PlanIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=1)   # 'a' (GHS 300) | 'b' (GHS 500)


@app.put("/api/v1/admin/accounts/{account_id}/plan", dependencies=[Depends(verify_admin)])
def admin_set_plan(account_id: int, payload: PlanIn, request: Request):
    """Admin moves an account between packages, in BOTH directions.

    This changes what the account is subscribed to, which package's allowance applies to its
    next workspace, and what the billing copy shows. It deliberately does NOT touch a running
    workspace: resources live in the stack file, so changing them recreates the container and
    is a separate, disruptive action (/admin/instances/{id}/plan). The response names any
    workspace still running on the other package so the admin can decide about it.
    """
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    try:
        target = plan_switch.normalize_plan(payload.plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    current = (db._row_get(account, "plan", "a") or "a")
    if target == current:
        return {"account_id": account_id, "plan": target, "changed": False,
                "label": plan_switch.plan_label(target),
                "note": "The account is already on this package."}
    db.set_account_plan(account_id, target)
    db.record_auth_event(sc.client_ip(request), "plan_change", account_id,
                         f"{current} -> {target} by admin")
    log.info("Account %s package %s -> %s by admin", account_id, current, target)
    behind = [str(i["stack_name"]) for i in db.list_instances(account_id)
              if ((db._row_get(i, "plan", "a") or "a") != target)]
    return {
        "account_id": account_id, "plan": target, "changed": True,
        "label": plan_switch.plan_label(target),
        "workspaces_on_other_plan": behind,
        "note": ("Running workspaces keep the resources they were built with. "
                 "Switch each one to move its CPU and memory." if behind
                 else "Every workspace already matches the new package."),
    }


class UpgradeRequestIn(BaseModel):
    note: str = ""


def _notify_admin_of_upgrade(account, instance, from_plan: str, to_plan: str, note: str) -> bool:
    """Tell the admin a customer asked to move up. Never fatal: the request row is the record."""
    to = (getattr(settings, "admin_email", "") or "").strip()
    if not to:
        log.warning("upgrade request: no admin_email configured, nobody was notified")
        return False
    who = (db._row_get(account, "display_name", "") or db._row_get(account, "email", "")
           or f"account {account['id']}")
    subject = f"Upgrade request: {who} wants {plan_switch.plan_label(to_plan)}"
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>{k}</td><td>{v}</td></tr>"
        for k, v in (
            ("Customer", who),
            ("Email", db._row_get(account, "email", "")),
            ("Workspace", (db._row_get(instance, "stack_name", "") or "none") if instance else "none"),
            ("Domain", (db._row_get(instance, "domain", "") or "") if instance else ""),
            ("Now", plan_switch.plan_label(from_plan)),
            ("Wants", plan_switch.plan_label(to_plan)),
            ("Their note", note or "(none)"),
        )
    )
    html = (
        "<p>A customer asked to upgrade through the portal. Payment for upgrades is not built "
        "yet, so nothing was charged and no workspace was changed.</p>"
        f"<table>{rows}</table>"
        "<p>Contact them to arrange it, then use Change plan and the workspace switch in the "
        "admin console.</p>"
    )
    try:
        send_email(to, subject, html)
        return True
    except EmailError as exc:
        log.warning("upgrade request: admin notification failed: %s", exc)
        return False


@app.post("/api/v1/accounts/{account_id}/upgrade-request")
def request_upgrade(account_id: int, payload: UpgradeRequestIn,
                    request: Request,
                    authorization: str = Header(default=None, alias="Authorization")):
    """A customer asks to move to the bigger package. Records the wish and emails the admin.

    Payment is deliberately out of scope for now: the admin contacts them. One open request per
    account is enough to act on, so a second click reports "already requested" instead of
    stacking duplicates.
    """
    authorize_owner_or_admin(authorization, account_id)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    current = (db._row_get(account, "plan", "a") or "a")
    target = plan_switch.other_plan(current)
    if target == "a":
        raise HTTPException(
            409, "You are already on the bigger package. For anything else, contact support."
        )
    instances = db.list_instances(account_id)
    instance = instances[0] if instances else None
    note = (payload.note or "").strip()
    rid = db.create_upgrade_request(
        account_id, current, target,
        instance_id=(instance["id"] if instance else None), note=note,
    )
    if rid is None:
        existing = db.open_upgrade_request(account_id) or {}
        return {"status": "already_requested", "request_id": existing.get("id"),
                "to_plan": target, "to_label": plan_switch.plan_label(target),
                "message": "Your upgrade request is already open. We will contact you."}
    notified = _notify_admin_of_upgrade(account, instance, current, target, note)
    db.record_auth_event(sc.client_ip(request), "upgrade_request", account_id,
                         f"{current} -> {target}")
    return {"status": "requested", "request_id": rid, "to_plan": target,
            "to_label": plan_switch.plan_label(target), "admin_notified": notified,
            "message": "Request received. We will contact you to arrange the upgrade."}


class BackupIn(BaseModel):
    backup_enabled: bool


@app.put("/api/v1/admin/accounts/{account_id}/backup", dependencies=[Depends(verify_admin)])
def admin_set_backup(account_id: int, payload: BackupIn):
    """Grant/revoke a customer's self-service backup permission. When False the
    customer sees NO backup UI. The admin always retains backup access."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    db.set_account_backup(account_id, payload.backup_enabled)
    return {"account_id": account_id, "backup_enabled": payload.backup_enabled}


class UpdateAccessIn(BaseModel):
    update_enabled: bool


@app.put("/api/v1/admin/accounts/{account_id}/update-access",
         dependencies=[Depends(verify_admin)])
def admin_set_update_access(account_id: int, payload: UpdateAccessIn):
    """Grant/revoke a customer's self-update permission (0.1.88). Same shape as the backup
    permission: off by default, and while it is off the customer sees NO update UI. The
    admin can always update any workspace from the Updates page."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    db.set_account_update(account_id, payload.update_enabled)
    return {"account_id": account_id, "update_enabled": payload.update_enabled}


# ---------- admin impersonation (login as a customer, 2026-09-03) ----------
# One-click troubleshooting: the admin steps into an ACTIVE customer's portal
# session. The minted token is a normal customer session (sub acc:<id>) so every
# customer route applies unchanged (lifecycle state still enforced), but it
# carries the 'imp' flag + a short TTL, and both start and end are recorded on
# the security trail (auth_events).

@app.post("/api/v1/admin/accounts/{account_id}/impersonate", response_model=dict)
def admin_impersonate(account_id: int, request: Request,
                      _: None = Depends(verify_admin)):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(
            409,
            "Only active accounts can be impersonated. Unsuspend or restore the account first.",
        )
    token = create_impersonation_token(account_id)
    db.record_auth_event(
        sc.client_ip(request), "impersonate_start", account_id,
        detail="username=" + str(account["username"]),
    )
    return {
        "token": token,
        "account": _account_to_out(account).model_dump(),
        "impersonation": True,
        "expires_minutes": settings.impersonation_ttl_minutes,
    }


@app.post("/api/v1/admin/billing/sweep-expired", dependencies=[Depends(verify_admin)])
def admin_sweep_expired():
    """Manually run the auto-expiry sweep (the background task also runs it)."""
    try:
        result = billing.sweep_expired()
    except Exception as e:
        raise HTTPException(502, str(e))
    return result


# ---------- admin-assisted operations (2026-09-02) ----------

@app.get("/api/v1/admin/environments", dependencies=[Depends(verify_admin)])
def admin_environments():
    """Environment cards: display numbering (n8n Server 1..N), full server
    names + IP, health, running stacks, linked accounts, storage."""
    try:
        return admin_ops.environment_overview()
    except AdminOpsError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"Environment scan failed: {e}")


@app.get("/api/v1/admin/accounts/{account_id}/subscription-anchor",
         dependencies=[Depends(verify_admin)])
def admin_subscription_anchor(account_id: int):
    """NPM created_on anchor for an account's workspace: the proxy host's
    creation date is the source of truth for when the service began, so
    mark-paid can preload start = created_on and expiry = +1 year."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    try:
        anchor = admin_ops.npm_subscription_anchor(account_id)
    except Exception as e:
        raise HTTPException(502, f"Anchor lookup failed: {e}")
    if not anchor:
        return {"anchor": None}
    return {"anchor": anchor}


@app.post("/api/v1/admin/accounts", response_model=dict, status_code=201,
          dependencies=[Depends(verify_admin)])
def admin_add_user(payload: AdminAddUserIn):
    """Admin creates a portal user directly (no access gate): auto-generated
    password, emailed. Account starts pending/unpaid; admin attaches an
    instance + marks paid to complete onboarding."""
    try:
        result = admin_ops.admin_create_account(
            str(payload.email), payload.first_name, payload.last_name)
    except AdminOpsError as e:
        raise HTTPException(409, str(e))
    return result


@app.get("/api/v1/admin/stacks/unlinked", dependencies=[Depends(verify_admin)])
def admin_unlinked_stacks():
    """n8n stacks (running or off) on the n8n servers not yet attached to any
    portal account. Dropdown source for the attach action."""
    try:
        return admin_ops.discover_unlinked_stacks()
    except Exception as e:
        raise HTTPException(502, f"Stack discovery failed: {e}")


@app.post("/api/v1/admin/accounts/{account_id}/attach", response_model=dict,
          dependencies=[Depends(verify_admin)])
def admin_attach(account_id: int, payload: AdminAttachIn):
    """Bind an existing n8n stack to a portal account as its instance. The
    stack must exist (running or stopped) on the given environment; the owner
    password is untouched; nothing is started/stopped here."""
    try:
        result = admin_ops.attach_instance(
            account_id, payload.environment_id, payload.stack_name,
            payload.port, payload.domain)
    except AdminOpsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"Attach failed: {e}")
    return result


@app.post("/api/v1/admin/accounts/{account_id}/mark-paid", response_model=dict,
          dependencies=[Depends(verify_admin)])
def admin_mark_paid(account_id: int, payload: AdminMarkPaidIn):
    """Admin records a subscription with custom dates (no payment gateway).
    Future expiry -> active + instance started if stopped; past expiry ->
    unpaid + instance stopped (backdated already-expired accounts).
    0.1.68: payload.plan selects WHICH package was paid for (a|b) and is
    stored on the account, so provisioning uses that plan's template + quota."""
    plan = (payload.plan or "").strip().lower()
    if plan and plan not in ("a", "b"):
        raise HTTPException(422, "Plan must be 'a' or 'b'.")
    try:
        result = admin_ops.mark_paid(account_id, payload.paid_until or 0,
                                     payload.paid_from)
        if plan:
            from . import db as _db
            _db.set_account_plan(account_id, plan)
            result["plan"] = plan
    except AdminOpsError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Mark-paid failed: {e}")
    return result


@app.post("/api/v1/admin/accounts/{account_id}/extend", response_model=dict,
          dependencies=[Depends(verify_admin)])
def admin_extend_expiry(account_id: int, payload: AdminExtendIn):
    """Free renewal (Steward 2026-09-03): extend an account's expiry by N
    whole years from its current expiry (or from today when none/past),
    mark active and resume a locked workspace. Works while payments are on
    hold - this is the no-payment path for onboarding and renewals."""
    try:
        result = admin_ops.extend_expiry(account_id, payload.years)
    except AdminOpsError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Extend failed: {e}")
    return result


@app.post("/api/v1/admin/accounts/{account_id}/state", response_model=dict,
          dependencies=[Depends(verify_admin)])
def admin_set_account_state(account_id: int, payload: AdminSetAccountStateIn):
    """Admin lifecycle: suspend | unsuspend | archive | restore (2026-09-02).
    suspend/archive STOP the attached workspace immediately and block portal
    login. Nothing is ever permanently deleted; restore lands in suspended."""
    actions = {
        "suspend": admin_ops.suspend_account,
        "unsuspend": admin_ops.unsuspend_account,
        "archive": admin_ops.archive_account,
        "restore": admin_ops.restore_account,
    }
    try:
        result = actions[payload.action](account_id)
    except AdminOpsError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"State change failed: {e}")
    return result


@app.post("/api/v1/instances/{instance_id}/reset-password", dependencies=[Depends(verify_admin)])
def reset_password(instance_id: int):
    """Full real reset: change the n8n owner password AND the basic-auth door lock,
    then email the new password. v1.1 (2026-09-01) — previously this only updated
    the DB + emailed, which emailed a password that did NOT match the door lock
    (same bug class as the owner-auto-create issue). Now:
      1. read current password from the stack env (source of truth)
      2. change the n8n owner password via PATCH /rest/me/password
      3. update the Portainer stack env (N8N_BASIC_AUTH_PASSWORD) + redeploy
      4. persist + email the new password"""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    if not inst["stack_id"] or not inst["environment_id"]:
        raise HTTPException(409, "Instance has no live stack (not provisioned).")

    pc = PortainerClient()
    try:
        stack = pc.get_stack(inst["stack_id"])
    except Exception as e:
        raise HTTPException(502, f"Cannot read stack: {e}")
    env = {e["name"]: e["value"] for e in (stack.get("Env") or [])}
    current_pw = env.get("N8N_BASIC_AUTH_PASSWORD")
    if not current_pw:
        raise HTTPException(502, "Stack env has no N8N_BASIC_AUTH_PASSWORD.")

    # forward IP for direct n8n API access (same resolution as provisioning)
    ep = pc.get_endpoint(inst["environment_id"])
    forward_ip = ep.get("PublicURL") or ep.get("URL", "").replace("tcp://", "").split(":")[0]
    if not forward_ip:
        raise HTTPException(502, "Cannot resolve environment forward IP.")

    new_pass = db.new_password(length=16)
    try:
        provisioner.validate_password_policy(new_pass)
        # PATCH /rest/me/password enforces N8N_HOST host-match (verified 2026-09-01):
        # must go through the PUBLIC origin, not the direct IP.
        provisioner.change_n8n_password(forward_ip, inst["port"], inst["basic_auth_user"],
                                        current_pw, new_pass,
                                        use_public=True, domain=inst["domain"])
    except Exception as e:
        raise HTTPException(502, f"n8n password change failed: {e}")

    # update the door lock env var + redeploy the stack
    new_env = [{"name": e["name"], "value": e["value"]} for e in (stack.get("Env") or [])]
    for item in new_env:
        if item["name"] == "N8N_BASIC_AUTH_PASSWORD":
            item["value"] = new_pass
    try:
        pc.update_stack_env(inst["stack_id"], inst["environment_id"], new_env)
    except Exception as e:
        # n8n password already changed; door lock update failed — report honestly
        raise HTTPException(502, f"n8n password changed but stack env update failed: {e}")

    db.update_instance(instance_id, basic_auth_password=new_pass)
    try:
        send_reset_password(inst["basic_auth_user"], inst["stack_name"], new_pass)
    except EmailError as e:
        log.warning(f"Reset email failed for instance {instance_id}: {e}")

    return {"status": "password_reset", "new_password": new_pass}


# ---------- admin backups + image update (Steward 2026-09-03) ----------

@app.get("/api/v1/admin/backups", dependencies=[Depends(verify_admin)])
def admin_backups(account_id: int | None = None):
    """All backups (or one account's). Control host can see every tenant's."""
    rows = db.list_backups(account_id)
    return {"backups": [_backup_to_out(r) for r in rows]}


@app.post("/api/v1/admin/instances/{instance_id}/backup",
          dependencies=[Depends(verify_admin)], status_code=201)
def admin_create_backup(instance_id: int, kind: str = "full",
                        background: BackgroundTasks = None):
    """Admin triggers a backup on any instance. kind = full | workflows | credentials."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    if kind not in ("full", "workflows", "credentials"):
        raise HTTPException(400, "kind must be 'full', 'workflows' or 'credentials'.")
    filename = {"full": "n8n-data.tar.gz", "workflows": "workflows.json",
                "credentials": "credentials.json"}[kind]
    bid = db.create_backup(inst["account_id"], instance_id, kind, filename)
    if background:
        background.add_task(_run_backup, bid, dict(inst), kind)
    return {"id": bid, "status": "creating"}


@app.get("/api/v1/admin/backups/{backup_id}/download", dependencies=[Depends(verify_admin)])
def admin_download_backup(backup_id: int):
    """Admin downloads any backup file."""
    b = db.get_backup(backup_id)
    if not b:
        raise HTTPException(404, "Backup not found.")
    return _serve_backup(b)


class UpdateImageIn(BaseModel):
    image: str = Field(..., min_length=1, max_length=120)


class UpdateQueueIn(BaseModel):
    instance_ids: list[int] | None = None
    all_behind: bool = False


@app.get("/api/v1/admin/updates", dependencies=[Depends(verify_admin)])
def admin_updates():
    """Everything the Updates page shows (0.1.85): who is behind the latest release,
    the live queue, each server's next allowed slot and recent history."""
    return update_queue.overview()


@app.post("/api/v1/admin/updates/queue", dependencies=[Depends(verify_admin)])
def admin_queue_updates(payload: UpdateQueueIn):
    """Queue updates for chosen workspaces, or for everything that is behind.

    Admin rows take priority over customer requests, but the worker still moves one
    workspace PER SERVER with the quiet gap between them, so a bulk action cannot
    pile recreations onto one box.
    """
    if payload.all_behind:
        res = update_queue.queue_all_behind(requested_by="admin")
    else:
        ids = [int(i) for i in (payload.instance_ids or [])]
        if not ids:
            raise HTTPException(400, "Pick at least one workspace, or use all_behind.")
        res = update_queue.queue_workspaces(ids, requested_by="admin")
    if not res.get("ok") and res.get("error"):
        raise HTTPException(503, res["error"])
    if res.get("queued"):
        # start the first one now rather than on the next supervisor tick
        update_queue.worker_tick()
    return res


@app.post("/api/v1/admin/updates/cancel", dependencies=[Depends(verify_admin)])
def admin_cancel_updates(batch_id: str):
    """Cancel the queued (not yet started) rows of one batch. A running update is
    left to finish, because interrupting a recreate is worse than waiting."""
    return {"ok": True, "canceled": update_queue.cancel_batch(batch_id)}


@app.post("/api/v1/admin/instances/{instance_id}/update-image",
          dependencies=[Depends(verify_admin)])
def admin_update_image(instance_id: int, payload: UpdateImageIn):
    """Change the n8n image tag on an instance and redeploy its stack container.
    The stack is recreated against the same volume (no data loss). Roll back by
    setting the previous image tag."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    # 0.1.85: a manual update runs at once, as Steward asked, but it takes this
    # server's slot so the queue worker cannot add a second update to the same box
    # while it runs.
    _manual_q = update_queue.reserve_manual(inst, payload.image,
                                            update_queue.tag_to_version(payload.image))
    try:
        new_image = backup_ops.update_instance_image(inst, payload.image)
    except ValueError as e:
        update_queue.complete_manual(_manual_q, False, error=str(e))
        raise HTTPException(422, str(e))
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if any(k in low for k in ("manifest unknown", "pull access denied",
                                  "no such image", "not found",
                                  "does not exist", "repository does not exist")):
            # A real registry miss (the old failure mode looked like the image
            # was missing on the server when the pull was never requested).
            raise HTTPException(
                422,
                "Version '" + payload.image + "' does not exist on Docker Hub for "
                "n8n. Double-check the tag (format like 2.31.6) and try again.",
            )
        log.error("update image %s: %s", instance_id, e)
        update_queue.complete_manual(_manual_q, False, error=msg)
        raise HTTPException(502, f"Image update failed: {e}")
    update_queue.complete_manual(_manual_q, True, note="manual update from the dashboard",
                                 result_version=update_queue.tag_to_version(payload.image))
    return {"ok": True, "instance_id": instance_id, "image": new_image}


@app.get("/api/v1/admin/instances/{instance_id}/plan-preview",
         dependencies=[Depends(verify_admin)])
def admin_plan_preview(instance_id: int, plan: str = ""):
    """What switching this workspace's package would change, read from its live stack file.

    Read-only: drives the confirm dialog so the admin sees the real numbers before a container
    is recreated.
    """
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Workspace not found.")
    try:
        target = plan_switch.normalize_plan(plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    try:
        return plan_switch.preview(inst, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


class InstancePlanIn(BaseModel):
    plan: str = Field(..., min_length=1, max_length=1)
    backup_first: bool = True


@app.post("/api/v1/admin/instances/{instance_id}/plan", dependencies=[Depends(verify_admin)])
def admin_switch_instance_plan(instance_id: int, payload: InstancePlanIn,
                               request: Request, background: BackgroundTasks = None):
    """Move ONE workspace to the other package's resources. Works in both directions.

    The container is recreated against the same named volume, so workflows, executions and
    credentials survive, but the workspace blinks offline for a few seconds and anything
    mid-execution is lost. `backup_first` (default true) records a full backup before the switch;
    it is the admin's safety net, so turning it off is a deliberate choice.
    """
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Workspace not found.")
    try:
        target = plan_switch.normalize_plan(payload.plan)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    current = (db._row_get(inst, "plan", "a") or "a")
    backup_id = None
    if payload.backup_first:
        try:
            bid = db.create_backup(inst["account_id"], instance_id, "full", "n8n-data.tar.gz")
            if background:
                background.add_task(_run_backup, bid, dict(inst), "full")
            backup_id = bid
        except Exception as exc:  # noqa: BLE001 - no backup, no switch
            raise HTTPException(409, f"Could not start the pre-switch backup: {exc}")
    try:
        result = plan_switch.switch(inst, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.record_auth_event(sc.client_ip(request), "plan_switch", inst["account_id"],
                         f"instance {instance_id} {current} -> {target}")
    log.info("Workspace %s switched %s -> %s by admin", instance_id, current, target)
    return {
        "instance_id": instance_id,
        "from_plan": current, "to_plan": target,
        "from_label": plan_switch.plan_label(current),
        "to_label": plan_switch.plan_label(target),
        "action": "downgrade" if target == "a" else "upgrade",
        "backup_id": backup_id,
        **result,
    }


@app.get("/api/v1/admin/upgrade-requests", dependencies=[Depends(verify_admin)])
def admin_upgrade_requests(status: str = "open"):
    """Customer upgrade requests, newest first. status=open|handled|all."""
    rows = db.list_upgrade_requests(status)
    for r in rows:
        r["from_label"] = plan_switch.plan_label(r.get("from_plan"))
        r["to_label"] = plan_switch.plan_label(r.get("to_plan"))
    return {"data": rows, "open_count": len(db.list_upgrade_requests("open"))}


@app.post("/api/v1/admin/upgrade-requests/{request_id}/handle",
          dependencies=[Depends(verify_admin)])
def admin_handle_upgrade_request(request_id: int):
    """Mark a request dealt with, so the admin list only shows what still needs contact."""
    if not db.handle_upgrade_request(request_id, handled_by="admin"):
        raise HTTPException(409, "That request is not open (already handled or missing).")
    return {"id": request_id, "status": "handled"}


@app.post("/api/v1/admin/instances/{instance_id}/restart", dependencies=[Depends(verify_admin)])
def admin_restart_instance(instance_id: int):
    """Restart any workspace in place. The admin is not paced and acts whenever needed."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Workspace not found.")
    return _restart_instance(inst, actor="admin")


@app.get("/api/v1/admin/instances/{instance_id}/image", dependencies=[Depends(verify_admin)])
def admin_current_image(instance_id: int):
    """The instance's image reference AND the version it is really running.

    `image` is what the update field needs; `version` is what the
    behind/current report must use, because the reference is normally the
    floating `latest` tag, which carries no version at all (0.1.80).
    """
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    ver = backup_ops.current_version(inst, allow_exec=True)
    return {
        "image": backup_ops.current_image(inst),
        "version": ver["version"],
        "source": ver["source"],
    }


@app.get("/api/v1/admin/instance-versions", dependencies=[Depends(verify_admin)])
def admin_instance_versions():
    """True running n8n version for every instance (workspace list chip).

    Batched: one container list + one image list per environment, so it stays
    cheap however many workspaces exist. Empty on failure - the UI must show
    "unknown" rather than claim a workspace is current.
    """
    try:
        return {"versions": backup_ops.true_versions(db.list_instances())}
    except Exception as e:
        log.warning("instance version list failed: %s", e)
        return {"versions": {}}


class RunnerAttachIn(BaseModel):
    # Reserved for future policy options (stdlib scope, third-party packages);
    # attach currently takes no input and always ships the safe default policy.
    pass


def _runner_instance_or_404(instance_id: int):
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    return inst


@app.post("/api/v1/admin/instances/{instance_id}/python-runner/attach",
          dependencies=[Depends(verify_admin)])
def admin_attach_python_runner(instance_id: int, payload: RunnerAttachIn = None):
    """Attach the official n8n Python/JS task-runner sidecar to ONE workspace
    (admin opt-in, one at a time). Recreates the stack (n8n restarts against
    the same volume; workflows and credentials are preserved) and starts the
    runners container alongside. Rollback = the detach endpoint."""
    inst = _runner_instance_or_404(instance_id)
    try:
        result = python_runner.attach_runner(inst)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("runner attach %s: %s", instance_id, e)
        raise HTTPException(502, f"Python runner attach failed: {e}")
    return {"ok": True, "instance_id": instance_id,
            "runners_tag": result.get("tag"),
            "runner_container_seen": result.get("runner_container_seen", False)}


@app.post("/api/v1/admin/instances/{instance_id}/python-runner/detach",
          dependencies=[Depends(verify_admin)])
def admin_detach_python_runner(instance_id: int):
    """Remove the task-runner sidecar from a workspace (the rollback path for
    attach). The stack is recreated without the runners service; the DB flag
    flips off. Data is untouched."""
    inst = _runner_instance_or_404(instance_id)
    try:
        python_runner.detach_runner(inst)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("runner detach %s: %s", instance_id, e)
        raise HTTPException(502, f"Python runner detach failed: {e}")
    return {"ok": True, "instance_id": instance_id}


@app.post("/api/v1/admin/instances/{instance_id}/ai-sandbox/attach",
          dependencies=[Depends(verify_admin)])
def admin_attach_ai_sandbox(instance_id: int):
    """Give ONE workspace the shared n8n AI sandbox (n8n-sandbox provider, admin opt-in).

    Creates a sandbox tenant key for this workspace only, writes the four sandbox env vars plus the
    SearXNG URL into its stack file and redeploys in place, so workflows and credentials survive.
    The user still picks their own model in their instance's Assistant settings. Rollback = detach.
    """
    inst = _runner_instance_or_404(instance_id)
    try:
        result = ai_sandbox.attach(inst)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except ai_sandbox.SandboxError as e:
        log.error("ai sandbox attach %s: %s", instance_id, e)
        raise HTTPException(502, f"AI sandbox attach failed: {e}")
    except Exception as e:  # noqa: BLE001
        log.error("ai sandbox attach %s: %s", instance_id, e)
        raise HTTPException(502, f"AI sandbox attach failed: {e}")
    return {"ok": True, **result}


@app.post("/api/v1/admin/instances/{instance_id}/ai-sandbox/detach",
          dependencies=[Depends(verify_admin)])
def admin_detach_ai_sandbox(instance_id: int):
    """Remove the AI sandbox from a workspace and revoke its tenant key (the rollback path)."""
    inst = _runner_instance_or_404(instance_id)
    try:
        result = ai_sandbox.detach(inst)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        log.error("ai sandbox detach %s: %s", instance_id, e)
        raise HTTPException(502, f"AI sandbox detach failed: {e}")
    return {"ok": True, **result}


@app.get("/api/v1/admin/instances/{instance_id}/ai-sandbox",
         dependencies=[Depends(verify_admin)])
def admin_ai_sandbox_status(instance_id: int):
    """Whether this workspace has the sandbox, plus the shared service's reachability (no secrets)."""
    inst = _runner_instance_or_404(instance_id)
    return ai_sandbox.status(inst)


@app.get("/api/v1/admin/ai-sandbox/service", dependencies=[Depends(verify_admin)])
def admin_ai_sandbox_service():
    """Health of the shared sandbox service, so the admin UI can warn before an attach."""
    return ai_sandbox.service_health()


@app.get("/api/v1/admin/instances", dependencies=[Depends(verify_admin)])
def admin_list_instances():
    """All instances across every account, with the owning account's label, so the
    admin Backups page can offer a per-workspace trigger without needing to open
    each account page. (Steward 2026-09-03: the admin Backups page had history only.)"""
    rows = db.list_instances(account_id=None)
    out = []
    for r in rows:
        inst = _instance_to_out(r).model_dump()
        acc = db.get_account(r["account_id"])
        inst["account_email"] = acc["email"] if acc else ""
        inst["account_username"] = acc["username"] if acc else ""
        inst["account_display"] = (acc["username"] if acc else "customer #" + str(r["account_id"]))
        out.append(inst)
    return {"instances": out}


@app.get("/api/v1/admin/n8n-releases", dependencies=[Depends(verify_admin)])
def admin_n8n_releases(force: int = 0):
    """Latest official n8n release tags from Docker Hub (admin-only). Lets the
    UI show when a workspace can be updated and offer a one-click update to the
    newest release. Cached server-side for 15 minutes; force=1 bypasses."""
    try:
        return n8n_releases.latest_release(force=bool(force))
    except Exception as e:
        log.warning("n8n releases fetch failed: %s", e)
        raise HTTPException(502, "Could not fetch n8n releases from Docker Hub.")


@app.get("/api/v1/health")
def health():
    return {"ok": True, "service": "n8n-portal-backend"}


# ---------- admin security: auth events + IP bans (Steward 2026-09-03) ----------

@app.get("/api/v1/admin/security/events", dependencies=[Depends(verify_admin)])
def admin_security_events(limit: int = 100):
    """Recent auth events (logins, failures, MFA) for triage.
    SECURITY (2026-09-03): admin-only — no IP details leak publicly."""
    rows = db.list_auth_events(min(limit, 500))
    import datetime as _dt
    return {"events": [{
        "id": r["id"], "ip": r["ip"], "event": r["event"],
        "account_id": r["account_id"], "detail": r["detail"],
        "created_at": r["created_at"],
        "created_iso": _dt.datetime.utcfromtimestamp(r["created_at"]).isoformat() + "Z",
    } for r in rows]}


@app.get("/api/v1/admin/security/bans", dependencies=[Depends(verify_admin)])
def admin_security_bans():
    """List current IP bans. SECURITY (2026-09-03): admin-only."""
    rows = db.list_ip_bans()
    import datetime as _dt
    return {"bans": [{
        "ip": r["ip"], "reason": r["reason"],
        "expires_at": r["expires_at"],
        "expires_iso": (_dt.datetime.utcfromtimestamp(r["expires_at"]).isoformat() + "Z"
                        if r["expires_at"] else "permanent"),
        "created_at": r["created_at"],
    } for r in rows]}


class BanIn(BaseModel):
    ip: str = Field(..., min_length=7, max_length=45)
    hours: float = Field(..., gt=0, le=24 * 365)


@app.post("/api/v1/admin/security/bans", dependencies=[Depends(verify_admin)])
def admin_ban_ip(payload: BanIn):
    """Manually ban an IP for N hours. SECURITY (2026-09-03): admin-only."""
    ip = payload.ip.strip()
    sc.ban(ip, "manual admin ban", payload.hours)
    return {"ok": True, "ip": ip, "hours": payload.hours}


@app.delete("/api/v1/admin/security/bans/{ip}", dependencies=[Depends(verify_admin)])
def admin_unban_ip(ip: str):
    """Remove an IP ban. SECURITY (2026-09-03): admin-only."""
    ok = sc.unban(ip.strip())
    return {"ok": ok, "ip": ip.strip()}


# ---------- static UI (served at /) ----------
import os as _os

# Dockerfile copies ./static -> /app/static; app lives at /app/app
_static_dir = _os.getenv("STATIC_DIR", _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "static"))
if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="ui")
else:
    log.warning("static UI dir not found at %s", _static_dir)
