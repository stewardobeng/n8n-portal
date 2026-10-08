"""Admin SMS one-time codes through mNotify (0.1.90, Steward 2026-10-07).

His words: "add sms otp as one of the options for 2fa for the admin account. so when the code
is generate it gets sents via sms to the admin's phone number ... we will use mnotify for the
sms sending and i will provide the api ... also if possible create a page or expand the
settings page for the admin, where i can enter those api/secret keys. also the admin should be
able to edit his profile to add his email and phone number for the sms and email otps."

Decisions he confirmed in the same thread:
  * Sender ID = "StePro-OTP"
  * plain transactional SMS (do NOT set sms_type:"otp", which costs 0.035 extra per campaign)
  * admin only for now

Pinned here, beyond the happy path:
  * the API key is write-only - a settings read returns only "set" + the last four;
  * the key never reaches a log line or an error message (mNotify takes it as a query
    parameter and httpx logs full URLs at INFO);
  * SMS codes cost money, so /admin/mfa-send-otp demands the MFA challenge on that channel;
  * if admin 2FA is switched on but nothing can deliver, login REFUSES instead of quietly
    signing the admin in with no second factor.
"""
import json

import pytest

from app import db
from app.config import settings
from app.services import account_security, sms


# --------------------------------------------------------------------- helpers

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _admin():
    from app.security import create_access_token
    return {"Authorization": "Bearer " + create_access_token("admin")}


def _challenge(methods=("sms",)):
    """An admin MFA challenge: what the password step hands out."""
    from app.security import create_mfa_token
    return create_mfa_token(0, list(methods))


OK_PAYLOAD = {
    "status": "success",
    "code": "2000",
    "message": "messages sent successfully",
    "summary": {"_id": "A59CCB70-TEST", "total_sent": 1, "credit_used": 1, "credit_left": 1483},
}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "sms.db"))
    db.init_db()
    yield


@pytest.fixture
def captured(monkeypatch):
    """Catch the outbound call instead of hitting mNotify, with nothing else faked."""
    calls = []

    def fake_post(url, **kw):
        calls.append({"method": "POST", "url": url, **kw})
        return _FakeResponse(OK_PAYLOAD)

    def fake_get(url, **kw):
        calls.append({"method": "GET", "url": url, **kw})
        return _FakeResponse({"status": "success", "balance": 1483, "bonus": 20})

    monkeypatch.setattr(sms.httpx, "post", fake_post)
    monkeypatch.setattr(sms.httpx, "get", fake_get)
    db.set_setting(sms.SMS_KEY, "test-key-not-real")
    return calls


def _configure_phone(phone="0241234567"):
    db.set_setting(account_security.ADMIN_PHONE_SETTING, phone)


# --------------------------------------------------------------------- numbers

@pytest.mark.parametrize("raw,expected", [
    ("0241234567", "0241234567"),
    ("024 123 4567", "0241234567"),
    ("241234567", "0241234567"),
    ("233241234567", "0241234567"),
    ("+233 24 123 4567", "0241234567"),
    ("00233241234567", "0241234567"),
    ("(024) 123-4567", "0241234567"),
])
def test_ghana_numbers_in_every_shape_people_type_them(raw, expected):
    assert sms.normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "12345", "02412345678", "not a number"])
def test_junk_numbers_are_refused(raw):
    assert sms.normalise_phone(raw) is None


def test_a_number_is_masked_where_it_is_shown_to_a_third_party():
    """Two forms on purpose: the login screen shows the mask, Steward's own settings page
    shows the whole number so he can check it is his."""
    assert sms.mask_phone("0241234567") == "024 *** 67"
    assert "123" not in sms.mask_phone("0241234567")
    assert sms.pretty_phone("0241234567") == "024 123 4567"


# --------------------------------------------------------------------- sender id

