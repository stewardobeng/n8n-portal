# Portal account security: password reset + two-factor authentication (2FA).
# v1.12 (2026-09-02):
#   - Password reset: email a single-use, expiring link that lets the user set a
#     new portal password (mirrors n8n's own reset; the portal had none before).
#   - 2FA: authenticator-app TOTP, email one-time-code, or both. Enforced at
#     login (password OK -> second factor required). Setup pages for the user
#     and the admin. Backend keeps PBKDF2 hashes and TOTP secrets only.
# Passkeys (WebAuthn) are a separate, larger integration — scoped as a follow-on.

import hmac
import secrets
import string
import time

import pyotp
import qrcode

from .. import db
from ..security import hash_password, verify_password
from .emailer import send_email, EmailError
from . import passkeys

RESET_TOKEN_TTL = 3600 * 1          # reset link valid 1 hour
OTP_TTL = 300                        # email OTP valid 5 minutes
OTP_LEN = 6
RESET_LEN = 43                       # secrets.token_urlsafe(32)


def _qr_data_uri(uri: str) -> str:
    """Render an otpauth:// URI as a compact SVG QR code data URI (no Pillow)."""
    import base64
    import qrcode
    import qrcode.image.svg
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage,
                        fill_color="#0d243d", back_color="#ffffff")
    svg = img.to_string().decode()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


class SecurityError(Exception):
    pass


def _rand_otp() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(OTP_LEN))


def _new_reset_token() -> str:
    return secrets.token_urlsafe(32)


# ---------- password reset ----------

def request_password_reset(email: str) -> str | None:
    """Issue a reset token for an account (active only) and email the link.
    Returns the plaintext token (only used for the link); None for unknown or
    non-active accounts (never reveal account existence)."""
    account = db.get_account_by_email(email.strip().lower())
    if not account:
        return None
    if db._row_get(account, "account_state", "active") != "active":
        return None
    token = _new_reset_token()
    db.set_password_reset(account["id"], token, RESET_TOKEN_TTL)
    link = f"https://portal.steprotech.com/#/reset?token={token}"
    _send_reset_email(account["email"], account["username"], link)
    return token


