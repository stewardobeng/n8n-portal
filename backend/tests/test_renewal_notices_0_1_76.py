# 0.1.76: renewal reminders + login-free renewal links.
# With the Paystack plan gone (0.1.75) renewal is a customer action, so the
# portal warns before and after the term ends, each mail carrying a link that
# starts a payment for that account without a sign-in.

import sys
import time

import pytest

from app import db
from app.config import settings

DAY = 86400


@pytest.fixture
def rdb(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "renewal.db"))
    db.init_db()
    return tmp_path


def _seed_account(email="renew@example.com", days_until=20, plan="a",
                  state="active", status="active"):
    aid = db.create_account(email, email.split("@")[0], "Renew Tester",
                            first_name="Renew", last_name="Tester", password_hash="x")
    db.create_instance(account_id=aid, stack_name="ws%d" % aid, environment_id=8,
                       environment_name="x", port=34000 + aid, domain=f"ws{aid}.test",
                       basic_auth_user=email, basic_auth_password="Str0ngPass1",
                       n8n_encryption_key="e" * 32, status="healthy")
    db.update_subscription_status(aid, status, paid_until=int(time.time()) + days_until * DAY)
    conn = db.get_conn()
    try:
        conn.execute("UPDATE accounts SET plan=?, account_state=? WHERE id=?",
                     (plan, state, aid))
        conn.commit()
    finally:
        conn.close()
    return aid


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing email instead of talking to SMTP."""
    box = []
    from app.services import emailer
    monkeypatch.setattr(emailer, "send_email",
                        lambda to, subject, html, text=None: box.append(
                            {"to": to, "subject": subject, "html": html, "text": text}))
    return box


# ---- schedule ----

def test_stage_boundaries():
    from app.services import renewal
    assert renewal.stage_for(31) is None
    assert renewal.stage_for(30) == "30"
    assert renewal.stage_for(8) == "30"
    assert renewal.stage_for(7) == "7"
    assert renewal.stage_for(1) == "7"
    assert renewal.stage_for(0) == "0"
    assert renewal.stage_for(-1) == "post"
    assert renewal.stage_for(-21) == "post"
    assert renewal.stage_for(-22) is None      # long lapsed: not woken up
    assert renewal.stage_for(None) is None


def test_days_left_counts_the_last_day_as_zero():
    from app.services import renewal
    now = 1_800_000_000
    assert renewal.days_left(now, now) == 0
    assert renewal.days_left(now - 1, now) == 0        # a second past is still "today"
    assert renewal.days_left(now - DAY, now) == -1
    assert renewal.days_left(now + 29 * DAY + 60, now) == 30   # ceil
    assert renewal.days_left(None, now) is None


# ---- links ----

def test_link_round_trip_and_expiry(rdb):
    from app.services import renewal
    now = int(time.time())
    token = renewal.create_link(7, "b", now=now)
    row = renewal.resolve_link(token, now=now)
    assert row and int(row["account_id"]) == 7 and row["plan"] == "b"
    assert renewal.link_url(token).endswith("/r/" + token)
    # expired, unknown and absurd tokens resolve to nothing
    assert renewal.resolve_link(token, now=now + 61 * DAY) is None
    assert renewal.resolve_link("not-a-real-token", now=now) is None
    assert renewal.resolve_link("x" * 500) is None


def test_link_is_reused_within_its_lifetime(rdb):
    from app.services import renewal
    now = int(time.time())
    first = renewal.link_for_account(9, "a", now=now)
    assert renewal.link_for_account(9, "a", now=now + 5 * DAY) == first


# ---- the sweep ----

def test_reminder_sends_once_per_stage_and_term(rdb, sent, monkeypatch):
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", True)
    aid = _seed_account(days_until=20)

    first = renewal.maybe_send_reminders(force=True)
    assert first["sent"] == 1 and len(sent) == 1
    assert "renews on" in sent[0]["subject"]
    acc = db.get_account(aid)
    assert acc is not None
    assert db.notice_sent(aid, "30", int(acc["paid_until"]))

    again = renewal.maybe_send_reminders(force=True)
    assert again["sent"] == 0 and len(sent) == 1, "the same stage must not repeat"


def test_next_terms_reminders_are_not_suppressed(rdb, sent, monkeypatch):
    """Renewal moves paid_until forward, so the next year's reminder must fire."""
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", True)
    now = int(time.time())
    aid = _seed_account(days_until=20)
    renewal.maybe_send_reminders(force=True, now=now)
    assert len(sent) == 1

    # customer renews: a new term, one year further out
    db.update_subscription_status(aid, "active", paid_until=now + 385 * DAY)
    assert renewal.maybe_send_reminders(force=True, now=now)["sent"] == 0  # too early
    later = now + 356 * DAY                       # 29 days before the new expiry
    assert renewal.maybe_send_reminders(force=True, now=later)["sent"] == 1
    assert len(sent) == 2


def test_switched_off_sends_nothing(rdb, sent, monkeypatch):
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", False)
    _seed_account(days_until=3)
    out = renewal.maybe_send_reminders()
    assert out["enabled"] is False and out["sent"] == 0 and sent == []
    # ... but the deliberate single send still works (the live test path)
    renewal.send_for_account(1, "7")
    assert len(sent) == 1 and "renews in 7 days" in sent[0]["subject"]


