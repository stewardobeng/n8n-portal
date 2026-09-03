# WebAuthn passkeys for the portal (Steward 2026-09-03).
#
# Passkeys let a user (or the admin) sign in with a platform/roaming authenticator
# instead of a password. This is a FIRST-factor login option, not a second factor:
# the WebAuthn assertion replaces the password. The RP (relying party) is
# portal.steprotech.com (the portal origin). The existing 2FA (TOTP/email) gate
# still runs afterwards for accounts that have it enabled — a passkey does NOT
# bypass an enabled 2FA; the session token is only issued once the full flow
# (passkey [+ 2FA challenge if configured]) completes.
#
# We store only the public key + a signature counter (never the private key, never
# a password). Challenges are stored server-side, consumed single-use.

import json
import time

from webauthn import (generate_registration_options,
                      verify_registration_response,
                      generate_authentication_options,
                      verify_authentication_response)
from webauthn.helpers import (bytes_to_base64url, base64url_to_bytes,
                              generate_challenge, options_to_json_dict)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement,
                                      PublicKeyCredentialDescriptor)

from .. import db
from ..config import settings

# Relaying party identity. Must match the origin the browser sees the app at.
RP_ID = getattr(settings, "rp_id", "portal.steprotech.com")
RP_NAME = "SteProTECH Portal"
ORIGINS = ["https://portal.steprotech.com"]
# allow HTTP for local/loopback testing (127.0.0.1 dev servers, ui-smoke)
ORIGINS = list(dict.fromkeys(ORIGINS + ["http://127.0.0.1:8798",
                                        "http://127.0.0.1:8000",
                                        "http://localhost:8798",
                                        "http://localhost:8000"]))
CHALLENGE_TTL = 600  # seconds


class PasskeyError(Exception):
    pass


def _scope(admin: bool) -> str:
    return "admin" if admin else "account"


def _origin_check(actual_origin: str | None = None) -> list[str]:
    """The expected_origin list the library checks against. The library parses the
    real origin from the credential's clientData — we only supply the whitelist."""
    return ORIGINS


# ---------- registration ----------

def registration_options(scope: str, account_id: int | None, email: str) -> dict:
    """Generate a fresh publicKey.create() options object + store the challenge."""
    user_handle = generate_challenge()
    opts = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_handle,
        user_name=email,
        user_display_name=email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    db.save_passkey_challenge(scope, account_id, opts.challenge, "register", CHALLENGE_TTL)
    return options_to_json_dict(opts)


def verify_registration(scope: str, account_id: int | None, credential_json: dict) -> dict:
    """Verify a publicKey.create() response, store the credential, return a summary."""
    challenge_row = db.get_latest_passkey_challenge(scope, account_id, "register")
    if not challenge_row:
        raise PasskeyError("No registration challenge. Start the setup again.")
    if time.time() > challenge_row["expires_at"]:
        db.consume_passkey_challenge(challenge_row["id"])
        raise PasskeyError("Registration challenge expired. Start the setup again.")
    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=challenge_row["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=_origin_check(credential_json.get("origin", "https://portal.steprotech.com")),
            require_user_verification=False,
        )
    except Exception as e:
        db.consume_passkey_challenge(challenge_row["id"])
        raise PasskeyError(f"Could not verify this passkey: {e}")
    db.consume_passkey_challenge(challenge_row["id"])

    cred = verified.credential_id
    cred_id_b64 = bytes_to_base64url(cred)
    transports = ",".join(t.value if hasattr(t, "value") else str(t)
                          for t in (getattr(verified, "transports", None) or []))
    db.add_passkey(scope, account_id, cred_id_b64,
                   verified.credential_public_key, verified.sign_count,
                   transports, "Passkey")
    return {"registered": True, "credential_id": cred_id_b64,
            "sign_count": verified.sign_count}


# ---------- authentication ----------

def authentication_options(scope: str, account_id: int | None) -> dict:
    """Generate a fresh publicKey.get() options object for an account with a
    registered passkey, and store the challenge."""
    creds = db.list_passkeys(scope, account_id) if not scope == "admin" else db.list_passkeys(scope)
    if not creds:
        raise PasskeyError("No passkey registered for this account.")
    allow = [PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(c["credential_id"])) for c in creds]
    opts = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    db.save_passkey_challenge(scope, account_id, opts.challenge, "login", CHALLENGE_TTL)
    return options_to_json_dict(opts)


def verify_authentication(scope: str, account_id: int | None, credential_json: dict) -> dict:
    """Verify a publicKey.get() assertion, update the signature counter, and
    return the credential_id + owning account_id. For account scope the caller may
    pass account_id=None and let us resolve the owner from the credential row."""
    cred_id_b64 = credential_json.get("id", "") or credential_json.get("rawId", "")
    stored = db.get_passkey_by_credential_id(cred_id_b64)
    if not stored:
        raise PasskeyError("This passkey is not registered.")
    if stored["scope"] != scope:
        raise PasskeyError("This passkey does not belong to this account.")
    # Resolve the owning account id for account-scope passkeys
    if scope == "account":
        account_id = stored["account_id"]
    challenge_row = db.get_latest_passkey_challenge(scope, account_id, "login")
    if not challenge_row:
        raise PasskeyError("No sign-in challenge. Start signing in again.")
    if time.time() > challenge_row["expires_at"]:
        db.consume_passkey_challenge(challenge_row["id"])
        raise PasskeyError("Sign-in challenge expired. Start signing in again.")
    try:
        verified = verify_authentication_response(
            credential=credential_json,
            expected_challenge=challenge_row["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=_origin_check(),
            credential_public_key=stored["public_key"],
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=False,
        )
    except Exception as e:
        db.consume_passkey_challenge(challenge_row["id"])
        raise PasskeyError(f"Could not verify the passkey signature: {e}")
    db.consume_passkey_challenge(challenge_row["id"])
    # Update the signature counter (clone/prevention)
    conn = db.get_conn()
    try:
        conn.execute("UPDATE passkeys SET sign_count=? WHERE credential_id=?",
                     (verified.new_sign_count, cred_id_b64))
        conn.commit()
    finally:
        conn.close()
    return {"verified": True, "account_id": stored["account_id"] if scope == "account" else None}


def list_passkeys_for(scope: str, account_id: int | None = None) -> list[dict]:
    rows = db.list_passkeys(scope, account_id) if scope != "admin" else db.list_passkeys(scope)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "credential_id": r["credential_id"],
            "name": r["name"] or "Passkey",
            "transports": r["transports"],
            "sign_count": r["sign_count"],
            "created_at": r["created_at"],
        })
    return out


def delete_passkey_for(scope: str, account_id: int | None, credential_id: str) -> bool:
    return db.delete_passkey(scope, credential_id, account_id)
