# 0.1.82 (2026-10-07): the admin account page's Workspaces card must not claim
# "No workspaces yet" while the workspace list is still being fetched.
#
# Steward's report (Strata bug doc, "flash of misinformation"): opening a customer
# who HAS a workspace briefly showed the empty state, then the real workspace.
# Measured on the local rig against the unfixed build: false claim at +18ms,
# real list at +1107ms - 1.09s of a wrong statement.
#
# Cause: the account row arrives with the accounts LIST, but the workspace list
# arrives later in a SECOND request (`GET /accounts/{id}` -> accountCache[id]),
# so `instances` was [] during the gap and the empty state rendered.
#
# There is no JS test harness in this repo, so these are structural guards on
# app.js: they pin the shape of the fix so a future edit cannot quietly put the
# empty state back in front of an unknown list. The behaviour itself was verified
# in a real browser (rig, three runs: with/without the fix + a zero-workspace
# account, timings recorded).

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
SRC = APP_JS.read_text(encoding="utf-8")

EMPTY_CLAIM = "The customer has not provisioned a workspace."


def test_card_knows_whether_the_list_has_loaded():
    """The card needs a flag that only says 'we know' once the per-account read
    has actually answered."""
    assert "var wsKnown = !!(detail && detail.instances)" in SRC
    assert "state.accountDetailTried" in SRC


def test_detail_read_marks_itself_finished_even_when_it_fails():
    """Otherwise a failed read would leave the card loading forever."""
    i = SRC.find("state.accountDetailTried[id] = true;")
    assert i > 0, "the per-account read must mark itself finished"
    # it sits after the try/catch, not inside the try
    before = SRC[max(0, i - 400):i]
    assert "catch (e) { /* public per-account read failed */ }" in before


def test_empty_state_is_gated_behind_wsKnown():
    """The admin empty state must be reachable ONLY when the list is known."""
    i = SRC.find(EMPTY_CLAIM)
    assert i > 0
    opening = SRC[max(0, i - 320):i]
    assert ": (wsKnown" in opening, "the empty state must be inside the wsKnown branch"
    # the loading frame is the wsKnown ternary's other branch, right after it
    assert "Loading workspaces..." in SRC[i:i + 420], "a loading frame must be the alternative"


def test_loading_frame_is_announced_to_screen_readers():
    # Scoped to the Workspaces CARD's loader: 0.1.84 added other "Loading
    # workspaces..." frames elsewhere in the file.
    i = SRC.find("Loading workspaces...", SRC.find("(wsKnown"))
    assert i > 0
    markup = SRC[max(0, i - 260):i + 120]
    assert 'role="status"' in markup
    assert 'aria-live="polite"' in markup


def test_no_second_ungated_copy_of_the_admin_claim():
    """Exactly one occurrence, and it is the gated one (a duplicate outside the
    guard would reintroduce the flash)."""
    assert SRC.count(EMPTY_CLAIM) == 1


def test_stale_cache_is_cleared_on_signout():
    assert "state.accountDetailTried = {};" in SRC