def _send_reset_email(to: str, username: str, link: str) -> None:
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Reset your portal password</h2>
      <p>Hello <b>{username}</b>, we received a request to reset the password for
      your SteProTECH n8n portal account.</p>
      <p style="text-align:center;margin:24px 0">
        <a href="{link}" style="background:#25b4b8;color:#fff;padding:12px 22px;
           border-radius:6px;text-decoration:none;display:inline-block">
           Reset my password</a></p>
      <p>Or paste this link into your browser:</p>
      <p style="word-break:break-all;background:#f3f4f6;padding:10px;border-radius:4px">
        <code>{link}</code></p>
      <p>The link is valid for <b>1 hour</b> and can be used once. If you did not
      request this, you can safely ignore this email.</p>
    </div>
    """
    send_email(to, "Reset your SteProTECH portal password", html)


def reset_password(token: str, new_password: str) -> bool:
    """Validate a reset token (exists, unused, unexpired) and set the new
    password hash. The token is consumed on success (single-use)."""
    if len(new_password) < 8 or len(new_password) > 128:
        raise SecurityError("Password must be 8 to 128 characters.")
    row = db.get_password_reset_by_token(token)
    if not row:
        raise SecurityError("This reset link is invalid. Request a new one.")
    if not verify_password(token, row["token_hash"]):
        raise SecurityError("This reset link is invalid. Request a new one.")
    if row["used"]:
        raise SecurityError("This reset link has already been used.")
    if time.time() > row["expires_at"]:
        raise SecurityError("This reset link has expired. Request a new one.")
    account = db.get_account(row["account_id"])
    if not account or db._row_get(account, "account_state", "active") != "active":
        raise SecurityError("This account is not available for a password reset.")
    db.set_account_password(account["id"], hash_password(new_password))
    db.consume_password_reset(row["id"])
    return True


# ---------- 2FA (authenticator TOTP + email OTP) ----------

def totp_setup(account_id: int) -> dict:
    """Generate a TOTP secret for an authenticator app. Returns the otpauth URL
    + provisioning URI so the UI can render a QR code. Kept until verified."""
    account = db.get_account(account_id)
    if not account:
        raise SecurityError("Account not found.")
    secret = db._get_totp_secret(account_id)
    if not secret:
        secret = pyotp.random_base32()
        db.set_account_totp_secret(account_id, secret)
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        account["email"], issuer_name="SteProTECH Portal")
    return {"secret": secret, "uri": uri, "qr": _qr_data_uri(uri)}


def totp_verify_and_enable(account_id: int, code: str) -> bool:
    """Verify a current TOTP code against the stored secret and mark TOTP
    enabled if correct."""
    secret = db._get_totp_secret(account_id)
    if not secret:
        raise SecurityError("No authenticator secret set up yet.")
    if not pyotp.totp.TOTP(secret).verify(code, valid_window=1):
        return False
    db.set_account_totp_enabled(account_id, True)
    return True


def totp_verify_code(account_id: int, code: str) -> bool:
    """Check a TOTP code (does not modify state) — used at login."""
    secret = db._get_totp_secret(account_id)
    if not secret:
        return False
    return pyotp.totp.TOTP(secret).verify(code, valid_window=1)


def email_otp_send(account_id: int) -> dict:
    """Generate + email a 6-digit one-time code for the account's email."""
    account = db.get_account(account_id)
    if not account:
        raise SecurityError("Account not found.")
    code = _rand_otp()
    db.set_account_email_otp(account_id, hash_password(code), OTP_TTL)
    try:
        _send_otp_email(account["email"], account["username"], code)
    except EmailError:
        # roll back the stored code so a failed send can't be guessed
        db.clear_email_otp(account_id)
        raise
    return {"sent": True, "ttl_seconds": OTP_TTL}


