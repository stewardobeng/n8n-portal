# Admin-gated portal onboarding (access tokens).
#
# Flow (Steward's design, 2026-09-01):
#   1. Visitor enters ONLY an email on the portal's first (login) page.
#   2. The backend decides the branch:
#        - account exists            -> action "login"     (password login)
#        - no account, no request    -> creates an access request, action "requested"
#        - request pending, no token -> action "waiting"   (admin hasn't issued yet)
#        - token issued, unused      -> action "token"     (enter the token)
#   3. Admin sees the request on their dashboard and issues a token (this module
#      generates it; the admin UI shows it and emails it to the person).
#   4. Visitor re-enters email -> gets "token" action -> enters token -> verified
#      -> the registration form opens (email locked).
#   5. Registration consumes the token; the visitor is auto-logged-in. Returning
#      users always take the "login" branch (email + password), never a token.

import secrets
import string
import time

from .. import db
from ..security import hash_password, verify_password

TOKEN_VALID_HOURS = 72


class AccessGateError(Exception):
    pass


def _generate_token() -> str:
    """8-char uppercase alphanumeric token (easy to read out loud), e.g. K7FQ-2MXP."""
    alphabet = string.ascii_uppercase + string.digits
    # drop lookalikes 0/O/1/I for clarity
    alphabet = alphabet.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def check_email(email: str) -> dict:
    """Branch decision for the email-only first page."""
    email = email.strip().lower()
    if db.get_account_by_email(email):
        return {"action": "login", "email": email}
    req = db.get_access_request(email)
    if not req:
        db.create_access_request(email)
        return {"action": "requested", "email": email}
    if req["status"] == "registered":
        # registered but account missing is an inconsistency; treat as new request
        db.update_access_request(req["id"], status="requested", token_hash="",
                                 token_sent_at=None, token_expires_at=None)
        return {"action": "requested", "email": email}
    if req["status"] == "canceled":
        db.update_access_request(req["id"], status="requested", token_hash="")
        return {"action": "requested", "email": email}
    if req["status"] == "token_sent":
        return {"action": "token", "email": email}
    return {"action": "waiting", "email": email}


def issue_token(request_id: int) -> str:
    """Admin action: generate a fresh token for the request, hash it, stamp expiry.
    Returns the PLAINTEXT token once (the UI shows it to the admin and emails it)."""
    req = None
    for r in db.list_access_requests():
        if r["id"] == request_id:
            req = r
            break
    if not req:
        raise AccessGateError("Access request not found.")
    if req["status"] == "registered":
        raise AccessGateError("This email has already registered.")
    token = _generate_token()
    db.update_access_request(
        request_id,
        status="token_sent",
        token_hash=hash_password(token),
        token_sent_at=int(time.time()),
        token_expires_at=int(time.time()) + TOKEN_VALID_HOURS * 3600,
    )
    return token


def verify_token(email: str, token: str) -> bool:
    """Visitor enters email + token; true only if the token matches, is unused,
    and not expired."""
    email = email.strip().lower()
    req = db.get_access_request(email)
    if not req or req["status"] != "token_sent":
        return False
    if not req["token_hash"]:
        return False
    expires = req["token_expires_at"] or 0
    if int(time.time()) > expires:
        return False
    return verify_password(token.strip(), req["token_hash"])


def consume(email: str) -> None:
    """Mark the request registered after a successful account creation."""
    req = db.get_access_request(email)
    if req:
        db.update_access_request(req["id"], status="registered",
                                 registered_at=int(time.time()))
