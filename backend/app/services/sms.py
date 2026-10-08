"""SMS delivery through mNotify - admin one-time codes (portal 0.1.90).

Steward (2026-10-07): "add sms otp as one of the options for 2fa for the admin account ...
we will use mnotify for the sms sending and i will provide the api, so implement it."

The API key and the Sender ID are entered from the admin Settings page and live in the
settings store. They are never echoed back to a browser, never written to a log and never
part of an error message: a settings read returns only whether a key is stored and its
last four characters.

mNotify v2 (developer.bms.africa/openapi14.yaml), all responses JSON:

  send      POST /api/sms/quick?key=KEY
            {"recipient": ["0241234567"], "sender": "StePro-OTP", "message": "...",
             "is_schedule": false, "schedule_date": ""}
            success == status "success" AND code 2000 (message "messages sent successfully")
  sender    POST /api/senderid/status/?key=KEY   {"sender_name": "StePro-OTP"}
  balance   GET  /api/balance/sms?key=KEY

Two things that matter here:

1. httpx logs every request at INFO *with the full URL*, and mNotify takes the key as a
   query parameter. _quiet_httpx() raises that logger's level for the duration of a call;
   without it the key would end up in `docker logs`.
2. The `sms_type: "otp"` field buys better routing for codes but costs an extra 0.035 per
   campaign, so it is off unless Steward switches it on in Settings.
"""
from __future__ import annotations

import datetime
import logging
import re
from contextlib import contextmanager

import httpx

from app import db

API_BASE = "https://api.mnotify.com/api"
DEFAULT_SENDER = "StePro-OTP"         # Steward's approved Sender ID (2026-10-07)
SENDER_MAX = 11                     # mNotify rejects a Sender ID longer than this
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

SMS_KEY = "sms_api_key"             # stored in the settings table, write-only in the UI
SMS_SENDER = "sms_sender_id"
SMS_OTP_TYPE = "sms_otp_type"       # "otp" -> send sms_type:"otp" (costs 0.035 extra)


class SmsError(Exception):
    """Anything that stops an SMS from going out, with a message safe to show."""


@contextmanager
def _quiet_httpx():
    logger = logging.getLogger("httpx")
    old = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(old)


# ---------------------------------------------------------------- configuration

def api_key() -> str:
    return (db.get_setting(SMS_KEY) or "").strip()


def sender_id() -> str:
    """The approved Sender ID, falling back to mNotify's default."""
    return (db.get_setting(SMS_SENDER) or "").strip() or DEFAULT_SENDER


def otp_typing() -> bool:
    """True when Steward paid the extra 0.035/campaign for OTP routing."""
    return (db.get_setting(SMS_OTP_TYPE) or "").strip() == "otp"


def configured() -> bool:
    return bool(api_key())


def masked_key() -> dict:
    """What a settings read may reveal about the stored key. Never the key itself."""
    key = api_key()
    return {"sms_api_key_set": bool(key), "sms_api_key_last4": key[-4:] if key else ""}


def validate_sender(name: str) -> str:
    """mNotify: sender must be at most 11 characters. Steward's is 'StePro-OTP'."""
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) > SENDER_MAX:
        raise SmsError("A Sender ID can be at most %d characters (yours is %d)."
                       % (SENDER_MAX, len(name)))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]*", name):
        raise SmsError("A Sender ID may only contain letters, numbers, spaces, dots, "
                       "dashes and underscores.")
    return name


# ---------------------------------------------------------------- phone numbers

def normalise_phone(raw: str) -> str | None:
    """Return the local 0XXXXXXXXX form mNotify expects, or None if it cannot be one.

    Accepts what a Ghanaian admin would actually type: 0241234567, 241234567,
    233241234567, +233241234567, 00233241234567, with spaces/dashes/brackets anywhere.
    """
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))
    if digits.startswith("00233"):
        digits = digits[5:]
    elif digits.startswith("233"):
        digits = digits[3:]
    if len(digits) == 9:
        digits = "0" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return None


def pretty_phone(raw: str) -> str:
    """A number for display: 024 123 4567, or the raw value if it is not a number."""
    local = normalise_phone(raw)
    if not local:
        return raw or ""
    return "%s %s %s" % (local[:3], local[3:6], local[6:])


def mask_phone(raw: str) -> str:
    """Ends only, so a screen share or a screenshot cannot leak the whole number."""
    local = normalise_phone(raw)
    if not local:
        return ""
    return local[:3] + " *** " + local[-2:]


# ---------------------------------------------------------------- transport

