# Billing facade + shared lock/unlock engine.
# Gateways:
#   paystack (PRIMARY, GHS) — real Paystack API (app/services/paystack.py)
#   stripe   (secondary, for later) — real Stripe API (app/services/stripe_billing.py)
#   mock     — no external service; checkout returns a fake URL and the webhook
#              endpoint accepts {"mock": true} events, so the full pay -> provision
#              -> renew -> lock flow is E2E-testable without keys.
#
# Subscription lifecycle (annual, auto-renewing):
#   none      -> (checkout completed) -> active (paid_until = now + 1y)
#   active    -> (renewal fails)       -> past_due (grace window starts)
#   past_due  -> (grace elapsed)       -> locked
#   past_due  -> (renewal succeeds)    -> active (unlocked)
#   locked    -> (renewal succeeds)    -> active (unlocked)
#   canceled  -> terminal; instance locked
#
# LOCK = STOP the tenant's Portainer stack (all containers off). Nothing is
# reachable: no login, no forgot-password, no API — the lock is absolute.
# Steward's direction 2026-09-01: rotating the owner password was NOT enough
# because n8n's forgot-password flow still worked (reset email sent via SMTP),
# letting a locked user recover access. Stopping the container closes that.
# UNLOCK = start the stack again; the owner password is untouched, so the
# user logs in with their original credentials.

import logging
import secrets
import string
import time

from .. import db
from ..config import settings

log = logging.getLogger("n8n-portal")


class BillingError(Exception):
    pass


def _rand(n: int = 20) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def gateway() -> str:
    return settings.payment_gateway.lower()


def is_live() -> bool:
    return gateway() in ("paystack", "stripe")


# ---------- checkout ----------

def create_checkout(account_id: int, email: str) -> dict:
    g = gateway()
    if g == "paystack":
        from . import paystack
        info = paystack.initialize_checkout(account_id, email)
        return {"gateway": "paystack", "url": info["authorization_url"],
                "reference": info["reference"]}
    if g == "stripe":
        from . import stripe_billing
        info = stripe_billing.create_checkout_session(account_id, email)
        return {"gateway": "stripe", "url": info["url"], "id": info["id"]}
    # mock
    token = _rand(24)
    url = f"{settings.paystack_callback_url.split('?')[0]}?checkout=mock&account={account_id}&token={token}"
    return {"gateway": "mock", "url": url, "reference": f"mock_ref_{token}"}


# ---------- webhooks ----------

def handle_webhook(gateway_name: str, payload: bytes, signature: str | None) -> dict:
    import json as _json

    g = gateway_name.lower()
    if g == "paystack":
        from . import paystack
        if not paystack.verify_webhook_signature(payload, signature):
            raise BillingError("Invalid Paystack signature.")
        event = _json.loads(payload)
        return paystack.handle_event(event)
    if g == "stripe":
        from . import stripe_billing
        return stripe_billing.handle_webhook(payload, signature)
    if g == "mock":
        try:
            event = _json.loads(payload)
        except Exception:
            raise BillingError("Invalid mock webhook JSON")
        if not event.get("mock"):
            raise BillingError("Live gateway event while PAYMENT_GATEWAY=mock.")
        return mock_handle_event(event)
    raise BillingError(f"Unknown gateway {g}")


# ---------- mock mode (E2E) ----------