def _send_otp_email(to: str, username: str, code: str) -> None:
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your sign-in code</h2>
      <p>Hello <b>{username}</b>, enter this code on the portal to finish signing in.</p>
      <p style="text-align:center;font-size:28px;letter-spacing:4px;padding:14px;
                background:#f3f4f6;border-radius:6px"><code>{code}</code></p>
      <p>The code expires in <b>5 minutes</b>. If you did not try to sign in, you
      can safely ignore this email.</p>
    </div>
    """
    send_email(to, "Your SteProTECH portal sign-in code", html)


def email_otp_verify(account_id: int, code: str) -> bool:
    """Verify the emailed OTP; consumed on success (single-use)."""
    row = db.get_email_otp(account_id)
    if not row:
        return False
    if time.time() > row["expires_at"]:
        return False
    if not row["otp_hash"] or not verify_password(code.strip(), row["otp_hash"]):
        return False
    db.clear_email_otp(account_id)
    return True


def has_2fa(account_id: int) -> bool:
    a = db.get_account(account_id)
    if not a:
        return False
    return bool(db._row_get(a, "totp_enabled", 0)) or bool(db._row_get(a, "email_2fa", 0))


def enabled_methods(account_id: int) -> list[dict]:
    """Second-factor methods available for an account after its password is
    accepted. Now (2026-09-03, Steward) a registered passkey is a second factor
    on the same footing as authenticator/email — it no longer replaces the
    password, it is offered *after* it at the MFA step."""
    a = db.get_account(account_id)
    out = []
    if a:
        if db._row_get(a, "totp_enabled", 0):
            out.append({"method": "totp", "label": "Authenticator app", "enabled": True})
        if db._row_get(a, "email_2fa", 0):
            out.append({"method": "email", "label": "Email code", "enabled": True})
    # passkey as a second factor (registered credential)
    if passkeys.list_passkeys_for("account", account_id):
        out.append({"method": "passkey", "label": "Passkey", "enabled": True})
    return out


# ---------- admin 2FA (single shared admin, stored in settings) ----------

ADMIN_2FA_SETTING = "admin_2fa"          # "totp,email" | "totp" | "email" | ""
ADMIN_TOTP_SECRET = "admin_totp_secret"
ADMIN_OTP_HASH = "admin_otp_hash"
ADMIN_OTP_EXPIRES = "admin_otp_expires"
ADMIN_EMAIL = "admin@steprotech.com"


def _admin_methods() -> list[dict]:
    val = db.get_setting(ADMIN_2FA_SETTING) or ""
    out = []
    if "totp" in val:
        out.append({"method": "totp", "label": "Authenticator app", "enabled": True})
    if "email" in val:
        out.append({"method": "email", "label": "Email code", "enabled": True})
    # passkey as a second factor for the admin (registered credential)
    if passkeys.list_passkeys_for("admin"):
        out.append({"method": "passkey", "label": "Passkey", "enabled": True})
    return out


def admin_2fa_state() -> dict:
    return {
        "methods": _admin_methods(),
        "totp_enabled": "totp" in (db.get_setting(ADMIN_2FA_SETTING) or ""),
        "email_2fa": "email" in (db.get_setting(ADMIN_2FA_SETTING) or ""),
        "has_totp_secret": bool(db.get_setting(ADMIN_TOTP_SECRET)),
    }


def admin_totp_setup() -> dict:
    secret = db.get_setting(ADMIN_TOTP_SECRET)
    if not secret:
        secret = pyotp.random_base32()
        db.set_setting(ADMIN_TOTP_SECRET, secret)
    uri = pyotp.totp.TOTP(secret).provisioning_uri(ADMIN_EMAIL, issuer_name="SteProTECH Portal")
    return {"secret": secret, "uri": uri, "qr": _qr_data_uri(uri)}


def admin_totp_verify_enable(code: str) -> bool:
    secret = db.get_setting(ADMIN_TOTP_SECRET)
    if not secret:
        raise SecurityError("No authenticator secret set up yet.")
    if not pyotp.totp.TOTP(secret).verify(code.strip(), valid_window=1):
        return False
    cur = db.get_setting(ADMIN_2FA_SETTING) or ""
    if "totp" not in cur:
        db.set_setting(ADMIN_2FA_SETTING, (cur + ",totp").strip(","))
    return True


def admin_totp_disable() -> None:
    cur = db.get_setting(ADMIN_2FA_SETTING) or ""
    cur = ",".join(x for x in cur.split(",") if x != "totp")
    db.set_setting(ADMIN_2FA_SETTING, cur)
    db.set_setting(ADMIN_TOTP_SECRET, "")


def admin_totp_verify_code(code: str) -> bool:
    secret = db.get_setting(ADMIN_TOTP_SECRET)
    if not secret:
        return False
    return pyotp.totp.TOTP(secret).verify(code.strip(), valid_window=1)


def admin_email_send_otp() -> dict:
    code = _rand_otp()
    db.set_setting(ADMIN_OTP_HASH, hash_password(code))
    db.set_setting(ADMIN_OTP_EXPIRES, str(int(time.time()) + OTP_TTL))
    _send_otp_email(ADMIN_EMAIL, "Administrator", code)
    return {"sent": True, "ttl_seconds": OTP_TTL}


def admin_email_otp_verify(code: str) -> bool:
    if int(db.get_setting(ADMIN_OTP_EXPIRES) or 0) < time.time():
        return False
    if not verify_password(code.strip(), db.get_setting(ADMIN_OTP_HASH) or ""):
        return False
    db.set_setting(ADMIN_OTP_HASH, "")
    db.set_setting(ADMIN_OTP_EXPIRES, "0")
    return True


def admin_email_enable() -> None:
    cur = db.get_setting(ADMIN_2FA_SETTING) or ""
    if "email" not in cur:
        db.set_setting(ADMIN_2FA_SETTING, (cur + ",email").strip(","))