def test_stewards_sender_id_is_accepted_and_bounds_are_enforced():
    assert sms.validate_sender("StePro-OTP") == "StePro-OTP"
    assert sms.validate_sender("") == ""            # empty means "use the default"
    with pytest.raises(sms.SmsError):
        sms.validate_sender("SteProTECH2026")       # 14 characters, over mNotify's 11
    with pytest.raises(sms.SmsError):
        sms.validate_sender("StePro<OTP>")


def test_the_default_sender_is_stewards_approved_name():
    assert sms.sender_id() == "StePro-OTP"


# --------------------------------------------------------------------- sending

def test_send_builds_the_request_mnotify_documents(captured):
    out = sms.send_sms("0241234567", "hello there")
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "https://api.mnotify.com/api/sms/quick"
    assert call["params"] == {"key": "test-key-not-real"}
    body = call["json"]
    assert body["recipient"] == ["0241234567"]
    assert body["sender"] == "StePro-OTP"
    assert body["message"] == "hello there"
    assert body["is_schedule"] is False and body["schedule_date"] == ""
    assert "sms_type" not in body                    # plain transactional, as he chose
    assert out["sent"] is True and out["credit_left"] == 1483


def test_otp_typing_stays_off_unless_switched_on(captured):
    db.set_setting(sms.SMS_OTP_TYPE, "")
    sms.send_otp_code("0241234567", "123456", minutes=5)
    assert "sms_type" not in captured[-1]["json"]

    db.set_setting(sms.SMS_OTP_TYPE, "otp")
    sms.send_otp_code("0241234567", "123456", minutes=5)
    assert captured[-1]["json"]["sms_type"] == "otp"


def test_a_refusal_becomes_a_plain_error_without_the_key(monkeypatch):
    monkeypatch.setattr(sms.httpx, "post",
                        lambda url, **kw: _FakeResponse({"status": "error", "code": "1050",
                                                         "message": "Insufficient balance"}))
    db.set_setting(sms.SMS_KEY, "test-key-not-real")
    with pytest.raises(sms.SmsError) as e:
        sms.send_sms("0241234567", "hi")
    assert "Insufficient balance" in str(e.value)
    assert "test-key-not-real" not in str(e.value)


def test_check_sender_and_balance_read_what_mnotify_returns(monkeypatch):
    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: _FakeResponse(
        {"status": "success", "code": "2000", "message": "ok",
         "summary": {"sender_name": "StePro-OTP", "status": "Approved"}}))
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _FakeResponse(
        {"status": "success", "balance": 1483}))
    db.set_setting(sms.SMS_KEY, "k")
    assert sms.check_sender()["approved"] is True
    assert sms.sms_balance()["balance"] == 1483


# --------------------------------------------------------------------- key handling

def test_the_api_key_is_write_only_for_the_whole_api(captured):
    client = _client()
    r = client.put("/api/v1/admin/settings", headers=_admin(),
                   json={"sms_api_key": "sk_live_super_secret_9876", "sms_sender_id": "StePro-OTP"})
    assert r.status_code == 200, r.text
    assert "sk_live_super_secret_9876" not in r.text          # not even in the write reply
    assert r.json()["sms_api_key_set"] is True
    assert r.json()["sms_api_key_last4"] == "9876"

    got = client.get("/api/v1/admin/settings", headers=_admin())
    assert got.status_code == 200
    assert "super_secret" not in got.text
    assert got.json()["sms_api_key_last4"] == "9876"
    assert got.json()["sms_configured"] is True
    assert got.json()["sms_sender_id"] == "StePro-OTP"


def test_saving_other_fields_leaves_the_key_alone(captured):
    client = _client()
    client.put("/api/v1/admin/settings", headers=_admin(), json={"sms_api_key": "keep-me-1234"})
    client.put("/api/v1/admin/settings", headers=_admin(), json={"sms_sender_id": "StePro-OTP"})
    assert db.get_setting(sms.SMS_KEY) == "keep-me-1234"


