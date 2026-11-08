"""The code format, and everything that must not depend on it (0.1.91 -> 0.1.92).

History worth keeping: 0.1.91 made SMS codes letters on the theory that a numeric code in a text
message reads as an OTP blast and gets dropped. The controlled A/B on 2026-10-08 disproved it -
from a working Sender ID the numeric-leading message DELIVERED, while from the OTP-purpose sender
even the lettered message was REJECTED - so 0.1.92 put the digits back (Steward: "can we bring back
the numbers only otp").

What survives, and what this file pins:
  * the format is not load-bearing: the message builder and the verifier work with any code;
  * verification is case-insensitive, so a lettered code typed in lower case would still work;
  * the code is stored only as a salted hash, in one slot, and is consumed on use;
  * the two code fields accept either format, so a future format change needs no UI work;
  * the customer code field keeps its numeric keypad, because customer codes are digits.
"""
import pytest

from app import db
from app.config import settings
from app.services import account_security as ac, sms


def code_from(message: str) -> str:
    """Pull the code out of the texted message, the way Steward reads it off his phone."""
    import re
    m = re.match(r"^([A-Za-z0-9]{6})\b", message)
    assert m, "the code should lead the message: %r" % message
    return m.group(1)


class _Res:
    status_code = 200

    def json(self):
        return {"status": "success", "code": "2000", "summary": {"_id": "C1", "credit_left": 9}}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "format.db"))
    db.init_db()
    yield


@pytest.fixture
def sent(monkeypatch):
    """Catch the outbound SMS; nothing is really sent."""
    calls = []
    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: (calls.append(kw.get("json") or {}), _Res())[1])
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res())
    db.set_setting(sms.SMS_KEY, "test-key")
    db.set_setting(sms.SMS_SENDER, "SteProTECH")      # his live choice after the rejected sends
    db.set_setting(ac.ADMIN_PHONE_SETTING, "0241234567")
    return calls


# ------------------------------------------------------------------ the format today

def test_the_live_code_is_six_digits():
    for _ in range(30):
        code = ac._rand_otp()
        assert len(code) == ac.OTP_LEN and code.isdigit()


def test_two_codes_are_not_the_same_code():
    assert len({ac._rand_otp() for _ in range(50)}) > 40


def test_the_texted_code_reaches_the_phone(sent):
    out = ac.admin_send_otp("sms")
    assert out["channel"] == "sms" and out["sent"] is True
    code = code_from(sent[-1]["message"])
    assert code.isdigit() and len(code) == ac.OTP_LEN
    assert sent[-1]["sender"] == "SteProTECH"


# ------------------------------------------------------------------ format independence

@pytest.mark.parametrize("code", ["482915", "KRTMXP", "A1B2C3"])
def test_the_message_builder_takes_any_code(code):
    msg = sms.otp_message(code, minutes=5)
    assert code in msg
    assert msg.endswith("Never share it with anyone.")


def test_verification_does_not_care_about_case(sent):
    """Digits are unaffected by .upper(), but the verifier must not break if letters ever return."""
    ac.admin_send_otp("sms")
    code = code_from(sent[-1]["message"])
    assert ac.admin_otp_verify(code.lower()) is True
    assert ac.admin_otp_verify(code) is False    # consumed: one use per code


def test_a_wrong_code_is_refused(sent):
    ac.admin_send_otp("sms")
    wrong = "999999" if code_from(sent[-1]["message"]) != "999999" else "111111"
    assert ac.admin_otp_verify(wrong) is False


def test_the_code_is_stored_as_a_hash_not_as_text(sent):
    ac.admin_send_otp("sms")
    code = code_from(sent[-1]["message"])
    stored = db.get_setting(ac.ADMIN_OTP_HASH) or ""
    assert code not in stored          # never stored in the clear
    assert len(stored) >= 64           # a salted hash, not the code


# ------------------------------------------------------------------ the UI fields

def _js():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()


def test_the_admin_code_field_accepts_either_format():
    js = _js()
    i = js.find('id="admin-mfa-code"')
    assert i > 0
    field = js[i - 20:i + 320]
    assert 'inputmode="text"' in field          # not a numeric keypad, so letters would work too
    assert 'placeholder="123456"' not in field


def test_the_sms_setup_field_accepts_either_format():
    js = _js()
    i = js.find('data-form="sec-sms-enable"')
    assert i > 0
    block = js[i:i + 700]
    assert 'inputmode="text"' in block
    assert 'autocapitalize="characters"' in block
    assert "6-digit" not in block


def test_the_customer_code_field_keeps_its_numeric_keypad():
    """Customer email codes are digits, so that field is left alone."""
    js = _js()
    i = js.find('id="mfa-code"')
    assert i > 0
    assert 'inputmode="numeric"' in js[i:i + 200]


def test_the_otp_toggle_warns_about_the_separate_wallet():
    js = _js()
    assert "MAIN WALLET" in js
    assert "separate purse from these SMS credits" in js
    assert "bal.wallet" in js


def test_the_versions_match_the_compose_image():
    import os
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    compose = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    tag = _re.search(r"n8n-portal-backend:([0-9.]+)", compose).group(1)
    assert "app.js?v=%s" % tag in html and "styles.css?v=%s" % tag in html
