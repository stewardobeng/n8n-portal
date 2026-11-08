"""0.1.84 (2026-10-07) - every table and list says it is loading.

Steward: "almost all tables that must be loaded show that same generic message
that none exists until it loads, so check every table display and fix so the user
will know its loading, for access requests, backups tables etc and others".

The 0.1.82/0.1.83 work fixed two spots one at a time. This release does the whole
class: each list/table that fills in asynchronously is gated on a flag that only
becomes true once its request has ANSWERED (success or failure - a failed read
must show the honest "could not be listed" text, not a loading screen for ever).

Two layers of guard below:
  * a generic rule that scans the whole file - every empty-state must sit within
    400 characters of a loading flag, with an explicit, justified exception list;
  * per-spot structure checks, so a later edit cannot quietly drop a gate.
"""
import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"

FLAGS = (
    "sessionLoaded", "backupsLoaded", "wsKnown", "adminAccountsLoaded",
    "requestsLoaded", "adminArchivedLoaded", "adminBackupsLoaded",
    "adminInstancesLoaded", "adminEnvsLoaded",
)

# Empty states that are deliberately NOT next to a flag, and why:
#   * the loader's own markup (it is the loading state);
#   * the customer pages, held by the single requireCustomer gate (0.1.83);
#   * the attach modal, which is only ever built after its scan has returned.
UNGUARDED_BY_DESIGN = {
    "Loading workspaces...",       # the loader itself
    '" + esc(title) + "',          # the loader's own title expression
    "No workspaces yet",           # customer page, central sessionLoaded gate
    "No unattached workspaces",    # attach modal, opens after the scan returns
}


def _src():
    return APP_JS.read_text(encoding="utf-8")


def _flat():
    """Source with whitespace collapsed, so assertions survive reformatting."""
    return re.sub(r"\s+", " ", _src())


# ---------------------------------------------------------------- the helpers

def test_loading_helpers_exist_and_announce_themselves():
    src = _src()
    assert src.count("function loadingBlock(") == 1
    assert src.count("function loadingRow(") == 1
    # aria-live: a screen reader must hear that data is on the way, not silence
    assert 'role="status" aria-live="polite"' in src
    flat = _flat()
    assert 'function loadingRow(colspan, title, hint, minHeight) { return \'<tr><td colspan="\' + colspan + \'">\' + loadingBlock(title, hint, minHeight) + "</td></tr>"; }' in flat


# ------------------------------------------- the generic, self-maintaining rule

def test_every_empty_state_is_next_to_a_loading_flag():
    src = _src()
    unexplained = []
    for m in re.finditer(r'class="empty-state"', src):
        i = m.start()
        back = src[max(0, i - 400):i]
        if any(f in back for f in FLAGS):
            continue
        title = re.search(r"<h3>([^<]+)</h3>", src[i:i + 300])
        title = title.group(1) if title else "?"
        if title not in UNGUARDED_BY_DESIGN:
            unexplained.append(title)
    assert unexplained == [], (
        "these empty states can still be shown before their data arrives: %r" % unexplained
    )


def test_the_exception_list_is_still_accurate():
    """If one of the justified exceptions gains a gate, drop it from the list."""
    flat = _flat()
    # the customer page claims are covered centrally
    assert "if (!state.sessionLoaded) {" in flat
    # the attach modal is only reached after the scan answered
    assert flat.count('showModal(modalHeader("Finding workspaces")') == 2


def test_honest_empty_states_were_not_removed():
    src = _src()
    for claim in ("No backups yet", "No access requests", "No matching requests",
                  "No archived accounts", "No customer accounts yet",
                  "No environments found", "No workspaces yet",
                  "There are no workspaces to back up right now."):
        assert claim in src, claim


# ------------------------------------------------------------ tables and lists

def test_access_requests_tables_gate_on_requests_loaded():
    flat = _flat()
    # admin overview tab
    assert "(state.requestsLoaded ? '<tr><td colspan=\"3\"><div class=\"empty-state\"" in flat
    assert 'loadingRow(3, "Loading access requests...", "", 160)' in flat
    # requests page
    assert "(state.requestsLoaded ? '<tr><td colspan=\"4\"><div class=\"empty-state\"" in flat
    assert 'loadingRow(4, "Loading access requests...", "", 220)' in flat


