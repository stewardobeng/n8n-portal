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

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .config import settings
from . import db
from .services import provisioner, billing, access_gate, admin_ops
from .services import account_security
from .services.admin_ops import AdminOpsError
from .services.portainer_client import PortainerClient
from .services.npm_client import NPMClient
from .services.emailer import (send_welcome_credentials, send_reset_password,
                               send_access_token, EmailError)
from .security import (create_access_token, verify_admin,
                       verify_admin_password, verify_password, hash_password,
                       create_client_token, verify_client, create_mfa_token,
                       verify_mfa_token)

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
    password: str = Field(..., min_length=8, max_length=128)


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
    error: Optional[str]
    created_at: int


class AdminSettingsIn(BaseModel):
    landing_environments: str  # comma-separated env ids in fallback order, e.g. "8,4,9"


class AdminLoginIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)


class AdminLoginOut(BaseModel):
    token: str
    expires_hours: int
    mfa: Optional[dict] = None  # present when admin 2FA is required


class AccessCheckIn(BaseModel):
    email: EmailStr


class AccessCheckOut(BaseModel):
    action: str  # login | requested | waiting | token
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


class TOTPSetupIn(BaseModel):
    # code to verify (validating the secret against the authenticator)
    code: str = Field(..., min_length=6, max_length=10)


class OTPEnableIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=10)


class AdminMFAVerifyIn(BaseModel):
    challenge: str = Field(..., min_length=10, max_length=512)
    code: str = Field(..., min_length=6, max_length=10)


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
        created_at=a["created_at"], provisioned_at=a["provisioned_at"],
    )


def _instance_to_out(i) -> InstanceStatusOut:
    return InstanceStatusOut(
        id=i["id"], account_id=i["account_id"], stack_name=i["stack_name"],
        stack_id=i["stack_id"], environment_id=i["environment_id"],
        environment_name=i["environment_name"], port=i["port"], domain=i["domain"],
        status=i["status"], locked=_row_get(i, "locked", 0), error=i["error"],
        managed=_row_get(i, "managed", 1),
        created_at=i["created_at"],
    )


def _run_provision(account_id: int, password: str | None = None) -> None:
    """Background task wrapper — provisioning runs async so the API returns fast."""
    try:
        result = provisioner.provision_account(account_id, password=password)
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
def access_check(payload: AccessCheckIn):
    """Email-only first page. Branch: login | requested | waiting | token."""
    return AccessCheckOut(**access_gate.check_email(str(payload.email)))


@app.post("/api/v1/auth/login", response_model=LoginOut)
def client_login(payload: LoginIn):
    """Returning-user login: email + password -> portal session token.
    Suspended/archived accounts are blocked (admin lifecycle, 2026-09-02).
    If the account has 2FA enabled, password is checked and an MFA challenge is
    returned (no token yet) — the client must pass the second factor (2026-09-02)."""
    email = str(payload.email).lower()
    account = db.get_account_by_email(email)
    if not account:
        raise HTTPException(404, "No account for this email.")
    if not verify_password(payload.password, account["password_hash"] or ""):
        raise HTTPException(401, "Invalid email or password.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(
            403,
            "This account is " + _row_get(account, "account_state", "active") +
            ". Contact support for help.",
        )
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
def client_mfa_verify(payload: MFAVerifyIn):
    """Second factor for a 2FA account: TOTP code from the authenticator app OR
    the emailed one-time code. Returns the real portal session token."""
    email = str(payload.email).lower()
    account = db.get_account_by_email(email)
    if not account:
        raise HTTPException(404, "No account for this email.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is " +
                            _row_get(account, "account_state", "active") +
                            ". Contact support for help.")
    code = payload.code.strip()
    methods = account_security.enabled_methods(account["id"])
    if not methods:
        raise HTTPException(409, "This account does not require a second factor.")
    ok = False
    if any(m["method"] == "totp" for m in methods):
        ok = account_security.totp_verify_code(account["id"], code)
    if not ok and any(m["method"] == "email" for m in methods):
        ok = account_security.email_otp_verify(account["id"], code)
    if not ok:
        raise HTTPException(401, "That code is invalid or has expired.")
    return LoginOut(
        token=create_client_token(account["id"]),
        account=_account_to_out(account),
    )


@app.post("/api/v1/auth/mfa-send-otp", response_model=dict)
def client_mfa_send_otp(payload: AccessCheckIn):
    """Resend the emailed one-time code for a 2FA account (after login)."""
    account = db.get_account_by_email(str(payload.email).lower())
    if not account:
        raise HTTPException(404, "No account for this email.")
    if _row_get(account, "account_state", "active") != "active":
        raise HTTPException(403, "This account is " +
                            _row_get(account, "account_state", "active") +
                            ". Contact support for help.")
    if not _row_get(account, "email_2fa", 0):
        raise HTTPException(409, "Email 2FA is not enabled for this account.")
    return account_security.email_otp_send(account["id"])


