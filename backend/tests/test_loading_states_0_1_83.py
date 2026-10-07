# 0.1.83 (2026-10-07): finish the flash-of-misinformation class on the portal.
#
# Steward, after 0.1.82: "fix that also" - the customer side and the account
# page's allowance. Both rendered claims derived from data that had not arrived:
#   * customer pages painted BEFORE /me answered, so a paying customer saw
#     "No workspaces yet" + "0 of 1 used" + "Subscribe and provision..." at once
#     (measured on the local rig against the pre-0.1.83 build: all three at +463ms,
#     truth at +546ms; the admin-side gap over the internet is ~0.7-1.1s),
#   * the admin account page's Quota card showed "0 / 1" and "The customer may
#     provision another workspace." while `used` was still unknown (measured: false
#     values at +92ms, truth at +670ms).
#
# Structural guards on app.js (no JS harness in this repo). Behaviour verified in a
# real browser: fixed build -> customer page shows "Loading your account..." then the
# real data with no false claim, and the Quota card shows "Checking..." then "1 / 1".

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
SRC = APP_JS.read_text(encoding="utf-8")


# ---------- customer side: /me gates the page ----------

def test_session_load_is_marked_on_every_exit():
    """Success, no-token and failure must all mark it, or the customer is either
    left on a loading screen forever or served a page of zeros."""
    assert "sessionLoaded: false" in SRC
    assert "state.session = null; state.sessionLoaded = true; return null;" in SRC
    assert "state.session = { token: token, account: data.account, instances: data.instances || [] };\n      state.sessionLoaded = true;" in SRC
    assert SRC.count("state.sessionLoaded = true;") == 3


def test_customer_pages_are_gated_before_first_paint():
    i = SRC.find("function requireCustomer(fn)")
    assert i > 0
    body = SRC[i:i + 900]
    assert "if (!state.sessionLoaded)" in body
    assert "Loading your account..." in body
    assert 'role="status"' in body and 'aria-live="polite"' in body


def test_gate_re_arms_when_the_identity_changes():
    """Impersonation swaps the customer, and signout drops them: the next paint
    must wait for the new /me, not reuse the previous answer."""
    assert "state.session = null;\n    state.sessionLoaded = false;" in SRC          # impersonate start
    assert "state.session = null;\n    state.sessionLoaded = false;\n    state.provisioningPw = null;" in SRC  # impersonation exit
    assert SRC.count("state.session = null; state.sessionLoaded = false; state.provisioningPw = null;") == 2  # both signout branches


# ---------- admin side: the Quota card ----------

def test_quota_card_stops_asserting_before_the_workspaces_arrive():
    i = SRC.find('<h2>Quota</h2>')
    assert i > 0
    card = SRC[i:i + 1100]
    assert '(wsKnown ? used + " / " + quota : "Checking...")' in card, "the count must not read 0 / 1 while unknown"
    assert card.count("(wsKnown ? ") == 3, "count, bar and sentence must all be gated"
    assert 'The customer may provision another workspace.' in card


def test_only_one_copy_of_the_allowance_verdict():
    """An ungated duplicate would reintroduce the false statement."""
    assert SRC.count("The customer may provision another workspace.") == 1
    assert SRC.count("Quota fully used.") == 1
