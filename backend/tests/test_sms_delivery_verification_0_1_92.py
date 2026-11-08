"""Digits back for SMS codes, and real delivery verification (0.1.92).

Steward: "but can we bring back the numbers only otp, or was that a real blocker, secondly was
the sender id the blocker, is there a way to verify all these?"

Answered by a controlled A/B on the live account (2026-10-08, one factor per row, same phone):
  A  SteProTECH + old numeric-leading text  -> DELIVERED
  B  SteProTECH + letters text              -> DELIVERED
  C  StePro-OTP + letters text              -> REJECTED
  D  StePro-OTP + old numeric-leading text  -> REJECTED
  E  SteProTECH + sms_type:"otp"            -> refused 402 (main wallet empty)

Content is free, the Sender ID's registered purpose is not. So the codes are digits again, and the
Settings card now asks mNotify for the actual delivery verdict instead of trusting "the API said
200". That is the verification he asked for, and it lives in the UI rather than in a chat reply.
"""
import pytest

from app import db
from app.config import settings
from app.services import account_security as ac, sms


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _admin():
    from app.security import create_access_token
    return {"Authorization": "Bearer " + create_access_token("admin")}


class _Res:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "delivery.db"))
    db.init_db()
    db.set_setting(sms.SMS_KEY, "test-key")
    db.set_setting(sms.SMS_SENDER, "SteProTECH")
    db.set_setting(ac.ADMIN_PHONE_SETTING, "0241234567")
    yield


# ------------------------------------------------------------------ digits are back

def test_the_sms_code_is_six_digits_again(monkeypatch):
    sent = []
    monkeypatch.setattr(sms.httpx, "post",
                        lambda url, **kw: (sent.append(kw.get("json") or {}), _Res({"status": "success", "code": "2000", "summary": {"_id": "C1"}}))[1])
    ac.admin_send_otp("sms")
    import re as _re
    code = _re.match(r"^([A-Za-z0-9]{6})\b", sent[-1]["message"]).group(1)
    assert code.isdigit() and len(code) == ac.OTP_LEN


def test_the_letter_generator_is_gone():
    assert not hasattr(ac, "_rand_word")
    assert not hasattr(ac, "OTP_ALPHABET")


def test_the_message_is_stewards_exact_template():
    """He supplied this text himself (2026-10-08) and the A/B sent it from a working sender, so it is
    both chosen and proven."""
    msg = sms.otp_message("482915", minutes=5)
    assert msg == ("482915 is your SteProTECH admin code. It expires in 5 minutes. "
                   "Never share it with anyone.")


# ------------------------------------------------------------------ the purpose matters

def test_check_sender_reports_the_registered_purpose(monkeypatch):
    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: _Res(
        {"status": "success", "code": "2000", "summary": {
            "sender_name": "StePro-OTP", "status": "approved", "purpose": "Send OTPs to my clients"}}))
    out = sms.check_sender("StePro-OTP")
    assert out["approved"] is True
    assert out["purpose"] == "Send OTPs to my clients"
    assert out["otp_purpose"] is True


@pytest.mark.parametrize("purpose,otp_typing,expect_mismatch", [
    ("Send OTPs to my clients", False, True),      # the exact trap that cost him this diagnosis
    ("Send OTPs to my clients", True, False),
    ("Notification to Members", True, True),
    ("Notification to Members", False, False),
])
def test_the_status_endpoint_names_a_purpose_route_mismatch(monkeypatch, purpose, otp_typing, expect_mismatch):
    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: _Res(
        {"status": "success", "code": "2000", "summary": {
            "sender_name": "X", "status": "approved", "purpose": purpose}}))
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res({"status": "success", "balance": 100, "wallet": 0}))
    db.set_setting(sms.SMS_OTP_TYPE, "otp" if otp_typing else "")
    out = _client().get("/api/v1/admin/sms/status", headers=_admin()).json()
    assert ("purpose_mismatch" in out) is expect_mismatch


# ------------------------------------------------------------------ the delivery verdict

def test_delivery_status_reads_the_report(monkeypatch):
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res({"status": "success", "report": [
        {"_id": 1, "campaign_id": "AAA", "recipient": "233244775129", "sender": "StePro-OTP",
         "status": "REJECTED", "date_sent": "2026-10-08 00:21:06", "message": "x"},
        {"_id": 2, "campaign_id": "BBB", "recipient": "233244775129", "sender": "SteProTECH",
         "status": "DELIVERED", "date_sent": "2026-10-08 00:36:31", "message": "y"},
    ]}))
    assert sms.delivery_status("BBB")["status"] == "DELIVERED"
    assert sms.delivery_status("AAA")["status"] == "REJECTED"


def test_a_campaign_without_a_report_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res({"status": "success", "report": []}))
    out = sms.delivery_status("NOPE")
    assert out["found"] is False and out["status"] == ""
    assert "no report" in out["reason"].lower()


def test_the_report_call_sends_from_and_to_as_real_query_params(monkeypatch):
    """0.1.92 shipped this broken: the dates were baked into the path, httpx encoded the "?",
    and mNotify answered 422 Validation error. Assert the URL and params, not just the parsing."""
    seen = []

    def fake_get(url, **kw):
        seen.append((url, kw.get("params") or {}))
        return _Res({"status": "success", "report": []})

    monkeypatch.setattr(sms.httpx, "get", fake_get)
    sms.delivery_report(days=1)
    url, params = seen[-1]
    assert url == "https://api.mnotify.com/api/report"
    assert "from" in params and "to" in params and params.get("key")
    assert "?" not in url


def test_delivery_status_without_a_campaign_id_says_so():
    out = sms.delivery_status("")
    assert out["found"] is False and "campaign id" in out["reason"].lower()


def test_the_delivery_endpoint_is_admin_only(monkeypatch):
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res({"status": "success", "report": [
        {"campaign_id": "BBB", "status": "DELIVERED", "sender": "SteProTECH"}]}))
    c = _client()
    assert c.get("/api/v1/admin/sms/delivery?campaign_id=BBB").status_code in (401, 403)
    r = c.get("/api/v1/admin/sms/delivery?campaign_id=BBB", headers=_admin())
    assert r.status_code == 200 and r.json()["status"] == "DELIVERED"


def test_the_test_send_returns_the_campaign_id_the_card_looks_up(monkeypatch):
    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: _Res(
        {"status": "success", "code": "2000", "summary": {"_id": "CAMP-9", "credit_left": 7}}))
    r = _client().post("/api/v1/admin/sms/test", headers=_admin())
    assert r.status_code == 200 and r.json()["campaign_id"] == "CAMP-9"


# ------------------------------------------------------------------ the UI verifier

def _js():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()


def test_the_card_shows_the_delivery_verdict():
    js = _js()
    assert "function testOutcome()" in js
    for word in ("Delivered", "Submitted", "Rejected"):
        assert word in js
    assert "admin/sms/delivery?campaign_id=" in js      # the lookup really happens
    assert "state.smsTest" in js


def test_the_card_names_the_purpose_mismatch():
    js = _js()
    assert "purpose_mismatch" in js
    assert "Registered for:" in js
    assert "Sender ID and routing mode disagree" in js


def test_the_test_button_explains_that_acceptance_is_not_delivery():
    js = _js()
    assert "Checking whether it actually delivers." in js


def test_the_versions_match_the_compose_image():
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    compose = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    tag = re.search(r"n8n-portal-backend:([0-9.]+)", compose).group(1)
    assert "app.js?v=%s" % tag in html and "styles.css?v=%s" % tag in html