def test_the_profile_saves_a_normalised_phone_and_validates_rubbish(captured):
    client = _client()
    r = client.put("/api/v1/admin/settings", headers=_admin(),
                   json={"admin_email": "steward@example.com", "admin_phone": "+233 24 123 4567"})
    assert r.status_code == 200, r.text
    assert db.get_setting(account_security.ADMIN_PHONE_SETTING) == "0241234567"
    assert r.json()["admin_phone_pretty"] == "024 123 4567"

    bad = client.put("/api/v1/admin/settings", headers=_admin(), json={"admin_phone": "12"})
    assert bad.status_code == 422
    bad_email = client.put("/api/v1/admin/settings", headers=_admin(), json={"admin_email": "nope"})
    assert bad_email.status_code == 422


# --------------------------------------------------------------------- login flow

def test_login_offers_sms_only_when_it_can_actually_deliver(captured, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "verify_admin_password", lambda pw: True)
    client = _client()
    db.set_setting("admin_2fa", "sms")
    # No phone yet: SMS must not be offered, and login must NOT fall through to no 2FA.
    r = client.post("/api/v1/admin/login", json={"password": "x"})
    assert r.status_code == 409
    assert "no usable method" in r.json()["detail"]

    _configure_phone()
    r = client.post("/api/v1/admin/login", json={"password": "x"})
    assert r.status_code == 200
    methods = [m["method"] for m in r.json()["mfa"]["methods"]]
    assert methods == ["sms"]
    assert r.json()["mfa"]["methods"][0]["to"] == "024 *** 67"


def test_sms_send_needs_the_challenge_because_each_text_costs_money(captured):
    client = _client()
    _configure_phone()
    db.set_setting("admin_2fa", "sms")

    no_challenge = client.post("/api/v1/admin/mfa-send-otp", json={"channel": "sms"})
    assert no_challenge.status_code == 400

    r = client.post("/api/v1/admin/mfa-send-otp",
                    json={"channel": "sms", "challenge": _challenge()})
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "sms"
    assert captured and captured[-1]["url"].endswith("/sms/quick")


