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


def verify_client(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    """FastAPI dependency for client (portal user) routes: requires a valid
    Bearer JWT whose sub is acc:<id>."""
    if not settings.jwt_secret:
        raise HTTPException(503, "Auth not configured (JWT_SECRET missing).")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    sub = payload.get("sub", "")
    if not sub.startswith("acc:"):
        raise HTTPException(401, "Not a client token.")
    try:
        account_id = int(sub.split(":", 1)[1])
    except ValueError:
        raise HTTPException(401, "Invalid client token subject.")
    return account_id


def verify_admin(authorization: Optional[str] = Header(default=None, alias="Authorization")):
    """FastAPI dependency for admin routes: requires a valid Bearer JWT."""
    if not settings.jwt_secret:
        raise HTTPException(503, "Admin auth not configured (JWT_SECRET missing).")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    try:
        jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")
    return True
