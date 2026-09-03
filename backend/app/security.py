# Minimal security helpers: admin login + JWT for admin routes.
# v1 keeps it simple: one admin account (password hash from env), clients of the
# portal don't need their own JWT yet (self-service endpoints are open until v2).

import time
import hashlib
from typing import Optional

import jwt
from fastapi import HTTPException, Header

from .config import settings


def hash_password(password: str) -> str:
    """Salted PBKDF2 hash for the admin password (stdlib only)."""
    salt = hashlib.sha256(settings.jwt_secret.encode()).hexdigest()[:16]
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def verify_admin_password(password: str) -> bool:
    expected = settings.admin_password_hash
    if not expected:
        return False
    return hash_password(password) == expected


def verify_password(password: str, expected_hash: str) -> bool:
    """Verify a PBKDF2 hash produced by hash_password() (accounts, access tokens)."""
    if not expected_hash:
        return False
    return hash_password(password) == expected_hash


def create_access_token(subject: str) -> str:
    payload = {"sub": subject, "exp": int(time.time()) + settings.jwt_expiry_hours * 3600}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_client_token(account_id: int) -> str:
    """Portal session JWT for a signed-in account."""
    payload = {"sub": f"acc:{account_id}", "exp": int(time.time()) + 30 * 24 * 3600}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_mfa_token(account_id: int, methods: list[str]) -> str:
    """Short-lived challenge JWT for the second factor (2FA). Not a session:
    it only proves the password was correct; the real token is minted after the
    code is verified. Holds the account id + allowed methods."""
    payload = {
        "sub": f"acc:{account_id}",
        "mfa": methods,
        "exp": int(time.time()) + 10 * 60,  # 10 minutes to complete the code
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_mfa_token(token: str) -> tuple[int, list[str]]:
    """Return (account_id, methods) for a valid MFA challenge, else 401."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid MFA challenge: {e}")
    sub = payload.get("sub", "")
    if not sub.startswith("acc:"):
        raise HTTPException(401, "Not a client MFA challenge.")
    try:
        account_id = int(sub.split(":", 1)[1])
    except ValueError:
        raise HTTPException(401, "Invalid MFA challenge subject.")
    return account_id, payload.get("mfa", [])


def verify_client(authorization: str | None = Header(default=None, alias="Authorization")):
    """FastAPI dependency for client (portal user) routes: requires a valid
    Bearer JWT whose sub is acc:<id>."""
    _require_jwt_secret()
    sub = _decode_subject(authorization)
    if not sub.startswith("acc:"):
        raise HTTPException(401, "Not a client token.")
    try:
        return int(sub.split(":", 1)[1])
    except ValueError:
        raise HTTPException(401, "Invalid client token subject.")


def verify_admin(authorization: str | None = Header(default=None, alias="Authorization")):
    """FastAPI dependency for admin routes.

    SECURITY FIX (2026-09-03, Steward): previously this only checked the JWT was
    validly signed — it did NOT verify the subject is 'admin', so ANY token minted
    with JWT_SECRET (including a normal customer token sub=acc:<id>) was accepted
    on every admin route: full account/backup read, lock/unlock, suspend/archive,
    quota, image update, delete. Confirmed live before the fix (customer token ->
    200 on /admin/accounts, /admin/security, /admin/backups, /admin/settings).
    Now requires sub == 'admin' (the only subject create_access_token ever mints
    for admin, verified 2026-09-03)."""
    _require_jwt_secret()
    sub = _decode_subject(authorization)
    if sub != "admin":
        raise HTTPException(401, "Admin token required.")


def _require_jwt_secret() -> None:
    if not settings.jwt_secret:
        raise HTTPException(503, "Auth not configured (JWT_SECRET missing).")


def _decode_subject(authorization: str | None) -> str:
    """Extract the JWT subject or raise 401. Both client and admin tokens use the
    same signing secret + HS256, so this centralizes token validation."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    return str(payload.get("sub", ""))


def authorize_owner_or_admin(authorization: str | None, account_id: int) -> None:
    """Allow access only if the caller is (a) the admin, or (b) the client whose
    account_id matches the resource being accessed. Used to close unauthenticated
    IDOR on /accounts/{id}, /checkout, /provision, /me/backups, etc. (2026-09-03)."""
    _require_jwt_secret()
    sub = _decode_subject(authorization)
    if sub == "admin":
        return
    if sub.startswith("acc:"):
        try:
            if int(sub.split(":", 1)[1]) == account_id:
                return
        except ValueError:
            pass
    raise HTTPException(403, "Not authorized for this account.")
