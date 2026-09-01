# Stripe billing (SECONDARY gateway — Steward will enable later; kept ready).
# Annual subscription (mode=subscription) + webhooks, sharing the lock engine.

import logging
import time

from .. import db
from ..config import settings

log = logging.getLogger("n8n-portal")

try:
    import stripe
except ImportError:  # pragma: no cover
    stripe = None

if settings.stripe_secret_key and stripe is not None:
    stripe.api_key = settings.stripe_secret_key


class StripeBillingError(Exception):
    pass


def create_checkout_session(account_id: int, email: str) -> dict:
    if stripe is None or not settings.stripe_secret_key:
        raise StripeBillingError("STRIPE_SECRET_KEY not configured.")
    if not settings.stripe_price_id:
        raise StripeBillingError("STRIPE_PRICE_ID not configured.")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=settings.stripe_success_url,
        cancel_url=settings.stripe_cancel_url,
        metadata={"account_id": str(account_id)},
        subscription_data={"metadata": {"account_id": str(account_id)}},
    )
    return {"id": session.id, "url": session.url}


def handle_webhook(payload: bytes, signature: str | None) -> dict:
    if stripe is None or not settings.stripe_webhook_secret:
        raise StripeBillingError("Stripe webhook secret not configured.")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, settings.stripe_webhook_secret
        )
    except Exception as e:
        raise StripeBillingError(f"Invalid Stripe signature: {e}")
    return _dispatch(event["type"], event.get("data", {}).get("object", {}))


def _dispatch(event_type: str, obj: dict) -> dict:
    from . import billing

    def account_from(obj):
        meta = obj.get("metadata") or {}
        if meta.get("account_id"):
            acc = db.get_account(int(meta["account_id"]))
            if acc:
                return acc
        sub_id = obj.get("subscription")
        if sub_id:
            acc = db.get_account_by_subscription(sub_id)
            if acc:
                return acc
        email = obj.get("customer_email") or obj.get("email")
        if email:
            return db.get_account_by_email(email)
        return None

    if event_type == "checkout.session.completed":
        account = account_from(obj)
        if not account:
            raise StripeBillingError("Checkout completed for unknown account.")
        paid_until = int(time.time()) + 365 * 24 * 3600
        db.set_subscription(account["id"], obj.get("customer") or "",
                            obj.get("subscription") or "", "active", paid_until)
        return {"status": "active", "account_id": account["id"]}

    if event_type == "invoice.paid":
        account = account_from(obj)
        if not account:
            raise StripeBillingError("Invoice paid for unknown account.")
        paid_until = int(time.time()) + 365 * 24 * 3600
        db.update_subscription_status(account["id"], "active", paid_until)
        billing.unlock_instance(account["id"])
        return {"status": "active", "account_id": account["id"]}

    if event_type == "invoice.payment_failed":
        account = account_from(obj)
        if not account:
            raise StripeBillingError("Payment failed for unknown account.")
        deadline = int(time.time()) + settings.lock_grace_days * 24 * 3600
        db.update_subscription_status(account["id"], "past_due", deadline)
        return {"status": "past_due", "account_id": account["id"]}

    if event_type == "customer.subscription.deleted":
        account = account_from(obj)
        if not account:
            raise StripeBillingError("Subscription deleted for unknown account.")
        db.update_subscription_status(account["id"], "canceled")
        billing.lock_instance(account["id"])
        return {"status": "canceled", "account_id": account["id"]}

    return {"status": "ignored", "event": event_type}