def test_runs_at_most_once_a_day(rdb, sent, monkeypatch):
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", True)
    _seed_account(days_until=2)
    assert renewal.maybe_send_reminders(force=True)["sent"] == 1
    db.set_setting("renewal_reminders_last_run", time.strftime("%Y-%m-%d", time.gmtime()))
    out = renewal.maybe_send_reminders()
    assert out["sent"] == 0 and "already ran" in out["skipped"]


def test_suspended_and_archived_accounts_are_never_emailed(rdb, sent, monkeypatch):
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", True)
    _seed_account("suspended@example.com", days_until=5, state="suspended")
    _seed_account("archived@example.com", days_until=5, state="archived")
    assert renewal.maybe_send_reminders(force=True)["sent"] == 0
    assert sent == []


def test_post_expiry_mail_tells_the_customer_the_workspace_is_paused(rdb, sent, monkeypatch):
    from app.services import renewal
    monkeypatch.setattr(settings, "renewal_reminders_enabled", True)
    _seed_account(days_until=-3)
    out = renewal.maybe_send_reminders(force=True)
    assert out["sent"] == 1
    assert "paused" in sent[0]["subject"].lower()
    assert "same login details" in sent[0]["text"]


# ---- copy ----

def test_email_is_customer_ready(rdb, sent):
    from app.services import renewal
    aid = _seed_account(days_until=30, plan="b")
    mail = renewal.render("30", db.get_account(aid))
    assert "Hello" in mail["text"] and "Renew" in mail["text"]
    assert settings.plan_b_name in mail["subject"] + mail["text"] or \
        settings.plan_b_name in mail["html"]
    assert "GHS 500" in mail["text"], "package B must show its own price"
    assert mail["link"] in mail["text"] and mail["link"] in mail["html"]
    assert "no need to sign in" in mail["text"]
    for blob in (mail["subject"], mail["text"], mail["html"]):
        assert "\u2014" not in blob, "no em dashes in customer-facing copy"
    for blob in (mail["subject"], mail["text"]):
        assert "  " not in blob, "no doubled spaces in the readable copy"


def test_preview_renders_every_stage_without_sending(rdb, sent):
    from app.services import renewal
    aid = _seed_account(days_until=12)
    out = renewal.preview(aid)
    assert [m["stage"] for m in out["emails"]] == ["30", "7", "0", "post"]
    assert out["sending_enabled"] is False
    assert out["sampled"] is False
    assert sent == [], "a preview must never email anyone"
    sample = renewal.preview(None)
    assert sample["sampled"] is True and len(sample["emails"]) == 4


# ---- the public link route ----

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "route.db"))
    db.init_db()
    return TestClient(app)


def test_link_route_starts_a_payment_without_a_sign_in(tmp_path, monkeypatch):
    from app.services import renewal, paystack
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "route.db"))
    db.init_db()
    aid = _seed_account(days_until=2)
    seen = {}

    def fake_checkout(account_id, email, plan="a"):
        seen.update(account_id=account_id, email=email, plan=plan)
        return {"authorization_url": "https://checkout.paystack.com/live-test-link",
                "reference": "ref", "access_code": "ac"}

    monkeypatch.setattr(paystack, "initialize_checkout", fake_checkout)
    token = renewal.create_link(aid, "a")
    client = _client(tmp_path, monkeypatch)
    r = client.get(f"/r/{token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://checkout.paystack.com/live-test-link"
    assert seen["account_id"] == aid and seen["plan"] == "a"
    assert db.get_renewal_link(token)["used_at"] is not None


def test_link_route_rejects_bad_expired_and_suspended(tmp_path, monkeypatch):
    from app.services import renewal, paystack
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "route2.db"))
    db.init_db()
    called = []
    monkeypatch.setattr(paystack, "initialize_checkout",
                        lambda *a, **k: called.append(a) or {"authorization_url": "x"})
    client = _client(tmp_path, monkeypatch)

    assert client.get("/r/nope").status_code == 410
    expired = renewal.create_link(_seed_account("exp@example.com"), "a",
                                  now=int(time.time()) - 100 * DAY)
    assert client.get(f"/r/{expired}").status_code == 410

    suspended = _seed_account("susp@example.com", state="suspended")
    token = renewal.create_link(suspended, "a")
    assert client.get(f"/r/{token}").status_code == 410
    assert called == [], "no checkout may be created for a suspended account"


def test_admin_preview_needs_admin_and_shows_the_copy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.security import create_access_token
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "preview.db"))
    db.init_db()
    _seed_account(days_until=9)
    client = TestClient(app)
    assert client.get("/api/v1/admin/billing/renewal-notices").status_code in (401, 403)
    h = {"Authorization": "Bearer " + create_access_token("admin")}
    r = client.get("/api/v1/admin/billing/renewal-notices?account_id=1", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert len(body["emails"]) == 4 and body["emails"][0]["subject"]
    assert body["emails"][0]["link"].startswith(settings.portal_base_url + "/r/")
    assert body["sending_enabled"] is False
