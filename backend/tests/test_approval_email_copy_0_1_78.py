# 0.1.78 (Steward, 2026-10-01): the approval email copy was polished and approved.
# It must stay free of two things he judged unnecessary (any mention of a code, and
# any mention of being signed in automatically) and of em dashes (house rule).

from app.services import emailer


def _rendered(monkeypatch):
    sent = {}

    def fake_send(to, subject, html, text=None):
        sent["to"] = to
        sent["subject"] = subject
        sent["html"] = html

    monkeypatch.setattr(emailer, "send_email", fake_send)
    emailer.send_access_approved("person@example.com")
    return sent


def test_subject_and_approved_copy(monkeypatch):
    s = _rendered(monkeypatch)
    assert s["to"] == "person@example.com"
    assert s["subject"] == "Your access to the SteProTECH n8n portal is approved"
    html = s["html"]
    assert "Your access is approved" in html
    assert "An administrator has approved your request to join the SteProTECH n8n" in html
    # the four steps, including choosing a plan as the final step
    for step in ("Open the portal at", "Enter this email address",
                 "Create your account with your name and a password",
                 "Choose a plan to set up your first workspace"):
        assert step in html, step
    assert "portal.steprotech.com" in html


def test_copy_says_nothing_about_a_code_or_auto_sign_in(monkeypatch):
    html = _rendered(monkeypatch)["html"].lower()
    assert "code" not in html            # no code exists in this flow
    assert "signed in" not in html       # Steward: not necessary to mention
    assert "log in" not in html and "login" not in html


def test_no_em_dashes(monkeypatch):
    s = _rendered(monkeypatch)
    assert "\u2014" not in s["subject"] and "\u2014" not in s["html"]