@app.post("/api/v1/auth/forgot-password", response_model=dict)
def forgot_password(payload: ForgotPasswordIn):
    """Email a single-use reset link. Always returns 200 so attackers cannot
    enumerate which emails have accounts. Mail failures are logged, not surfaced
    (the response must not reveal whether an account or SMTP problem occurred)."""
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
def reset_password(payload: ResetPasswordIn):
    """Consume a reset token and set a new portal password."""
    try:
        account_security.reset_password(payload.token, payload.password)
    except account_security.SecurityError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/v1/auth/verify-token", response_model=AccessCheckOut)
def verify_access_token(payload: TokenVerifyIn):
    """Visitor enters email + admin-issued token; verified -> registration opens."""
    if not access_gate.verify_token(str(payload.email), payload.token):
        raise HTTPException(401, "Invalid or expired access token.")
    return AccessCheckOut(action="verified", email=str(payload.email).lower())


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


# ---------- client endpoints ----------

@app.post("/api/v1/accounts", response_model=dict, status_code=201)
def create_account(payload: AccountCreate):
    """Registration is GATED: requires a verified access token for this email
    (Steward 2026-09-01). Returns the account + a portal session token."""
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
              payload: Optional[ProvisionRequest] = None):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
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
    background.add_task(_run_provision, account_id, password)
    return {"status": "provisioning_started", "account_id": account_id}


@app.get("/api/v1/accounts/{account_id}", response_model=dict)
def account_status(account_id: int):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    instances = db.list_instances(account_id)
    return {
        "account": _account_to_out(account).model_dump(),
        "instances": [_instance_to_out(i).model_dump() for i in instances],
    }


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
    return {"plans": plans_out, "gateway": billing.gateway()}


@app.post("/api/v1/accounts/{account_id}/checkout", response_model=CheckoutOut)
def create_checkout(account_id: int):
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found.")
    if account["status"] == "provisioned" and account["subscription_status"] == "active":
        raise HTTPException(409, "Already subscribed and provisioned.")
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
async def mock_webhook(request: Request):
    """E2E helper: POST {"mock": true, "type": "charge.success",
    "data": {"metadata": {"account_id": "5"}}} etc. Only active when
    PAYMENT_GATEWAY=mock."""
    payload = await request.body()
    try:
        result = billing.handle_webhook("mock", payload, None)
    except Exception as e:
        raise HTTPException(400, str(e))
    return result


@app.get("/api/v1/environments")
def environments():
    pc = PortainerClient()
    return pc.list_endpoints()


# ---------- admin endpoints ----------

@app.post("/api/v1/admin/login", response_model=AdminLoginOut)
def admin_login(payload: AdminLoginIn):
    if not verify_admin_password(payload.password):
        raise HTTPException(401, "Invalid admin password.")
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
def admin_mfa_verify(payload: AdminMFAVerifyIn):
    """Admin second factor after the password. Verifies the MFA challenge token
    (proves the password was correct) + the code, then issues the admin JWT."""
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
        raise HTTPException(401, "That code is invalid or has expired.")
    return AdminLoginOut(
        token=create_access_token("admin"),
        expires_hours=settings.jwt_expiry_hours,
    )


@app.get("/api/v1/admin/security", dependencies=[Depends(verify_admin)])
def admin_security_state():
    return account_security.admin_2fa_state()


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


@app.get("/api/v1/admin/settings", dependencies=[Depends(verify_admin)])
def get_admin_settings():
    return {"landing_environments": db.get_setting("landing_environments", default="8")}


@app.put("/api/v1/admin/settings", dependencies=[Depends(verify_admin)])
def put_admin_settings(payload: AdminSettingsIn):
    ids = [x.strip() for x in payload.landing_environments.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(422, "Provide at least one environment id.")
    db.set_setting("landing_environments", ",".join(ids))
    return {"landing_environments": ",".join(ids)}


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


@app.get("/api/v1/health")
def health():
    return {"ok": True, "service": "n8n-portal-backend"}


# ---------- static UI (served at /) ----------
import os as _os

# Dockerfile copies ./static -> /app/static; app lives at /app/app
_static_dir = _os.getenv("STATIC_DIR", _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "static"))
if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="ui")
else:
    log.warning("static UI dir not found at %s", _static_dir)
