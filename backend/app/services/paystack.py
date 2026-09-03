# Paystack payment client (PRIMARY gateway, GHS annual plans).
# Docs reference: https://paystack.com/docs/api
# - Plans with interval=annually give native auto-renewal: Paystack re-charges
#   the customer's saved card each year and emits charge.success on each renewal.
# - Webhooks (configured in the Paystack dashboard to
#   https://portal.steprotech.com/api/v1/webhook/paystack):
#     charge.success            -> activate/extend subscription, unlock, provision if pending
#     invoice.payment_failed    -> mark past_due, start grace window
#     subscription.disable      -> canceled, lock immediately
#     subscription.not_renew    -> customer turned off auto-renew; lock at period end
#   Signature: x-paystack-signature = HMAC-SHA512 hex of the raw body, keyed by
#   the secret key.

import hashlib
import hmac
import json
import logging
import time
import urllib.request
import urllib.error

from .. import db
from ..config import settings

log = logging.getLogger("n8n-portal")

PAYSTACK_API = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def _headers() -> dict:
    if not settings.paystack_secret_key:
        raise PaystackError("PAYSTACK_SECRET_KEY not configured.")
    return {
        "Authorization": f"Bearer {settings.paystack_secret_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Cloudflare on api.paystack.co Error-1010-blocks the default
        # Python-urllib User-Agent (bot signature). Send a browser UA so the
        # server-side client is not rejected. (2026-09-03, verified live.)
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{PAYSTACK_API}{path}", data=data, headers=_headers(), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            raise PaystackError(f"Paystack HTTP {e.code}")
    if not payload.get("status"):
        raise PaystackError(payload.get("message") or "Paystack request failed")
    return payload


def ensure_plan() -> str:
    """Return the annual plan code, creating the plan if not configured/existing."""
    if settings.paystack_plan_code:
        return settings.paystack_plan_code
    # look for an existing plan with our name first (idempotent)
    try:
        plans = _req("GET", "/plan?perPage=100")
        for p in plans.get("data", []):
            if (p.get("name") == settings.plan_name
                    and p.get("interval") == "annually"
                    and p.get("currency") == settings.plan_currency):
                return p["plan_code"]
    except PaystackError:
        pass
    created = _req("POST", "/plan", {
        "name": settings.plan_name,
        "amount": settings.plan_amount_minor,
        "interval": "annually",
        "currency": settings.plan_currency,
    })
    code = created["data"]["plan_code"]
    log.info("Created Paystack annual plan %s (%s)", code, settings.plan_name)
    return code


def initialize_checkout(account_id: int, email: str) -> dict:
    """Create a Paystack transaction for the annual plan. Returns
    {authorization_url, reference, access_code} for the client redirect."""
    plan_code = ensure_plan()
    payload = {
        "email": email,
        "amount": settings.plan_amount_minor,
        "currency": settings.plan_currency,
        "plan": plan_code,
        # `app` tags this transaction so the shared Paystack webhook router can
        # route it to the correct website (Steward 2026-09-03: one account, many
        # sites -> a single router fans events out by this tag).
        "metadata": {"account_id": str(account_id), "gateway": "paystack",
                     "app": "n8n-portal"},
        "callback_url": settings.paystack_callback_url,
    }
    resp = _req("POST", "/transaction/initialize", payload)
    data = resp.get("data", {})
    return {
        "authorization_url": data.get("authorization_url", ""),
        "reference": data.get("reference", ""),
        "access_code": data.get("access_code", ""),
    }


def verify_transaction(reference: str) -> dict:
    resp = _req("GET", f"/transaction/verify/{reference}")
    return resp.get("data", {})


def verify_webhook_signature(payload: bytes, signature: str | None) -> bool:
    """Paystack signs webhooks with HMAC-SHA512 (hex) of the raw body."""
    if not signature or not settings.paystack_secret_key:
        return False
    expected = hmac.new(
        settings.paystack_secret_key.encode(), payload, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_event(event: dict) -> dict:
    """Route a verified Paystack webhook event to the shared billing actions.
    Returns a status dict for the caller.

    ROUTER GUARD (2026-09-03, Steward): the portal shares one Paystack account
    with other websites via a webhook router. Events are tagged with `metadata.app`.
    Ignore any event not tagged for this portal so a misrouted event can never
    touch portal billing state."""
    from . import billing

    event_type = event.get("event", "")
    data = event.get("data", {}) or {}
    # If a router tagged it, only handle events meant for the n8n portal.
    app_tag = (data.get("metadata") or {}).get("app")
    if app_tag and app_tag != "n8n-portal":
        log.info("Ignoring Paystack event %s tagged app=%s (not this portal)",
                 event_type, app_tag)
        return {"status": "ignored", "event": event_type}

    if event_type == "charge.success":
        return _on_charge_success(data)
    if event_type == "invoice.payment_failed":
        return _on_payment_failed(data)
    if event_type in ("subscription.disable", "subscription.not_renew"):
        return _on_subscription_disabled(data)
    if event_type == "subscription.create":
        # store the subscription code so renewal events can be matched
        sub = data.get("subscription") or {}
        account = _account_from_data(data)
        if account and sub.get("subscription_code"):
            db.update_subscription_status(account["id"], "active")
            db.set_subscription(
                account["id"],
                stripe_customer_id=account.get("stripe_customer_id", ""),
                subscription_id=sub["subscription_code"],
                status="active",
            )
        return {"status": "subscription_created"}
    return {"status": "ignored", "event": event_type}


def _account_from_data(data: dict):
    meta = data.get("metadata") or {}
    if meta.get("account_id"):
        account = db.get_account(int(meta["account_id"]))
        if account:
            return account
    customer = data.get("customer") or {}
    email = customer.get("email")
    if email:
        return db.get_account_by_email(email)
    return None


def _on_charge_success(data: dict) -> dict:
    from . import billing

    account = _account_from_data(data)
    if not account:
        raise PaystackError("charge.success for unknown account.")
    if billing._row_get(account, "account_state", "active") != "active":
        # Admin suspension/archive outranks payment: acknowledge the money but
        # do NOT reactivate or restart the workspace.
        log.warning(
            "Paystack charge.success for %s account %s; NOT reactivating (admin state)",
            billing._row_get(account, "account_state", "active"), account["id"])
        return {"status": "ignored_suspended", "account_id": account["id"]}
    # annual: paid_until = now + 1 year (Paystack renews yearly on the plan)
    paid_until = int(time.time()) + 365 * 24 * 3600
    customer = data.get("customer") or {}
    sub = data.get("subscription") or {}
    db.set_subscription(
        account["id"],
        stripe_customer_id=customer.get("customer_code", ""),
        subscription_id=sub.get("subscription_code", ""),
        status="active",
        paid_until=paid_until,
    )
    billing.unlock_instance(account["id"])
    log.info("Paystack charge success; account %s active until %s",
             account["id"], paid_until)
    return {"status": "active", "account_id": account["id"], "paid_until": paid_until}


def _on_payment_failed(data: dict) -> dict:
    from . import billing

    account = _account_from_data(data)
    if not account:
        raise PaystackError("payment_failed for unknown account.")
    deadline = int(time.time()) + settings.lock_grace_days * 24 * 3600
    db.update_subscription_status(account["id"], "past_due", deadline)
    log.warning("Paystack renewal failed for account %s; grace until %s",
                account["id"], deadline)
    return {"status": "past_due", "account_id": account["id"], "lock_deadline": deadline}


def _on_subscription_disabled(data: dict) -> dict:
    from . import billing

    account = _account_from_data(data)
    if not account:
        raise PaystackError("subscription disabled for unknown account.")
    db.update_subscription_status(account["id"], "canceled")
    billing.lock_instance(account["id"])
    log.warning("Paystack subscription disabled for account %s; locked", account["id"])
    return {"status": "canceled", "account_id": account["id"]}
