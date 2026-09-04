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
import time as _time
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .config import settings
from . import db
from .services import provisioner, billing, access_gate, admin_ops, backup_ops
from .services import account_security, passkeys, n8n_releases
from .services import security_controls as sc
from .services.admin_ops import AdminOpsError
from .services.portainer_client import PortainerClient
from .services.npm_client import NPMClient
from .services.emailer import (send_welcome_credentials, send_reset_password,
                               send_access_token, EmailError)
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
    subscription_status: str = "none"
    paid_until: Optional[int] = None
    paid_from: Optional[int] = None
    account_state: str = "active"  # active | suspended | archived
    backup_enabled: bool = False   # customer may self-service backup (admin-set)
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
    error: Optional[str]
    created_at: int


class AdminSettingsIn(BaseModel):
    # comma-separated env ids in fallback order, e.g. "8,4,9"; optional so a
    # caller can update ONLY the payments switch without touching placement.
    landing_environments: Optional[str] = None
    payments_open: Optional[bool] = None


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
    return AccountOut(
        id=a["id"], email=a["email"], username=a["username"],
        display_name=a["display_name"], status=a["status"],
        quota=_row_get(a, "quota", 1),
        subscription_status=_row_get(a, "subscription_status", "none"),
        paid_until=_row_get(a, "paid_until"),
        paid_from=_row_get(a, "paid_from"),
        account_state=_row_get(a, "account_state", "active"),
        backup_enabled=bool(_row_get(a, "backup_enabled", 0)),
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
                   workspace: dict | None = None) -> None:
    """Background task wrapper — provisioning runs async so the API returns fast."""
    try:
        result = provisioner.provision_account(account_id, password=password,
                                               workspace=workspace)
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
    return {
        "account": _account_to_out(account).model_dump(),
        "instances": [_instance_to_out(i).model_dump() for i in instances],
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
    """Registration is GATED: requires a verified access token for this email
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
    # gate: a token must have been issued AND verified for this email
    req = db.get_access_request(email)
    if not req or req["status"] != "token_sent":
        raise HTTPException(403, "Access token required. Request access from an administrator first.")
    if not req["token_hash"] or req["token_expires_at"] and _time.time() > req["token_expires_at"]:
        raise HTTPException(403, "Access token expired. Request a new one from an administrator.")
    # require the caller to prove token possession: the UI verified it first and
    # we re-verify here with the plaintext token carried by the registration form.
    if not payload.access_token:
        raise HTTPException(403, "Access token required.")
    if not access_gate.verify_token(email, payload.access_token):
        raise HTTPException(401, "Invalid access token for this email.")
    username = provisioner.ensure_unique_username(payload.username, email)
    try:
        provisioner.validate_password_policy(payload.password)
    except provisioner.ProvisionError as e:
        raise HTTPException(422, str(e))
    account_id = db.create_account(
        email, username, payload.display_name or "",
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
                workspace["username"] = provisioner.validate_username(payload.username)
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
    # Quota gate: one instance per account unless the admin raised the quota
    # (provisioner enforces the same rule; this gives the API a fast 409).
    quota = account["quota"] if "quota" in account.keys() else settings.default_quota
    live = db.count_instances(account_id)
    if live >= quota:
        raise HTTPException(409, f"Instance quota reached ({live}/{quota}). Contact an administrator to increase your quota.")
    # PAYMENT GATE: instances are only created after the annual subscription is
    # active (webhook sets subscription_status=active on charge.success).
    if billing.gateway() in ("paystack", "stripe") and account["subscription_status"] != "active":
        raise HTTPException(402, "Payment required: subscribe before provisioning.")
    password = payload.password if payload else None
    background.add_task(_run_provision, account_id, password, workspace)
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
    return {
        "account": _account_to_out(account).model_dump(),
        "instances": [_instance_to_out(i).model_dump() for i in instances],
    }


@app.get("/api/v1/username-available", dependencies=[Depends(verify_client)])
def check_username_available(username: str = ""):
    """Live uniqueness check for a NEW workspace username (extra workspace
    form). Returns immediately so the UI can alert the customer the moment the
    username they typed is not available."""
    uname = (username or "").strip().lower()
    available, message = provisioner.username_available(uname)
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
    """Both annual plans (Steward 2026-09-01): GHS 300 active, GHS 500
    inactive until the special compose is built. Kept in GHS for now."""
    plans_out = [{
        "name": settings.plan_name,
        "currency": settings.plan_currency,
        "amount_minor": settings.plan_amount_minor,
        "interval": "annually",
        "active": True,
    }]
    plans_out.append({
        "name": settings.plan_b_name,
        "currency": settings.plan_currency,
        "amount_minor": settings.plan_b_amount_minor,
        "interval": "annually",
        "active": settings.plan_b_active,
    })
    return {"plans": plans_out, "gateway": billing.gateway(),
            "payments_open": billing.payments_open()}


@app.post("/api/v1/accounts/{account_id}/checkout", response_model=CheckoutOut)
def create_checkout(account_id: int,
                    authorization: str | None = Header(default=None, alias="Authorization")):
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
    try:
        return billing.create_checkout(account_id, account["email"])
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/v1/webhook/paystack")
async def paystack_webhook(request: Request):
    """Paystack webhook endpoint. Signature: x-paystack-signature (HMAC-SHA512)."""
    payload = await request.body()
    signature = request.headers.get("x-paystack-signature")
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
    # Admin 2FA gate
    methods = account_security.admin_2fa_state()["methods"]
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
    if not ok and any(m["method"] == "email" for m in enabled):
        ok = account_security.admin_email_otp_verify(payload.code)
    if not ok:
        sc.record_failed(ip, "mfa_fail", None)
        raise HTTPException(401, "That code is invalid or has expired.")
    sc.record_ok(ip, "login_ok", None)
    return AdminLoginOut(
        token=create_access_token("admin"),
        expires_hours=settings.jwt_expiry_hours,
    )


@app.post("/api/v1/admin/mfa-send-otp", response_model=dict)
def admin_mfa_send_otp(request: Request):
    """Resend the emailed one-time code for the admin second-factor step.
    (Steward 2026-09-03: admin now has a real post-password MFA flow.)"""
    ip = sc.client_ip(request)
    if sc.check_rate(ip, "forgot"):
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    enabled = account_security.admin_2fa_state()["methods"]
    if not any(m["method"] == "email" for m in enabled):
        raise HTTPException(409, "Email 2FA is not enabled for the admin.")
    return account_security.admin_email_send_otp()


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
    return {
        "landing_environments": db.get_setting("landing_environments", default="8"),
        "payments_open": billing.payments_open(),
    }


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


@app.post("/api/v1/admin/access-requests/{request_id}/token", dependencies=[Depends(verify_admin)])
def admin_issue_token(request_id: int):
    """Generate a fresh access token for a request, email it to the person, and
    return it once so the admin can also share it directly (WhatsApp etc.)."""
    try:
        token = access_gate.issue_token(request_id)
    except access_gate.AccessGateError as e:
        raise HTTPException(409, str(e))
    req = db.get_access_request_by_id(request_id)
    email = req["email"] if req else ""
    if email:
        try:
            send_access_token(email, token)
        except EmailError as e:
            log.warning(f"Access-token email failed for {email}: {e}")
    return {"request_id": request_id, "email": email,
            "token": token, "expires_hours": access_gate.TOKEN_VALID_HOURS}


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


@app.put("/api/v1/admin/accounts/{account_id}/quota", dependencies=[Depends(verify_admin)])
def admin_set_quota(account_id: int, payload: QuotaIn):
    """Admin raises/lowers how many n8n instances this account may provision."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Account not found.")
    db.set_account_quota(account_id, payload.quota)
    return {"account_id": account_id, "quota": payload.quota}


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
    unpaid + instance stopped (backdated already-expired accounts)."""
    try:
        result = admin_ops.mark_paid(account_id, payload.paid_until or 0,
                                     payload.paid_from)
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


@app.post("/api/v1/admin/instances/{instance_id}/update-image",
          dependencies=[Depends(verify_admin)])
def admin_update_image(instance_id: int, payload: UpdateImageIn):
    """Change the n8n image tag on an instance and redeploy its stack container.
    The stack is recreated against the same volume (no data loss). Roll back by
    setting the previous image tag."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    try:
        new_image = backup_ops.update_instance_image(inst, payload.image)
    except ValueError as e:
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
        raise HTTPException(502, f"Image update failed: {e}")
    return {"ok": True, "instance_id": instance_id, "image": new_image}


@app.get("/api/v1/admin/instances/{instance_id}/image", dependencies=[Depends(verify_admin)])
def admin_current_image(instance_id: int):
    """Report the instance's current running n8n image tag (for the UI)."""
    inst = db.get_instance(instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found.")
    return {"image": backup_ops.current_image(inst)}


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
