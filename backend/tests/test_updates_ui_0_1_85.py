"""Updates page and the self-update button (0.1.85, Steward 2026-10-07).

Static guards for the front end, next to the queue's own tests. The rules the page
must keep telling the truth about:
  * Steward's pacing rule is stated on the page, not just implemented;
  * a GHS 300 workspace shows no self-update button at all;
  * nothing is claimed before /me/updates has answered (the 0.1.84 rule again);
  * the customer's button says it QUEUES (with their place in the line), because it
    never starts on click;
  * an undetermined version is never sold as "needs update".
"""
import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def _src():
    return APP_JS.read_text(encoding="utf-8")


def _flat():
    return re.sub(r"\s+", " ", _src())


def test_the_asset_version_matches_the_release():
    """A deploy must not leave a browser on a cached app.js (the rig hit exactly that:
    the fixed file was on disk while the page ran the previous one). The query string
    and the image tag are kept in step by this test."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    ver = re.search(r"n8n-portal-backend:(\S+)", compose).group(1)
    assert ("app.js?v=" + ver) in html, ("index.html must cache-bust app.js at " + ver)
    assert ("styles.css?v=" + ver) in html


def test_the_pacing_interval_reads_correctly_when_it_is_one_minute():
    src = _src()
    assert '(Number(d.interval_minutes) === 1 ? " minute</b>" : " minutes</b>")' in src


def test_the_page_exists_and_is_reachable_from_the_sidebar():
    src = _src()
    assert '["updates", "Updates", "/admin/updates"]' in src
    assert 'route === "/admin/updates") html = requireAdmin(function () { return adminUpdates(); })' in src
    assert "function adminUpdates() {" in src


def test_the_page_states_the_pacing_rule_before_the_admin_acts():
    src = _src()
    rule = src[src.find("function adminUpdates()"):]
    rule = rule[:rule.find("function adminBackups()")]
    for phrase in ("One workspace updates at a time <b>on each server</b>",
                   "before its next one starts",
                   "ahead of customer requests",
                   "starts immediately</b> and takes that server's slot",
                   "No safety backup is taken before an update (your instruction)"):
        assert phrase in rule, phrase


def test_the_page_loads_with_an_announced_loading_state():
    src = _src()
    i = src.find("if (!state.adminUpdatesLoaded) {")
    assert i > 0
    assert 'loadingBlock("Loading the update queue..."' in src[i:i + 600]
    assert "state.adminUpdatesLoaded = true;" in src


def test_the_page_helpers_are_passed_their_data_not_reaching_for_it():
    """Live rig bug 2026-10-07: updateStateBadge() referenced the page's own `d`, which
    is not in its scope, so every render threw and the page froze on its loading state.
    Rendering is checked for scope: helpers get their data as arguments."""
    src = _src()
    i = src.find("function updateStateBadge(")
    body = src[i:src.find("function updateRowHtml(", i)]
    assert "latestKnown" in body, "the flag is a parameter"
    assert "d.latest_known" not in body, "the helper must not reach for page data"
    row = src[src.find("function updateRowHtml("):src.find("function ", src.find("function updateRowHtml(") + 20)]
    assert "d.latest_known" not in row, "the row builder must not reach for page data either"
    assert "updateStateBadge(w, latestKnown)" in row
    assert "updateRowHtml(w, sel, d.latest_known)" in src, "the page passes it in (only here)"


def test_a_downed_release_check_is_stated_and_offers_nothing():
    """If the latest release cannot be read there is nothing to compare: the page must
    say so rather than badge every workspace "Up to date" (caught live 2026-10-07)."""
    src = _src()
    assert 'if (latestKnown === false) return statusBadge("warning", "Cannot compare");' in src
    assert "behind.length && d.latest_known ?" in src, "no queue button while nothing can be compared"
    assert "esc(d.compare_note)" in src, "the page renders the API's reason for the admin"
    flat = _flat()
    assert "Version check unavailable" in flat, "the customer sees a note, not a false all-clear"


def test_a_failed_load_says_so_instead_of_looking_empty():
    """An unanswered request must not render as an empty queue (same class as every
    other loading fix): the page states the failure and offers Refresh."""
    src = _src()
    assert 'state.adminUpdates = state.adminUpdates ||' in src
    assert "load_error" in src
    assert "The update queue could not" in src and "Use Refresh to try again." in src


def test_the_page_offers_selection_and_everything_behind():
    flat = _flat()
    assert 'data-action="updates-queue-selected"' in flat
    assert 'data-action="updates-queue-all"' in flat
    assert 'data-upd-check' in flat
    assert "Update all that need it (" in flat
    # the ticked boxes survive the 15 second refresh
    assert 'state.updSel[t.value] = true' in flat
    assert 'state.updSel = {};' in flat


def test_the_queue_poll_only_runs_while_the_page_is_open():
    flat = _flat()
    assert 'if (route === "/admin/updates") {' in flat
    assert "state.updatesPoll = setInterval(" in flat
    assert "clearInterval(state.updatesPoll)" in flat
    assert "}, 15000);" in flat


def test_an_unknown_version_is_never_shown_as_needing_an_update():
    src = _src()
    assert 'if (w.version_unknown) return statusBadge("neutral", "Version unknown");' in src
    # the page says how many are unknown instead of quietly leaving them out
    assert "report an undetermined version" in src


def test_the_self_update_button_is_only_for_the_plus_package():
    flat = _flat()
    assert 'if (!info || info.plan !== "b") return "";' in flat
    assert 'data-action="ws-self-update"' in flat
    # and nothing at all is shown until /me/updates has answered
    assert "if (!state.myUpdatesLoaded || !inst) return null;" in flat


def test_the_customer_is_told_that_it_queues_and_where_they_are():
    flat = _flat()
    assert "Queued: " in flat
    assert 'position " + q.position + " on your server' in flat
    assert "it QUEUES, it never starts on click" in flat
    assert "Your workspace is in the line: position" in flat
    assert "We will email you the moment it is done." in flat


def test_a_queued_or_gated_workspace_offers_no_button():
    flat = _flat()
    # queued -> a status, not a button; blocked -> the reason, not a button
    assert 'if (info.queued) {' in flat
    assert 'if (info.blocked_reason) {' in flat
    # per-row Update is hidden while a row is already in the line
    assert '(w.queued ? ""' in flat
