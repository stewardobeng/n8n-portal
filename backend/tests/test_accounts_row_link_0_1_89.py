"""Accounts table: the name opens the account, no View account button (0.1.89).

Steward (2026-10-07): "remove the view account button for the accounts table, and rather when i
click on the account name then it opens the view account page for that particular account. so
the other buttons will fit without the table needing to scroll horizontally".

Pinned here because it is easy to undo by accident: the account NAME is the only way into the
account page from that table, and it must stay a real link (keyboard usable), not a div with a
click handler.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(ROOT, "static", name), encoding="utf-8").read()


def _flat(text):
    return " ".join(text.split())


def test_the_account_name_is_a_real_link_to_that_account():
    js = _read("app.js")
    flat = _flat(js)
    assert ('<td><a class="customer-cell cell-link" href="#/admin/account/\' + a.id + '
            '\'" data-route="/admin/account/\' + a.id + \'" title="Open this account">') in flat
    # the cell keeps its old classes, so the row still looks like every other row
    assert '<span class="avatar">' in flat and "cell-text" in flat


def test_the_view_account_button_is_gone():
    js = _read("app.js")
    flat = _flat(js)
    # the button that used to sit in the actions cell, matched by its label
    assert "View account</button>" not in js, "the button came back; the name is the way in now"
    # the overview cards keep their own, differently worded link
    assert "View accounts " in js
    # and the name link still carries the route
    assert 'data-route="/admin/account/' in flat and 'title="Open this account"' in flat


def test_clicking_a_data_route_element_still_navigates():
    """The name link rides the one delegated dispatcher: prove the branch is intact."""
    flat = _flat(_read("app.js"))
    assert 'var route = event.target.closest("[data-route]"); if (route) { navigate(route.dataset.route); return; }' in flat


def test_the_link_looks_like_the_cell_it_replaced():
    css = _read("styles.css")
    assert ".cell-link { color: inherit; text-decoration: none; }" in css
    assert ".cell-link:hover .cell-text strong { text-decoration: underline; }" in css
    assert ".cell-link:focus-visible" in css          # keyboard users can see the focus ring
    for token in ("--teal-700",):
        assert token in css, "focus ring uses a token that must exist"


def test_the_accounts_table_is_now_eight_columns_without_the_extra_button():
    """Sanity on the widths that motivated the change: the actions cell holds only Login as user."""
    flat = _flat(_read("app.js"))
    assert "<th>Customer</th><th>Plan</th><th>Subscription</th><th>Period end</th><th>Quota</th><th>Backup</th><th>Update</th><th></th>" in flat
    assert '<td class="cell-actions-td"><div class="cell-actions">\' + (a.account_state === "active" ? ' in flat


def test_the_permission_buttons_are_one_word_with_the_full_phrase_kept():
    """The widths that forced the sideways scroll: "Allow backup" + "Allow update" + the row
    action. The verb stays on the button, the sentence moves to the tooltip and the popup."""
    js = _read("app.js")
    assert '(a.backup_enabled ? "Revoke" : "Allow") + "</button></div></td>"' in js
    assert '(a.update_enabled ? "Revoke" : "Allow") + "</button></div></td>"' in js
    for phrase in ("Allow backup access for this account",
                   "Revoke backup access for this account",
                   "Allow update access for this account",
                   "Revoke update access for this account"):
        assert phrase in js, phrase
    # labelled for screen readers too, since the visible text is now one word
    assert js.count('aria-label="\' + (a.') == 2
    assert 'title="Login as user" aria-label="Login as user"' in js


def test_the_tighter_gutter_only_applies_to_the_accounts_table():
    css = _read("styles.css")
    assert ".accounts-table th, .accounts-table td { padding-left: 10px; padding-right: 10px; }" in css
    assert ".accounts-table th:last-child, .accounts-table td:last-child { padding-right: 4px; }" in css
    assert "th, td { padding: 14px 20px;" in css          # every other table is untouched
    assert '<table class="accounts-table">' in _read("app.js")
