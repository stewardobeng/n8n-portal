# 0.1.75: the checkout is a one-off annual charge (no Paystack plan), so every
# channel the account has enabled shows up (card, mobile money, bank transfer);
# renewal is the customer repaying, and a payment EXTENDS the term.

import sys
import time

import pytest

from app import db
from app.config import settings


@pytest.fixture
def renew_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "renew.db"))
    db.init_db()
    return tmp_path


def _account_with_workspace(email, paid_until):
    aid = db.create_account(email, email.split("@")[0], "Test Customer",
                            first_name="T", last_name="C", password_hash="x")
    db.create_instance(account_id=aid, stack_name="ws" + str(aid), environment_id=8,
                       environment_name="x", port=33000 + aid, domain=f"{aid}.test",
                       basic_auth_user=email, basic_auth_password="Str0ngPass1",
                       n8n_encryption_key="e" * 32, status="healthy")
    db.update_subscription_status(aid, "active", paid_until=paid_until)
    return aid


def test_checkout_payload_is_a_one_off_charge_not_a_subscription(renew_db, monkeypatch):
    """A Paystack plan object turns the checkout into a SUBSCRIPTION, and a
    subscription checkout is card-only, which is why mobile money disappeared.
    The payload must not carry `plan`."""
    from app.services import paystack

    seen = {}

    def fake_req(method, path, payload=None):
        seen["path"] = path
        seen["payload"] = payload
        return {"status": True, "data": {"authorization_url": "https://checkout.paystack.com/x",
                                         "reference": "ref1", "access_code": "ac1"}}

    monkeypatch.setattr(paystack, "_req", fake_req)
    out = paystack.initialize_checkout(7, "buyer@example.com", plan="b")

    p = seen["payload"]
    assert seen["path"] == "/transaction/initialize"
    assert "plan" not in p, "a plan object makes this a card-only subscription checkout"
    assert p["amount"] == settings.plan_b_amount_minor
    assert p["currency"] == settings.plan_currency
    assert p["metadata"]["app"] == "n8n-portal"
    assert p["metadata"]["plan"] == "b"
    assert p["metadata"]["account_id"] == "7"
    assert p["callback_url"] == settings.paystack_callback_url
    assert out["reference"] == "ref1" and out["access_code"] == "ac1"


def test_package_a_still_charges_the_package_a_amount(renew_db, monkeypatch):
    from app.services import paystack

    seen = {}

    def fake_req(method, path, payload=None):
        seen["payload"] = payload
        return {"status": True, "data": {"authorization_url": "u", "reference": "r",
                                         "access_code": "a"}}

    monkeypatch.setattr(paystack, "_req", fake_req)
    paystack.initialize_checkout(1, "a@example.com", plan="a")
    assert seen["payload"]["amount"] == settings.plan_amount_minor
    assert seen["payload"]["metadata"]["plan"] == "a"


def test_payment_extends_an_active_subscription(renew_db, monkeypatch):
    """Paying while still active ADDS a year to the remaining term (the old code
    reset it to now + 1y, which silently threw away the remaining time)."""
    from app.services import paystack, billing
    monkeypatch.setattr(billing, "unlock_instance", lambda account_id: None)

    now = int(time.time())
    future = now + 100 * 86400
    aid = _account_with_workspace("extend@example.com", future)
    out = paystack._on_charge_success({"amount": settings.plan_amount_minor,
                                       "customer": {"email": "extend@example.com",
                                                    "customer_code": "CUS_x"},
                                       "metadata": {"account_id": str(aid)}})
    expected = future + 365 * 86400
    assert abs(out["paid_until"] - expected) <= 5, "early payment must add a year"
    acc = db.get_account(aid)
    assert acc is not None
    assert acc["subscription_status"] == "active"
    assert acc["paid_until"] == out["paid_until"]


def test_payment_after_expiry_starts_a_fresh_year(renew_db, monkeypatch):
    from app.services import paystack, billing
    monkeypatch.setattr(billing, "unlock_instance", lambda account_id: None)

    now = int(time.time())
    aid = _account_with_workspace("expired@example.com", now - 10 * 86400)
    out = paystack._on_charge_success({"amount": settings.plan_amount_minor,
                                       "customer": {"email": "expired@example.com"},
                                       "metadata": {"account_id": str(aid)}})
    assert abs(out["paid_until"] - (now + 365 * 86400)) <= 5


def test_mock_gateway_payment_also_extends(renew_db):
    from app.services import billing

    now = int(time.time())
    future = now + 30 * 86400
    aid = _account_with_workspace("mockext@example.com", future)
    out = billing.mock_handle_event({"mock": True, "type": "charge.success",
                                     "data": {"metadata": {"account_id": str(aid)}}})
    assert abs(out["paid_until"] - (future + 365 * 86400)) <= 5


def test_package_b_charge_still_moves_the_account_to_plus(renew_db, monkeypatch):
    """Package resolution no longer relies on the plan code: the AMOUNT decides.
    Production runs with PLAN_B_ACTIVE=true, so a 50000 charge is Plus."""
    from app.services import paystack, billing
    monkeypatch.setattr(billing, "unlock_instance", lambda account_id: None)
    monkeypatch.setattr(settings, "plan_b_active", True)

    aid = _account_with_workspace("plus@example.com", int(time.time()) + 86400)
    paystack._on_charge_success({"amount": settings.plan_b_amount_minor,
                                 "customer": {"email": "plus@example.com"},
                                 "metadata": {"account_id": str(aid)}})
    acc = db.get_account(aid)
    assert acc is not None
    assert billing._row_get(acc, "plan", "a") == "b"
