# Tests: 0.1.67 webhook hardening - idempotency, amount gate, router secret.
import hashlib
import hmac
import json
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def wh_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "wh.db"))
    monkeypatch.setattr(settings, "jwt_secret", "wh-test-secret")
    monkeypatch.setattr(settings, "paystack_secret_key", "sk_test_wh")
    monkeypatch.setattr(settings, "paystack_router_secret", "")  # off by default
    monkeypatch.setattr(settings, "paystack_expected_amount_a", "")
    monkeypatch.setattr(settings, "paystack_expected_amount_b", "")
    monkeypatch.setattr(settings, "plan_b_active", True)
    monkeypatch.setattr(settings, "plan_b_amount_minor", 50000)
    db.init_db()
    return tmp_path


def _charge(amount=30000, reference="REF-1", account_id=1, app_tag="n8n-portal"):
    return {
        "event": "charge.success",
        "data": {
            "metadata": {"app": app_tag, "account_id": str(account_id)},
            "reference": reference,
            "amount": amount,
            "currency": "GHS",
            "customer": {"email": "acct@example.com"},
        },
    }


def _seed_account(email="acct@example.com"):
    return db.create_account(email, email.split("@")[0], "Name",
                             first_name="F", last_name="L")


# ---- idempotency ----

def test_duplicate_charge_success_is_noop(wh_db, monkeypatch):
    from app.services import paystack
    aid = _seed_account()
    ev = _charge(account_id=aid)
    r1 = paystack.handle_event(ev)
    assert r1["status"] == "active"
    paid1 = db.get_account(aid)["paid_until"]
    # same reference delivered again (router re-drive / replay)
    r2 = paystack.handle_event(ev)
    assert r2["status"] == "duplicate_ignored"
    paid2 = db.get_account(aid)["paid_until"]
    assert paid1 == paid2, "duplicate must NOT extend the subscription again"


def test_different_references_both_process(wh_db):
    from app.services import paystack
    aid = _seed_account()
    r1 = paystack.handle_event(_charge(reference="REF-A", account_id=aid))
    r2 = paystack.handle_event(_charge(reference="REF-B", account_id=aid))
    assert r1["status"] == "active" and r2["status"] == "active"


def test_claim_is_what_blocks_duplicates(wh_db):
    assert db.claim_event("paystack", "charge.success", "R1") is True
    assert db.claim_event("paystack", "charge.success", "R1") is False
    assert db.claim_event("paystack", "charge.success", "R2") is True


# ---- amount gate ----

def test_amount_gate_rejects_wrong_amount(wh_db):
    from app.services import paystack
    monkey_amount(settings, "30000")
    aid = _seed_account()
    res = paystack.handle_event(_charge(amount=999999, reference="REF-BAD",
                                        account_id=aid))
    assert res["status"] == "amount_mismatch"


def test_amount_gate_accepts_plan_prices(wh_db, monkeypatch):
    from app.services import paystack
    monkey_amount(settings, "30000")
    monkeypatch.setattr(settings, "paystack_expected_amount_b", "50000")
    aid = _seed_account()
    ok_a = paystack.handle_event(_charge(amount=30000, reference="RA", account_id=aid))
    assert ok_a["status"] == "active"
    ok_b = paystack.handle_event(_charge(amount=50000, reference="RB", account_id=aid))
    assert ok_b["status"] == "active"


def test_amount_gate_off_when_unconfigured(wh_db):
    from app.services import paystack
    aid = _seed_account()
    res = paystack.handle_event(_charge(amount=1, reference="R-off", account_id=aid))
    assert res["status"] == "active"


def monkey_amount(settings, value):
    settings.paystack_expected_amount_a = value


# ---- router secret ----

def _signed(payload: bytes, key: str = "sk_test_wh") -> str:
    return hmac.new(key.encode(), payload, hashlib.sha512).hexdigest()


def test_router_secret_enforced_when_configured(wh_db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "paystack_router_secret", "rs-correct")
    c = TestClient(app)
    body = json.dumps(_charge(reference="R-rs1", account_id=_seed_account())).encode()
    sig = _signed(body)
    # missing header
    r = c.post("/api/v1/webhook/paystack", content=body,
               headers={"x-paystack-signature": sig})
    assert r.status_code == 401
    # wrong header
    r = c.post("/api/v1/webhook/paystack", content=body,
               headers={"x-paystack-signature": sig, "x-router-secret": "nope"})
    assert r.status_code == 401
    # correct header -> passes to signature + billing path
    r = c.post("/api/v1/webhook/paystack", content=body,
               headers={"x-paystack-signature": sig, "x-router-secret": "rs-correct"})
    assert r.status_code == 200


def test_router_secret_off_allows_direct(wh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    body = json.dumps(_charge(reference="R-rs2", account_id=_seed_account())).encode()
    r = c.post("/api/v1/webhook/paystack", content=body,
               headers={"x-paystack-signature": _signed(body)})
    assert r.status_code == 200


def test_bad_signature_still_rejected(wh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    body = json.dumps(_charge(reference="R-bad", account_id=_seed_account())).encode()
    r = c.post("/api/v1/webhook/paystack", content=body,
               headers={"x-paystack-signature": "0" * 128})
    assert r.status_code == 400
