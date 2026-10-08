"""SMS codes are letters, and the message reads like an ordinary text (0.1.91).

Steward, after the rejected sends: "don't you think its the content that makes them suspect
its otp, so lets make it normal, it must not be numbers the otp can just be letters or so".

The A/B proof behind this file is in the portal record: the same plain message that mNotify
REJECTED from the OTP-purpose sender `StePro-OTP` was DELIVERED from `Uptime` and accepted from
`SteProTECH` in the same minute, so the Sender ID was the real blocker. The letters-only code and
the plain wording are the extra precaution he asked for: an SMS that opens with a bare number is
what a spam filter reads as an OTP blast.

Rules pinned here:
  * the SMS code is letters only, six of them, from an alphabet with no look-alike letters;
  * a lower-case code typed by hand still verifies;
  * the email channel keeps its familiar six digits;
  * the message text contains no numeric run that could be read as a code;
  * the code fields in the UI are not numeric-keypad fields any more.
"""
import re

import pytest

from app import db
from app.config import settings
from app.services import account_security as ac, sms


def code_from(message: str) -> str:
    """Pull the code out of the texted message, the way Steward reads it off his phone."""
    return message.split("is ")[1].split(".")[0]


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "letters.db"))
    db.init_db()
    yield


@pytest.fixture
def sent(monkeypatch):
    """Catch the outbound SMS; nothing is really sent."""
    calls = []

    class _Res:
        status_code = 200

        def json(self):
            return {"status": "success", "code": "2000", "summary": {"_id": "C1", "credit_left": 9}}

    monkeypatch.setattr(sms.httpx, "post", lambda url, **kw: (calls.append(kw.get("json") or {}), _Res())[1])
    monkeypatch.setattr(sms.httpx, "get", lambda url, **kw: _Res())
    db.set_setting(sms.SMS_KEY, "test-key")
    db.set_setting(sms.SMS_SENDER, "SteProTECH")      # his live choice after the rejected sends
    db.set_setting(ac.ADMIN_PHONE_SETTING, "0241234567")
    return calls


# ------------------------------------------------------------------ the code itself

def test_the_sms_code_is_letters_only():
    for _ in range(40):
        code = ac._rand_word()
        assert len(code) == ac.OTP_LEN
        assert code.isalpha() and code.isupper()
        assert not any(ch.isdigit() for ch in code)


def test_the_letters_avoid_the_ones_people_read_as_digits():
    alphabet = ac.OTP_ALPHABET
    for confusing in ("I", "L", "O"):
        assert confusing not in alphabet
    assert len(alphabet) == 23


def test_the_email_code_still_uses_digits():
    code = ac._rand_otp()
    assert len(code) == ac.OTP_LEN and code.isdigit()


def test_two_codes_are_not_the_same_code():
    assert len({ac._rand_word() for _ in range(50)}) > 40


# ------------------------------------------------------------------ the message

def test_the_message_does_not_look_like_a_numeric_otp_blast():
    msg = sms.otp_message("KRTMXP", minutes=5)
    assert "KRTMXP" in msg
    assert not msg[0].isdigit()                       # a leading number is the tell
    assert not re.search(r"\d{4,}", msg)              # no four-digit run anywhere
    assert "SteProTECH" in msg
    assert "one-time" not in msg.lower() and "OTP" not in msg


def test_the_texted_code_reaches_the_phone(sent):
    out = ac.admin_send_otp("sms")
    assert out["channel"] == "sms" and out["sent"] is True
    msg = sent[-1]["message"]
    code = code_from(msg)
    assert code.isalpha() and len(code) == ac.OTP_LEN
    assert sent[-1]["sender"] == "SteProTECH"


# ------------------------------------------------------------------ verifying it

def test_a_lettered_code_verifies_even_typed_in_lower_case(sent):
    ac.admin_send_otp("sms")
    code = code_from(sent[-1]["message"])
    assert ac.admin_otp_verify(code.lower()) is True
    # and it is consumed: the same code cannot be used twice
    assert ac.admin_otp_verify(code) is False


def test_a_wrong_lettered_code_is_still_refused(sent):
    ac.admin_send_otp("sms")
    assert ac.admin_otp_verify("ZZZZZZ") is False


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


def test_the_admin_code_field_is_not_a_numeric_keypad_field():
    js = _js()
    i = js.find('id="admin-mfa-code"')
    assert i > 0
    field = js[i - 20:i + 320]
    assert 'inputmode="text"' in field
    assert 'inputmode="numeric"' not in field
    assert 'placeholder="123456"' not in field


def test_the_sms_setup_field_takes_letters_too():
    js = _js()
    i = js.find('data-form="sec-sms-enable"')
    assert i > 0
    block = js[i:i + 700]
    assert 'inputmode="text"' in block
    assert 'autocapitalize="characters"' in block
    assert "6-digit" not in block


def test_the_customer_code_field_is_left_alone():
    """Customer email codes are still digits, so that field keeps its numeric keypad."""
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
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    compose = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
    import re
    tag = re.search(r"n8n-portal-backend:([0-9.]+)", compose).group(1)
    assert "app.js?v=%s" % tag in html and "styles.css?v=%s" % tag in html