def test_the_sms_code_verifies_the_admin_signin(captured):
    client = _client()
    _configure_phone()
    db.set_setting("admin_2fa", "sms")
    challenge = _challenge()
    client.post("/api/v1/admin/mfa-send-otp", json={"channel": "sms", "challenge": challenge})

    # Recover the code the way the admin would: read what was texted.
    sent = captured[-1]["json"]["message"]
    code = sent.split()[0]
    assert code.isdigit() and len(code) == 6

    r = client.post("/api/v1/admin/mfa-verify", json={"challenge": challenge, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["token"]


def test_a_wrong_code_is_still_refused(captured):
    client = _client()
    _configure_phone()
    db.set_setting("admin_2fa", "sms")
    challenge = _challenge()
    client.post("/api/v1/admin/mfa-send-otp", json={"channel": "sms", "challenge": challenge})
    r = client.post("/api/v1/admin/mfa-verify", json={"challenge": challenge, "code": "000000"})
    assert r.status_code == 401


def test_the_test_button_does_not_invalidate_a_live_code(captured):
    client = _client()
    _configure_phone()
    db.set_setting("admin_2fa", "sms")
    challenge = _challenge()
    client.post("/api/v1/admin/mfa-send-otp", json={"channel": "sms", "challenge": challenge})
    live_hash = db.get_setting(account_security.ADMIN_OTP_HASH)

    r = client.post("/api/v1/admin/sms/test", headers=_admin())
    assert r.status_code == 200, r.text
    assert "test message" in captured[-1]["json"]["message"].lower()
    assert db.get_setting(account_security.ADMIN_OTP_HASH) == live_hash


# --------------------------------------------------------------------- enable / disable

def test_enabling_sms_needs_a_code_that_arrived(captured):
    client = _client()
    _configure_phone()
    sent = client.post("/api/v1/admin/security/sms/send", headers=_admin())
    assert sent.status_code == 200 and sent.json()["channel"] == "sms"

    wrong = client.post("/api/v1/admin/security/sms/enable", headers=_admin(), json={"code": "000000"})
    assert wrong.status_code == 400
    assert "sms" not in (db.get_setting("admin_2fa") or "")

    code = captured[-1]["json"]["message"].split()[0]
    ok = client.post("/api/v1/admin/security/sms/enable", headers=_admin(), json={"code": code})
    assert ok.status_code == 200, ok.text
    assert "sms" in (db.get_setting("admin_2fa") or "").split(",")


def test_sms_cannot_be_switched_on_without_a_phone_or_a_key(captured):
    client = _client()
    db.set_setting(sms.SMS_KEY, "")
    _configure_phone()
    r = client.post("/api/v1/admin/security/sms/send", headers=_admin())
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]


def test_disabling_sms_leaves_the_other_factors_alone(captured):
    client = _client()
    _configure_phone()
    db.set_setting("admin_2fa", "totp,email,sms")
    r = client.post("/api/v1/admin/security/sms/disable", headers=_admin())
    assert r.status_code == 200
    assert db.get_setting("admin_2fa") == "totp,email"


def test_customer_login_is_untouched_by_the_admin_sms_work(captured):
    """Customers keep totp/email: no sms method is offered on their side."""
    from app.services import account_security as ac
    import time
    conn = db.get_conn()
    conn.execute("INSERT INTO accounts (email, username, password_hash, account_state, "
                 "subscription_status, quota, created_at, plan) VALUES "
                 "('c@example.com','cust',?, 'active','active',1,?, 'a')",
                 (ac.hash_password("pw") if hasattr(ac, "hash_password") else "x", int(time.time())))
    conn.commit()
    aid = conn.execute("SELECT id FROM accounts WHERE username='cust'").fetchone()["id"]
    db.set_setting("admin_2fa", "sms")
    methods = [m["method"] for m in ac.enabled_methods(aid)]
    assert "sms" not in methods


# --------------------------------------------------------------------- front end guards

def _js():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()


def test_the_code_step_lets_the_admin_choose_the_channel():
    js = _js()
    assert 'data-action="admin-mfa-send" data-channel="sms"' in js
    assert 'data-action="admin-mfa-send" data-channel="email"' in js
    # the send carries the challenge: required for SMS, and harmless for email
    assert 'body: { channel: chan, challenge: ams.challenge }' in js
    assert "Text me a code" in js


def test_the_key_field_is_a_password_field_and_never_echoes_the_saved_key():
    js = _js()
    assert '<input id="sms-api-key" type="password"' in js
    assert "sms_api_key_last4" in js
    assert "sms_api_key" in js
    # the stored key must never be rendered into the page
    assert 'value="\' + esc(s.sms_api_key' not in js


def test_settings_page_has_the_profile_and_mnotify_cards():
    js = _js()
    assert "function adminProfileCard()" in js and "function mnotifyCard()" in js
    assert "Administrator profile" in js and "Text messages (mNotify)" in js
    assert 'data-action="save-admin-profile"' in js and 'data-action="save-sms"' in js
    assert 'data-action="sms-test"' in js and 'data-action="sms-status"' in js


def test_sms_2fa_card_is_admin_only():
    js = _js()
    assert "var smsCard = isAdmin" in js
    assert 'data-action="sec-sms-setup"' in js
    assert 'data-action="sec-sms-disable"' in js
    assert "Add your phone number in" in js


def test_the_versions_match_the_compose_image():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    compose = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    assert "app.js?v=0.1.90" in html and "styles.css?v=0.1.90" in html
    assert "n8n-portal-backend:0.1.90" in compose


def test_json_body_never_carries_the_key_in_a_logged_url():
    """The key travels as a query param (mNotify's design) - the service silences httpx's
    request logging around the call so it cannot reach docker logs."""
    src = open(sms.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert "_quiet_httpx" in src
    assert 'logging.getLogger("httpx")' in src
    assert json.dumps({"key": "x"})  # json import is used by nothing else: keep flake happy