def mock_handle_event(event: dict) -> dict:
    """Accept mock events shaped like the real gateways' webhooks:
      {"mock": true, "type": "charge.success",
       "data": {"metadata": {"account_id": "5"}, ...}}
      {"mock": true, "type": "invoice.payment_failed", "data": {...}}
      {"mock": true, "type": "subscription.disable", "data": {...}}
    """
    event_type = event.get("type", "")
    data = event.get("data", {}) or {}

    def account_from(data):
        meta = data.get("metadata") or {}
        if meta.get("account_id"):
            acc = db.get_account(int(meta["account_id"]))
            if acc:
                return acc
        email = data.get("customer", {}).get("email") or data.get("email")
        if email:
            return db.get_account_by_email(email)
        return None

    if event_type == "charge.success":
        account = account_from(data)
        if not account:
            raise BillingError("charge.success for unknown account.")
        paid_until = int(time.time()) + 365 * 24 * 3600
        db.set_subscription(account["id"], "mock_cus", "mock_sub",
                            "active", paid_until)
        unlock_instance(account["id"])
        return {"status": "active", "account_id": account["id"], "paid_until": paid_until}

    if event_type == "invoice.payment_failed":
        account = account_from(data)
        if not account:
            raise BillingError("payment_failed for unknown account.")
        deadline = int(time.time()) + settings.lock_grace_days * 24 * 3600
        db.update_subscription_status(account["id"], "past_due", deadline)
        return {"status": "past_due", "account_id": account["id"], "lock_deadline": deadline}

    if event_type == "subscription.disable":
        account = account_from(data)
        if not account:
            raise BillingError("subscription disabled for unknown account.")
        db.update_subscription_status(account["id"], "canceled")
        lock_instance(account["id"])
        return {"status": "canceled", "account_id": account["id"]}

    return {"status": "ignored", "event": event_type}


# ---------- sweep ----------

def sweep_past_due() -> dict:
    """Lock any past_due account whose grace deadline has passed."""
    locked_now = []
    for account in db.list_accounts():
        if account["subscription_status"] == "past_due":
            deadline = account["paid_until"] or 0
            if deadline and int(time.time()) > deadline:
                lock_instance(account["id"])
                db.update_subscription_status(account["id"], "locked")
                locked_now.append(account["id"])
    return {"locked": locked_now}


def sweep_expired() -> dict:
    """AUTO-EXPIRY (Steward 2026-09-01): any account whose subscription has
    actually expired (paid_until passed) has its instance STOPPED immediately,
    exactly like the admin lock. Renewal (charge.success) restarts it.

    Runs periodically in the background (see main.lifespan). No grace here:
    the annual period is prepaid, so when it ends, access ends."""
    locked_now = []
    for account in db.list_accounts():
        status = account["subscription_status"]
        if status in ("active", "past_due", "unpaid"):
            paid_until = account["paid_until"] or 0
            if paid_until and int(time.time()) > paid_until:
                if lock_instance(account["id"]):
                    db.update_subscription_status(account["id"], "locked")
                    locked_now.append(account["id"])
    return {"locked": locked_now}


# ---------- lock / unlock engine ----------

def lock_instance(account_id: int) -> bool:
    """STOP the tenant's stack so nothing is reachable (no login, no
    forgot-password, no API). Absolute lock, per Steward 2026-09-01.
    Admin-attached stacks (managed=0, no Portainer stack record) are stopped at
    the container level via their recorded container_id (2026-09-02)."""
    from .portainer_client import PortainerClient

    inst = db.get_active_instance(account_id)
    if not inst:
        log.warning("lock: no healthy instance for account %s", account_id)
        return False
    if inst["locked"]:
        return True  # already locked
    pc = PortainerClient()
    try:
        if inst["managed"] == 0 and inst["container_id"]:
            pc.stop_container(inst["environment_id"], inst["container_id"])
        else:
            pc.stop_stack(inst["stack_id"], inst["environment_id"])
    except Exception as e:
        log.error("lock: stop failed for %s: %s", inst["stack_name"], e)
        return False
    db.update_instance(inst["id"], locked=1)
    log.info("instance %s locked (stack stopped)", inst["stack_name"])
    return True


def unlock_instance(account_id: int) -> bool:
    """Start the tenant's stack again. Owner password is untouched, so the
    user logs in with their original credentials."""
    from .portainer_client import PortainerClient

    inst = db.get_active_instance(account_id)
    if not inst:
        return False
    if not inst["locked"]:
        return True  # already unlocked
    pc = PortainerClient()
    try:
        if inst["managed"] == 0 and inst["container_id"]:
            pc.start_container(inst["environment_id"], inst["container_id"])
        else:
            pc.start_stack(inst["stack_id"], inst["environment_id"])
    except Exception as e:
        log.error("unlock: start failed for %s: %s", inst["stack_name"], e)
        return False
    db.update_instance(inst["id"], locked=0)
    log.info("instance %s unlocked (stack started)", inst["stack_name"])
    return True