def test_archived_and_accounts_tables_gate_on_their_flags():
    flat = _flat()
    assert "(state.adminArchivedLoaded ? '<tr><td colspan=\"4\"><div class=\"empty-state\"" in flat
    assert 'loadingRow(4, "Loading archived accounts...", "", 220)' in flat
    assert "(state.adminAccountsLoaded ? '<tr><td colspan=\"8\"><div class=\"empty-state\"" in flat
    assert 'loadingRow(8, "Loading customer accounts...", "", 240)' in flat


def test_backup_tables_gate_on_their_flags():
    flat = _flat()
    # admin backup history
    assert "(rows || (state.adminBackupsLoaded ? '<div class=\"empty-state\"" in flat
    assert 'loadingBlock("Loading backups...", "", 160)' in flat
    # the "create a backup" workspace list waits for /admin/instances
    assert "(creatable || (state.adminInstancesLoaded ?" in flat
    assert 'loadingBlock("Loading workspaces...", "", 160)' in flat
    # customer backup history
    assert "var listHtml = rows ? rows : (state.backupsLoaded ?" in flat
    assert 'loadingBlock("Loading backups...", "Fetching your workspace backups.", 240)' in flat


def test_settings_server_list_and_overview_env_cards_gate_on_admin_envs_loaded():
    flat = _flat()
    assert "|| (state.adminEnvsLoaded ? '<div class=\"empty-state\"" in flat
    assert 'loadingBlock("Loading servers...", "Asking each n8n server for its state.", 160)' in flat
    assert "if (!envs.length && !state.adminEnvsLoaded) {" in flat


def test_overview_counters_do_not_print_zero_before_the_lists_arrive():
    flat = _flat()
    assert "var acctKnown = state.adminAccountsLoaded;" in flat
    assert "var reqKnown = state.requestsLoaded;" in flat
    # 0.1.98: the first counter is people who opened the form and have not finished
    # registering ('approved'/'token_sent' rows), because nothing waits for an
    # approval any more and the old 'requested' counter would sit at zero forever.
    assert "var pendingSignup = requests.filter" in flat
    for var, flag in (("pendingSignup", "reqKnown"), ("pastDue", "acctKnown"),
                      ("locked", "acctKnown"), ("total", "acctKnown")):
        assert '+ (' + flag + ' ? ' + var + ' : "...") +' in flat, var


def test_attach_button_reports_that_it_is_scanning():
    flat = _flat()
    assert flat.count('showModal(modalHeader("Finding workspaces")') == 2
    assert "Checking the n8n servers for workspaces that are not attached to an account yet..." in flat


# ------------------------------------------------- the flags are actually set

def test_each_flag_is_marked_when_its_request_answers_either_way():
    src = _src()
    # one mark where the request answers, plus the degrade path in the refresh catch
    assert src.count("state.requestsLoaded = true;") == 2
    assert src.count("state.adminArchivedLoaded = true;") == 2
    assert src.count("state.adminEnvsLoaded = true;") == 2
    assert src.count("state.adminBackupsLoaded = true;") == 2   # on the page read and after the try
    assert src.count("state.backupsLoaded = true;") == 3        # boot read, lazy read, lazy failure
    # the lazy /me/backups read also ends the loading state when it fails
    assert 'state.backups = state.backups || []; state.backupsLoaded = true; render();' in src


def test_a_broken_admin_refresh_degrades_instead_of_hanging_on_loading():
    flat = _flat()
    tail = flat[flat.find("async function refreshAdminData()"):]
    tail = tail[:tail.find("async function loadPlans()")]
    assert "state.adminAccountsLoaded = true; state.requestsLoaded = true;" in tail.replace("\n", " ") \
        or "state.adminAccountsLoaded = true;" in tail
    assert "state.adminEnvsLoaded = true;" in tail


def test_identity_changes_clear_the_loading_flags():
    src = _src()
    # admin sign out: the next admin must not see the previous one's tables
    flat = _flat()
    assert "state.adminInstancesLoaded = false; state.requests = []; state.requestsLoaded = false; state.adminArchived = []; state.adminArchivedLoaded = false;" in flat
    assert "state.adminEnvs = []; state.adminEnvsLoaded = false; state.adminBackups = []; state.adminBackupsLoaded = false;" in flat
    for flag in ("state.requestsLoaded = false", "state.adminArchivedLoaded = false",
                 "state.adminEnvsLoaded = false", "state.adminBackupsLoaded = false"):
        assert flag in src, flag
    # customer side: every identity change drops the previous backups too
    assert src.count("state.backupsLoaded = false; state.backups = [];") >= 4