def _request(method: str, path: str, *, json_body=None, params=None) -> dict:
    """One mNotify call. Extra query parameters MUST come through `params`.

    Never bake "?from=..&to=.." into `path`: httpx percent-encodes the "?" in a path and mNotify
    answers `422 Validation error`, which is exactly how the delivery lookup first shipped broken
    (0.1.92), with the unit test blind to it because it stubbed the URL away.
    """
    key = api_key()
    if not key:
        raise SmsError("No mNotify API key has been saved yet. Add it in Settings.")
    url = "%s/%s" % (API_BASE, path.lstrip("/"))
    query = {"key": key}
    if params:
        query.update(params)
    try:
        with _quiet_httpx():
            if method == "GET":
                res = httpx.get(url, params=query, timeout=_TIMEOUT)
            else:
                res = httpx.post(url, params=query, json=json_body or {},
                                 timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        # str(e) can carry the URL with the key, so the class name only.
        raise SmsError("Could not reach mNotify (%s). Check the server's internet access."
                       % type(e).__name__) from None
    try:
        data = res.json()
    except ValueError:
        raise SmsError("mNotify answered with something that was not JSON (%d)."
                       % res.status_code) from None
    if not isinstance(data, dict):
        raise SmsError("mNotify answered in an unexpected shape (%d)." % res.status_code)
    # /sms/quick answers {status, code:"2000", ...}; /balance/sms answers {status, balance}
    # with no code at all, so a code is only wrong when one is actually present.
    status = str(data.get("status", "")).lower()
    code = str(data.get("code", "") or "").strip()
    if status != "success" or (code and code != "2000"):
        detail = str(data.get("message") or data.get("error") or "").strip()
        raise SmsError(detail or "mNotify refused the request (code %s)."
                       % (code or res.status_code))
    return data


# ---------------------------------------------------------------- public actions

def send_sms(to: str, message: str, *, otp: bool = False) -> dict:
    """Send one SMS. Returns mNotify's summary (campaign id, credits used/left)."""
    local = normalise_phone(to)
    if not local:
        raise SmsError("That phone number does not look like a Ghana number. "
                       "Use 024 123 4567 or +233 24 123 4567.")
    if not (message or "").strip():
        raise SmsError("Nothing to send.")
    body = {
        "recipient": [local],
        "sender": sender_id(),
        "message": message.strip(),
        "is_schedule": False,
        "schedule_date": "",
    }
    if otp and otp_typing():
        body["sms_type"] = "otp"
    data = _request("POST", "sms/quick", json_body=body)
    summary = data.get("summary") or {}
    return {
        "sent": True,
        "to": mask_phone(local),
        "campaign_id": summary.get("_id") or "",
        "credit_used": summary.get("credit_used"),
        "credit_left": summary.get("credit_left"),
        "sender": body["sender"],
        "typed_as_otp": "sms_type" in body,
    }


def otp_message(code: str, *, minutes: int) -> str:
    """The admin sign-in code message (0.1.91, Steward 2026-10-07: "don't you think its the
    content that makes them suspect its otp, so lets make it normal, it must not be numbers
    the otp can just be letters or so").

    A text that opens with a bare number reads as an OTP blast to the carrier, so the
    letters-only code sits inside a plain sentence instead. Kept short: SMS is charged per
    160 characters.
    """
    return ("SteProTECH: your admin sign-in code is %s. It expires in %d minutes."
            % (code, int(minutes)))


def send_otp_code(to: str, code: str, *, minutes: int) -> dict:
    return send_sms(to, otp_message(code, minutes=minutes), otp=True)


def check_sender(name: str | None = None) -> dict:
    """Ask mNotify whether our Sender ID is approved (Approved / Pending / Rejected)."""
    wanted = validate_sender(name or sender_id())
    data = _request("POST", "senderid/status/", json_body={"sender_name": wanted})
    summary = data.get("summary") or {}
    state = str(summary.get("status") or summary.get("sender_status") or "").strip()
    purpose = str(summary.get("purpose") or "").strip()
    return {
        "sender": str(summary.get("sender_name") or wanted),
        "status": state,
        "approved": state.lower() == "approved",
        # The purpose decides which traffic the gateway lets through, so never judge delivery
        # by "approved" alone: an OTP-purpose sender rejects ordinary plain messages.
        "purpose": purpose,
        "otp_purpose": "otp" in purpose.lower(),
        "message": str(data.get("message") or "").strip(),
    }


def delivery_report(days: int = 2) -> list:
    """mNotify's own delivery report: one row per recipient per send.

    This is the only honest way to call an SMS delivered: `status` is DELIVERED, SUBMITTED (sent,
    not yet acknowledged) or REJECTED (the gateway dropped it after accepting and charging).
    """
    today = datetime.date.today()
    rows = []
    for back in range(max(1, days)):
        day = (today - datetime.timedelta(days=back)).isoformat()
        data = _request("GET", "report", params={"from": day, "to": day})
        rows.extend(data.get("report") or [])
    return rows


def delivery_status(campaign_id: str, days: int = 2) -> dict:
    """What happened to a campaign we just sent? Used by the Settings card's test button."""
    if not campaign_id:
        return {"found": False, "status": "", "reason": "No campaign id to look up."}
    try:
        rows = delivery_report(days=days)
    except SmsError as e:
        return {"found": False, "status": "", "reason": str(e)}
    for row in rows:
        if str(row.get("campaign_id")) == str(campaign_id):
            return {"found": True, "status": str(row.get("status") or "").upper(),
                    "sender": str(row.get("sender") or ""), "date_sent": str(row.get("date_sent") or ""),
                    "reason": ""}
    return {"found": False, "status": "",
            "reason": "mNotify has no report for it yet. Give it a moment."}


def sms_balance() -> dict:
    data = _request("GET", "balance/sms")
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    # "balance" is the SMS credit; "wallet" is a separate purse and the OTP route
    # (sms_type:"otp") bills 0.035 per campaign from it. An empty wallet is why an
    # OTP-typed send fails while SMS credits look healthy.
    return {"balance": _num(data.get("balance")),
            "bonus": _num(data.get("bonus") or data.get("bonus_balance")),
            "wallet": _num(data.get("wallet"))}
