/* SteProTECH n8n Workspace Portal - production app
 * Design system: n8n-portal-ui-ux (designers' prototype, kept as visual source of truth)
 * Data layer: wired to the live FastAPI backend (/api/v1). Backend unchanged.
 * Auth: portal_token (customer) / admin_token (admin) in localStorage.
 */
(function () {
  "use strict";

  var API = "/api/v1";

  /* ================= state (live, loaded from backend) ================= */
  var state = {
    session: null,        // { token, account, instances } customer session
    adminAuthed: false,
    plans: [],
    paymentsOpen: true,       // /plans -> master payments switch (admin hold)
    adminPaymentsOpen: true,  // /admin/settings -> same switch, admin view
    gateEmail: null,
    gateToken: null,
    environmentList: [],  // [{id, name, status}] from GET /environments
    envOrder: [],         // ordered env ids from admin settings
    accountCache: {},     // admin: accountId -> {account, instances}
    requests: [],         // admin: access requests
    adminEnvs: [],        // admin: n8n Server 1..N + health (GET /admin/environments)
    adminArchived: [],    // admin: archived accounts (GET /admin/accounts?include_archived=1)
    unlinkedStacks: [],   // admin: attach candidates (GET /admin/stacks/unlinked)
    menuOpen: false,
    pollTimer: null,
    provisioningPw: null, // signup password held in-session for owner auto-create
    mfa: null,             // 2FA challenge in-flight (email, challenge, methods)
    adminMfa: null,
    adminMfaSent: null,
    smsStatus: null,
    smsTest: null,        // admin 2FA challenge in-flight (challenge, methods)
    security2fa: null,     // 2FA setup state (GET /me/security or /admin/security)
    adminInstances: [],    // admin: all instances (GET /admin/instances) for backup/update triggers
    adminInstancesLoaded: false,
    sessionLoaded: false, // customer: /me has answered (or failed) at least once
    requestsLoaded: false,       // admin: the access-request list has answered
    myUpdates: [],               // customer: version/self-update state per workspace (0.1.85)
    myUpdatesLoaded: false,      // customer: /me/updates has answered
    myUpdatesAllowed: false,     // customer: the admin allowed self-update for this account (0.1.88)
    adminUpdates: null,          // admin: the update queue overview (0.1.85)
    adminUpdatesLoaded: false,
    updSel: {},                  // admin: workspaces ticked on the Updates page
    adminArchivedLoaded: false,  // admin: the archived-account list has answered
    adminEnvsLoaded: false,      // admin: the per-server environment list has answered
    adminAccountsLoaded: false,
    accountDetailTried: {}, // admin: accountId -> the per-account read has finished (ok or failed)
    impersonation: null,     // admin login-as: {id, label} while viewing a customer
    impTtlMinutes: 60,       // lifetime of the impersonation session (server-set)
    n8nLatestTag: "",        // latest n8n release tag seen (admin update watch)
    n8nVersions: {},         // admin: instanceId -> {version, source} actually running (0.1.80)
    instUpdating: {},        // instanceId -> target tag while an n8n update runs
    instRestarting: {},      // instanceId -> true while a restart is in flight (0.1.96)
    runnerBusy: {},          // instanceId -> "attach"|"detach" while a python-runner change runs
    sandboxBusy: {},         // instanceId -> "attach"|"detach" while an AI-sandbox change runs
    busyModal: false         // true while a long-running action modal is open (render must not wipe it)
  };

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var appEl = $("#app");
  var modalRoot = $("#modal-root");
  var toastRoot = $("#toast-root");

  /* ================= tiny helpers ================= */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtMinor(minor, cur) {
    return (Number(minor || 0) / 100).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + " " + (cur || "");
  }
  function fmtDate(ts) {
    if (!ts) return "-";
    return new Date(Number(ts) * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  function fmtDateTime(ts) {
    if (!ts) return "-";
    return new Date(Number(ts) * 1000).toLocaleString();
  }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function titleCase(s) { return String(s || "").replace(/[-_]+/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); }); }

  /* ================= API layer ================= */
  async function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers["Accept"] = "application/json";
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    var token = localStorage.getItem("admin_token") || localStorage.getItem("portal_token");
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    var res = await fetch(API + path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      if (detail && typeof detail === "object") detail = JSON.stringify(detail);
      var err = new Error(detail || "Request failed (" + res.status + ")");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ================= icons + badges (Lucide inline SVG, ISC) ================= */
/* Lucide icons (ISC) — inline SVG for consistent real icons */
var ICONS = {
  account: '<circle cx="12" cy="12" r="10" /> <circle cx="12" cy="10" r="3" /> <path d="M7 20.662V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1.662" />',
  accounts: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /> <circle cx="9" cy="7" r="4" /> <path d="M22 21v-2a4 4 0 0 0-3-3.87" /> <path d="M16 3.13a4 4 0 0 1 0 7.75" />',
  arrow: '<path d="M5 12h14" /> <path d="m12 5 7 7-7 7" />',
  "arrow-left": '<path d="m12 19-7-7 7-7" /> <path d="M19 12H5" />',
  phone: '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" />',
  message: '<path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 0 1-4-.84L3 21l1.05-4.2A7.7 7.7 0 0 1 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />',
  archive: '<rect width="20" height="5" x="2" y="3" rx="1" /> <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" /> <path d="M10 12h4" />',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1" /> <rect x="6" y="4" width="4" height="16" rx="1" />',
  bell: '<path d="M10.268 21a2 2 0 0 0 3.464 0" /> <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326" />',
  billing: '<rect width="20" height="14" x="2" y="5" rx="2" /> <line x1="2" x2="22" y1="10" y2="10" />',
  check: '<path d="M20 6 9 17l-5-5" />',
  clock: '<circle cx="12" cy="12" r="10" /> <polyline points="12 6 12 12 16 14" />',
  close: '<path d="M18 6 6 18" /> <path d="m6 6 12 12" />',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2" /> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />',
  dashboard: '<rect width="7" height="9" x="3" y="3" rx="1" /> <rect width="7" height="5" x="14" y="3" rx="1" /> <rect width="7" height="9" x="14" y="12" rx="1" /> <rect width="7" height="5" x="3" y="16" rx="1" />',
  down: '<path d="m6 9 6 6 6-6" />',
  drag: '<circle cx="9" cy="12" r="1" /> <circle cx="9" cy="5" r="1" /> <circle cx="9" cy="19" r="1" /> <circle cx="15" cy="12" r="1" /> <circle cx="15" cy="5" r="1" /> <circle cx="15" cy="19" r="1" />',
  error: '<circle cx="12" cy="12" r="10" /> <path d="m15 9-6 6" /> <path d="m9 9 6 6" />',
  external: '<path d="M15 3h6v6" /> <path d="M10 14 21 3" /> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /> <path d="m7 10 5 5 5-5" /> <path d="M12 15V3" />',
  eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0" /> <circle cx="12" cy="12" r="3" />',
  eyeoff: '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49" /> <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242" /> <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" /> <path d="m2 2 20 20" />',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2" /> <path d="M7 11V7a5 5 0 0 1 10 0v4" />',
  mail: '<rect width="20" height="16" x="2" y="4" rx="2" /> <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />',
  maintenance: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" /> <path d="M21 3v5h-5" />',
  menu: '<line x1="4" x2="20" y1="12" y2="12" /> <line x1="4" x2="20" y1="6" y2="6" /> <line x1="4" x2="20" y1="18" y2="18" />',
  more: '<circle cx="12" cy="12" r="1" /> <circle cx="19" cy="12" r="1" /> <circle cx="5" cy="12" r="1" />',
  overview: '<rect width="7" height="9" x="3" y="3" rx="1" /> <rect width="7" height="5" x="14" y="3" rx="1" /> <rect width="7" height="9" x="14" y="12" rx="1" /> <rect width="7" height="5" x="3" y="16" rx="1" />',
  plus: '<path d="M5 12h14" /> <path d="M12 5v14" />',
  requests: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12" /> <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />',
  server: '<rect width="20" height="8" x="2" y="2" rx="2" ry="2" /> <rect width="20" height="8" x="2" y="14" rx="2" ry="2" /> <line x1="6" x2="6.01" y1="6" y2="6" /> <line x1="6" x2="6.01" y1="18" y2="18" />',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /> <path d="M21 3v5h-5" /> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" /> <path d="M8 16H3v5" />',
  settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /> <circle cx="12" cy="12" r="3" />',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" /> <path d="m9 12 2 2 4-4" />',
  signout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /> <polyline points="16 17 21 12 16 7" /> <line x1="21" x2="9" y1="12" y2="12" />',
  spark: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" /> <path d="M20 3v4" /> <path d="M22 5h-4" /> <path d="M4 17v2" /> <path d="M5 18H3" />',
  support: '<circle cx="12" cy="12" r="10" /> <path d="m4.93 4.93 4.24 4.24" /> <path d="m14.83 9.17 4.24-4.24" /> <path d="m14.83 14.83 4.24 4.24" /> <path d="m9.17 14.83-4.24 4.24" /> <circle cx="12" cy="12" r="4" />',
  up: '<path d="m18 15-6-6-6 6" />',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /> <circle cx="9" cy="7" r="4" /> <path d="M22 21v-2a4 4 0 0 0-3-3.87" /> <path d="M16 3.13a4 4 0 0 1 0 7.75" />',
  warning: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" /> <path d="M12 9v4" /> <path d="M12 17h.01" />',
  workspace: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />',
  key: '<path d="m15.5 7.5 3 3L22 7l-3-3" /><path d="m21 2-9.6 9.6" /><circle cx="7.5" cy="15.5" r="5.5" />',
  fingerprint: '<path d="M12 11a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4" /><path d="M14 14c0 .52-.02 1.11-.08 1.82-.12 1.36-.28 2.75-.44 4.05" /><path d="M18 13c0 2.68-.32 5.26-1.06 7.67" /><path d="M6 13c0-2.2.86-4.2 2.25-5.67A7.07 7.07 0 0 1 12 5c1.49 0 2.87.45 4.02 1.23" /><path d="M8 13a4 4 0 0 1 7.31-2.11c1.04 1.4 1.69 3.07 1.69 4.85 0 3.69-1.31 7.44-3.24 10.74" /><path d="M9 13c0 5.74-2.19 11.06-5.06 15.02" /><path d="M10 13c0 4.5-1.36 8.7-3.69 11.92" />',
};

  function icon(name) {
    var inner = ICONS[name] || ICONS.plus;
    return '<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">' + inner + '</svg>';
  }

  /* Loading placeholders (0.1.84). A table or list that is still being fetched must
   * SAY so, and must never print its "nothing here" message, because the user reads
   * that as fact (Steward: almost every table claimed there was nothing until it
   * loaded). The frame matches .empty-state exactly, so nothing shifts when the real
   * rows arrive. */
  function loadingBlock(title, hint, minHeight) {
    return '<div class="empty-state" style="min-height:' + (minHeight || 180) + 'px" role="status" aria-live="polite">' +
      "<div><h3>" + esc(title) + "</h3>" + (hint ? '<p class="muted">' + esc(hint) + "</p>" : "") + "</div></div>";
  }
  function loadingRow(colspan, title, hint, minHeight) {
    return '<tr><td colspan="' + colspan + '">' + loadingBlock(title, hint, minHeight) + "</td></tr>";
  }

  function statusBadge(type, label) {
    var map = { active: "success", running: "success", approved: "success", awaiting: "warning",
      pastdue: "warning", provisioning: "info", registered: "info", failed: "danger",
      expired: "danger", off: "neutral", cancelled: "neutral", locked: "danger",
      suspended: "warning", archived: "neutral", denied: "danger",
      healthy: "success", pending: "info", requested: "warning", token_sent: "info", past_due: "warning" };
    return '<span class="status ' + (map[type] || "neutral") + '">' + esc(label || type) + "</span>";
  }

  function brand() {
    return '<a class="brand" href="#/entry" aria-label="SteProTECH home"><span class="brand-mark"><span>S</span></span><span>StePro<em>TECH</em></span></a>';
  }

  function navigate(route) {
    state.menuOpen = false;
    if (location.hash === "#" + route) render();
    else location.hash = route;
  }

  /* ================= layouts (designer) ================= */
  function authLayout(content) {
    return '<main class="auth-page">' +
      '<aside class="auth-aside">' + brand() +
        '<div class="auth-copy"><div class="eyebrow">Managed automation infrastructure</div>' +
        "<h1>Your private n8n workspace, managed for you.</h1>" +
        "<p>Build and run business automations without managing servers, security certificates, or updates.</p></div>" +
        '<div class="trust-row"><span>' + icon("shield") + " Secure infrastructure</span><span>" + icon("support") + " Ghana-based support</span></div>" +
      "</aside>" +
      '<section class="auth-main"><div class="auth-card">' + content + "</div></section>" +
      "</main>";
  }

  var customerNav = [
    ["dashboard", "Dashboard", "/customer/dashboard"],
    ["workspace", "My workspaces", "/customer/workspaces"],
    ["backup", "Backups", "/customer/backups"],
    ["billing", "Plans & billing", "/customer/billing"],
    ["support", "Support", "/customer/support"],
    ["account", "Account", "/customer/account"],
    ["security", "Security", "/customer/security"]
  ];
  var adminNav = [
    ["overview", "Overview", "/admin/overview"],
    ["requests", "Access requests", "/admin/requests"],
    ["accounts", "Accounts", "/admin/accounts"],
    ["backup", "Backups", "/admin/backups"],
    ["updates", "Updates", "/admin/updates"],
    ["maintenance", "Billing maintenance", "/admin/maintenance"],
    ["settings", "Settings", "/admin/settings"],
    ["security", "Security", "/admin/security"]
  ];

  function profileChip() {
    var isAdmin = currentKind() === "admin";
    var acc = state.session && state.session.account ? state.session.account : null;
    var name = isAdmin ? "Administrator" : (acc ? firstNameOf(acc) : "Account");
    var sub = isAdmin ? "SteProTECH staff" : (acc ? acc.email : "");
    var initials = isAdmin ? "SO" : (acc ? initialsOf(acc.display_name || acc.username) : "?");
    // Interactive account chip (Steward 2026-09-04): toggles a drop-up menu
    // anchored above the chip. Log out reuses the existing signout-route flow
    // (admin clears + goes to admin sign-in, customer confirms + goes to entry,
    // impersonation exits first) so behavior stays identical to the topbar icon.
    return '<div class="profile-chip" role="button" tabindex="0" aria-haspopup="menu" aria-expanded="false" aria-controls="profile-chip-menu" data-action="profile-menu" title="Account menu">' +
      '<span class="avatar">' + esc(initials) + '</span><span class="chip-text"><strong>' + esc(name) + '</strong><small>' + esc(sub) + '</small></span>' +
      '<svg class="chip-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></div>' +
      '<div class="chip-menu" id="profile-chip-menu" role="menu" aria-label="Account menu">' +
        '<button class="chip-menu-item" role="menuitem" data-action="signout-route">' + icon("signout") + " Log out</button>" +
      "</div>";
  }
  function firstNameOf(acc) {
    // First name only (0.1.74, Steward): the chip is 216px wide, so a full name
    // pushed the email into a hard clip at the container edge. `first_name` is
    // the real field; older rows fall back to the first token of the display
    // name ("Olisaemeka Ekwenem" -> "Olisaemeka"), then to the username.
    var first = String((acc && acc.first_name) || "").trim();
    if (first) return first;
    var dn = String((acc && acc.display_name) || "").trim();
    if (dn) return dn.split(/\s+/)[0];
    return cap((acc && acc.username) || "");
  }
  function initialsOf(name) {
    var words = String(name || "?").split(/[\s._-]+/).filter(function (w) { return w.length; });
    var a = (words[0] || "?").charAt(0).toUpperCase();
    var b = words.length > 1 ? (words[1] || "").charAt(0).toUpperCase() : "";
    return a + b;
  }
  function currentKind() {
    var h = (location.hash || "").slice(1);   // strip leading '#'
    return h.indexOf("/admin/") === 0 ? "admin" : "customer";
  }

  function customerNavFor() {
    // The Backup item is admin-gated (2026-09-03): only shown when the admin
    // enabled self-service backup for THIS account. Default hidden.
    var enabled = !!(state.session && state.session.account && state.session.account.backup_enabled);
    return customerNav.filter(function (n) {
      return !(n[0] === "backup" && !enabled);
    });
  }

  function impersonationStrip() {
    var imp = state.impersonation || {};
    var label = esc(imp.label || ("Account #" + imp.id));
    return '<div class="imp-strip"><span class="imp-icon">' + icon("eye") + "</span>" +
      '<div class="imp-copy"><strong>Viewing as ' + label + "</strong>" +
      "<span>This is the customer view, for troubleshooting. Anything changed here applies to this customer's account.</span></div>" +
      '<button class="imp-exit" data-action="exit-impersonation">' + icon("signout") + " Exit and return to admin</button></div>";
  }

  function appLayout(kind, title, active, content) {
    var isAdmin = kind === "admin";
    var nav = isAdmin ? adminNav : customerNavFor();
    return '<div class="app-shell">' +
      '<aside class="sidebar' + (state.menuOpen ? " open" : "") + '">' + brand() +
        '<div class="side-label">' + (isAdmin ? "Admin portal" : "Customer portal") + "</div>" +
        '<nav class="nav-list" aria-label="Primary navigation">' +
          nav.map(function (n) {
            return '<a class="nav-item' + (active === n[0] ? " active" : "") + '" href="#' + n[2] + '"><span class="nav-icon">' + icon(n[0]) + "</span>" + n[1] + "</a>";
          }).join("") +
        "</nav>" +
        '<div class="sidebar-footer">' + profileChip() + "</div>" +
      "</aside>" +
      '<div class="side-backdrop" data-action="side-close"></div>' +
      '<div class="main-area">' +
        (state.impersonation && !isAdmin ? impersonationStrip() : "") +
        '<header class="topbar"><div class="actions">' +
          '<button class="top-icon mobile-menu" data-action="menu" aria-label="Open menu">' + icon("menu") + "</button>" +
          "<h2>" + esc(title) + "</h2></div>" +
          '<div class="top-actions"><button class="top-icon" data-action="signout-route" aria-label="Sign out" title="Sign out">' + icon("signout") + "</button></div>" +
        "</header>" + content +
      "</div></div>";
  }

  function pageHead(title, description, actions, headCls) {
    return '<div class="page-head' + (headCls ? " " + headCls : "") + '"><div><h1>' + esc(title) + '</h1><p class="muted">' + esc(description || "") + "</p></div>" + (actions || "") + "</div>";
  }

  /* ================= customer pages ================= */

  function entryPage() {
    return authLayout(
      '<div class="eyebrow">Welcome to the portal</div><h1>Start with your email</h1>' +
      '<p class="muted">New and existing customers begin here. We will guide you to the right next step.</p>' +
      '<form data-form="entry"><div class="field"><label for="entry-email">Email address</label>' +
      '<input id="entry-email" name="email" type="email" autocomplete="email" placeholder="you@company.com" required>' +
      '</div><button class="button primary-wide" type="submit">Continue ' + icon("arrow") + "</button></form>" +
      '<p class="small muted" style="text-align:center;margin-top:20px">Staff? <a href="#/admin/signin">Admin sign in</a></p>'
    );
  }

  // Fallback only since 0.1.98: nothing sends a visitor here any more, because a
  // first-time email opens the create-account form immediately. It is kept so an
  // unexpected status still renders a real page instead of a blank screen.
  function requestPage() {
    var email = state.gateEmail || "your address";
    return authLayout(
      '<div class="center-state"><div class="state-orb">' + icon("mail") + "</div>" +
      '<div class="eyebrow">Request received</div><h1>Your request is awaiting approval</h1>' +
      '<p class="muted">A SteProTECH team member will review it shortly. Once approved we will email <strong>' + esc(email) + "</strong> so you can create your account.</p>" +
      '<div class="banner" style="margin:26px 0;text-align:left">' + icon("clock") +
        "<div><strong>Watch your inbox</strong><br><span class=\"small\">Approval is handled by a person. You do not need to submit another request.</span></div></div>" +
      '<div class="actions" style="justify-content:center"><button class="button secondary" data-route="/entry">Use a different email</button></div></div>'
    );
  }

  function deniedPage() {
    var email = state.gateEmail || "";
    return authLayout(
      '<div class="center-state"><div class="state-orb danger">' + icon("error") + "</div>" +
      '<div class="eyebrow">Request declined</div><h1>This request was disapproved</h1>' +
      '<p class="muted">A SteProTECH administrator declined the access request for <strong>' + esc(email) + "</strong>. If you believe this is a mistake, contact support from a different address or ask an administrator to approve you.</p>" +
      '<div class="actions" style="justify-content:center;margin-top:24px"><button class="button secondary" data-route="/entry">Use a different email</button></div></div>'
    );
  }

  function codePage() {
    var email = state.gateEmail || "";
    // 0.1.98 (Steward, 2026-10-11): this page is now the FIRST step of onboarding,
    // not the result of an approval, so nothing here talks about being approved.
    return authLayout(
      '<div class="eyebrow">Get started</div><h1>Create your account</h1>' +
      '<p class="muted">Add your name and a password, then choose a plan to set up your first workspace. Your account will use <strong>' + esc(email) + '</strong>.</p>' +
      '<form data-form="code"><div class="form-grid">' +
        '<div class="field full"><label>Email address</label><input value="' + esc(email) + '" readonly><span class="hint">This is the email address you entered.</span></div>' +
        '<div class="field"><label>First name</label><input name="first_name" required maxlength="50"></div>' +
        '<div class="field"><label>Last name</label><input name="last_name" required maxlength="50"></div>' +
        '<div class="field"><label>Password</label><div class="input-row">' +
        '<input id="reg-password" name="password" type="password" required minlength="8" maxlength="128">' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="reg-password" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="field"><label>Confirm password</label><div class="input-row">' +
        '<input id="reg-confirm" type="password" required>' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="reg-confirm" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="checks full" id="pw-checks">' +
        '<span class="check" data-check="len" data-label="At least 8 characters">At least 8 characters</span>' +
        '<span class="check" data-check="upper" data-label="One uppercase letter">One uppercase letter</span>' +
        '<span class="check" data-check="lower" data-label="One lowercase letter">One lowercase letter</span>' +
        '<span class="check" data-check="digit" data-label="One digit">One digit</span>' +
        '<span class="check" data-check="match" data-label="Passwords match">Passwords match</span></div>' +
      '</div>' +
      '<button class="button primary-wide" type="submit">Create account &amp; continue</button></form>' +
      '<p class="small muted" style="text-align:center;margin-top:18px">Wrong address? <a href="#/entry">Use a different email</a> or contact support@steprotech.com</p>'
    );
  }


  function registerPage() {
    var email = state.gateEmail || "";
    return authLayout(
      '<div class="eyebrow">Complete registration</div><h1>Create your account</h1>' +
      '<p class="muted">Your portal account and first workspace will use these details.</p>' +
      '<form data-form="register"><div class="form-grid">' +
        '<div class="field full"><label>Email address</label><input value="' + esc(email) + '" readonly><span class="hint">This email was verified with your access code.</span></div>' +
        '<div class="field"><label>First name</label><input name="first_name" required maxlength="50"></div>' +
        '<div class="field"><label>Last name</label><input name="last_name" required maxlength="50"></div>' +
        '<div class="field full"><label>Username <span class="muted" id="username-state"></span></label>' +
        '<input name="username" pattern="[a-z0-9-]+" maxlength="62" placeholder="leave blank to derive from your email">' +
        '<span class="hint">Becomes your workspace address: <strong id="username-preview"></strong><br>Lowercase letters, digits, and hyphens only. No dots.</span></div>' +
        '<div class="field"><label>Password</label><div class="input-row">' +
        '<input id="reg-password" name="password" type="password" required minlength="8" maxlength="128">' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="reg-password" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="field"><label>Confirm password</label><div class="input-row">' +
        '<input id="reg-confirm" type="password" required>' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="reg-confirm" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="checks full" id="pw-checks">' +
        '<span class="check" data-check="len" data-label="At least 8 characters">At least 8 characters</span>' +
        '<span class="check" data-check="upper" data-label="One uppercase letter">One uppercase letter</span>' +
        '<span class="check" data-check="lower" data-label="One lowercase letter">One lowercase letter</span>' +
        '<span class="check" data-check="digit" data-label="One digit">One digit</span>' +
        '<span class="check" data-check="match" data-label="Passwords match">Passwords match</span></div>' +
      '</div>' +
      '<div class="banner" style="margin-bottom:18px">' + icon("arrow") + "<div><strong>Next, choose a plan</strong><br><span class=\"small\">Your workspace will be created after secure payment.</span></div></div>" +
      '<button class="button primary-wide" type="submit">Create account</button>' +
      '<p class="small muted" style="text-align:center;margin-top:14px">Already have an account? <a href="#/signin">Sign in</a></p></form>'
    );
  }

  function forgotPage() {
    return authLayout(
      '<div class="eyebrow">Password help</div><h1>Reset your password</h1>' +
      '<p class="muted">Enter your account email and we will send a link to set a new portal password.</p>' +
      '<form data-form="forgot"><div class="field"><label>Email address</label>' +
      '<input name="email" type="email" required placeholder="you@example.com"></div>' +
      '<button class="button primary-wide" type="submit">Send reset link</button></form>' +
      '<div class="banner success" id="forgot-done" style="display:none;margin-top:24px">' + icon("check") + "<div><strong>Check your email</strong><br><span class=\"small\">If an account exists for that address, a reset link is on its way. The link works for 1 hour.</span></div></div>" +
      '<p class="small muted" style="text-align:center;margin-top:18px"><a href="#/entry">Back to sign in</a></p>'
    );
  }

  function resetPage() {
    var params = new URLSearchParams(location.hash.split("?")[1] || "");
    var tok = params.get("token") || "";
    if (!tok) {
      return authLayout(
        '<div class="eyebrow">Password help</div><h1>Set a new password</h1>' +
        '<p class="muted">This reset link is missing its token. Request a fresh one below.</p>' +
        '<div class="actions"><a class="button secondary" href="#/forgot">Request a new link</a></div>'
      );
    }
    return authLayout(
      '<div class="eyebrow">Password help</div><h1>Set a new password</h1>' +
      '<p class="muted">Choose a strong new password for your portal account. It must be at least 8 characters.</p>' +
      '<form data-form="reset"><div class="field"><label>New password</label><div class="input-row">' +
      '<input id="reset-password" name="password" type="password" required minlength="8">' +
      '<button type="button" class="input-action" data-action="toggle-password" data-target="reset-password">' + icon("eye") + '</button></div></div>' +
      '<button class="button primary-wide" type="submit">Set new password</button></form>' +
      '<p class="small muted" style="text-align:center;margin-top:18px"><a href="#/entry">Back to sign in</a></p>'
    );
  }

  function mfaPage() {
    var m = state.mfa;
    if (!m) { navigate("/entry"); return ""; }
    var hasEmail = m.methods.some(function (x) { return x.method === "email"; });
    var hasPasskey = m.methods.some(function (x) { return x.method === "passkey"; });
    var labels = m.methods.map(function (x) { return x.label || x.method; });
    var ex = labels.join(" or ");
    return authLayout(
      '<div class="eyebrow">Two-factor authentication</div><h1>Enter your code</h1>' +
      '<p class="muted">Sign in as ' + esc(m.email) + '. Use your <b>' + esc(ex) + '</b> to continue.</p>' +
      '<form data-form="mfa"><div class="field"><label>Verification code</label>' +
      '<input id="mfa-code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" required placeholder="123456" maxlength="10"></div>' +
      '<button class="button primary-wide" type="submit">Verify &amp; sign in</button></form>' +
      (hasPasskey ? '<div style="text-align:center;margin-top:12px"><button class="button secondary" data-action="mfa-passkey">' + icon("fingerprint") + " Use a passkey</button></div>" : "") +
      (hasEmail ? '<p class="small muted" style="text-align:center;margin-top:14px"><a href="#" data-action="mfa-resend">Resend email code</a></p>' : "") +
      '<p class="small muted" style="text-align:center;margin-top:12px"><a href="#/entry">Use a different email</a></p>'
    );
  }

  function adminMfaPage() {
    var m = state.adminMfa;
    if (!m) { navigate("/admin/signin"); return ""; }
    var byMethod = {};
    m.methods.forEach(function (x) { byMethod[x.method] = x; });
    var hasEmail = !!byMethod.email;
    var hasSms = !!byMethod.sms;
    var hasPasskey = !!byMethod.passkey;
    var labels = m.methods.map(function (x) { return x.label || x.method; });
    var ex = labels.join(" or ");
    // Nothing is sent automatically: an SMS costs real credits, so the admin presses the
    // button and sees exactly where the code went. (0.1.90, Steward 2026-10-07.)
    var senders = "";
    if (hasSms) {
      senders += '<button class="button primary-wide" data-action="admin-mfa-send" data-channel="sms">' +
        icon("phone") + " Text me a code" + (byMethod.sms.to ? ' <span class="small">' + esc(byMethod.sms.to) + "</span>" : "") + "</button>";
    }
    if (hasEmail) {
      senders += '<button class="button ' + (hasSms ? "secondary " : "") + 'primary-wide" style="margin-top:8px" data-action="admin-mfa-send" data-channel="email">' +
        icon("mail") + " Email me a code" + (byMethod.email.to ? ' <span class="small">' + esc(byMethod.email.to) + "</span>" : "") + "</button>";
    }
    var sentNote = state.adminMfaSent
      ? '<p class="small muted" style="text-align:center;margin-top:12px">Code sent to <b>' +
        esc(state.adminMfaSent.to || "") + "</b> (" + esc(state.adminMfaSent.channel === "sms" ? "text message" : "email") + ").</p>"
      : "";
    return authLayout(
      '<div class="eyebrow">Staff verification</div><h1>Enter your code</h1>' +
      '<p class="muted">Use your <b>' + esc(ex) + '</b> to finish signing in.</p>' +
      (senders ? '<div style="margin-bottom:14px">' + senders + "</div>" : "") +
      sentNote +
      '<form data-form="admin-mfa"><div class="field"><label>Verification code</label>' +
      '<input id="admin-mfa-code" name="code" type="text" inputmode="text" autocapitalize="characters" ' +
      'autocorrect="off" spellcheck="false" autocomplete="one-time-code" required placeholder="Code" maxlength="10"></div>' +
      '<button class="button primary-wide" type="submit">Verify &amp; sign in</button></form>' +
      (hasPasskey ? '<div style="text-align:center;margin-top:12px"><button class="button secondary" data-action="admin-mfa-passkey">' + icon("fingerprint") + " Use a passkey</button></div>" : "") +
      '<p class="small muted" style="text-align:center;margin-top:20px">Customer? <a href="#/entry">Return to customer portal</a></p>'
    );
  }

  function signinPage() {
    var gateEmail = state.gateEmail || "";
    // Pre-fill the email from the entry screen and lock it: the customer walks
    // the email-first login, so editing the address here would break the login
    // branch. "Change email" returns them to /entry to start over (2026-09-02).
    return authLayout(
      '<div class="eyebrow">Welcome back</div><h1>Sign in to your portal</h1>' +
      '<p class="muted">Use your portal password to manage billing and open your workspace.</p>' +
      '<form data-form="signin"><div class="field"><label>Email address</label>' +
      '<input name="email" type="email" value="' + esc(gateEmail) + '" readonly aria-readonly="true">' +
      '<small class="field-note">' + icon("account") + ' Signed in as ' + esc(gateEmail) +
      ' &middot; <a href="#/entry">Change email</a></small></div>' +
      '<div class="field"><label>Password</label><div class="input-row">' +
      '<input id="signin-password" name="password" type="password" required>' +
      '<button type="button" class="input-action" data-action="toggle-password" data-target="signin-password">' + icon("eye") + '</button></div></div>' +
      '<button class="button primary-wide" type="submit">Sign in</button></form>' +
      '<p class="small muted" style="text-align:center;margin-top:14px"><a href="#/forgot">Forgot your portal password?</a></p>' +
      '<p class="small muted" style="text-align:center;margin-top:18px">No account yet? <a href="#/entry">Request access</a></p>'
    );
  }

  /* ---- customer dashboard ---- */

  function subscriptionState() {
    /* derive the customer-facing "state" from account + first instance */
    var acc = state.session ? state.session.account : null;
    if (!acc) return "none";
    var inst = (state.session.instances || [])[0];
    var sub = acc.subscription_status || "none";
    if (sub === "locked") return inst && inst.locked ? "expired" : "expired";
    if (sub === "canceled") return "cancelled";
    if (sub === "past_due") return "pastdue";
    if (sub !== "active") return "none";
    // active subscription: instance decides
    if (!inst) return "none"; // paid but never provisioned (should not happen in mock flow)
    if (inst.locked) return "expired";
    if (inst.status === "provisioning") return "provisioning";
    if (inst.status === "failed") return "failed";
    return "active"; // healthy
  }

  function dashboardStatus() {
    var acc = state.session ? state.session.account : null;
    var inst = state.session && state.session.instances ? state.session.instances[0] : null;
    var s = subscriptionState();
    var openPay = state.paymentsOpen !== false;
    if (!acc) {
      return '<div class="hero-status info"><span class="big-icon">' + icon("shield") + "</span><div><h2>Welcome to the portal</h2><p>Create your account or sign in to manage your workspace.</p></div><a class=\"button\" href=\"#/entry\">Get started</a></div>";
    }
    if (s === "active") {
      var url = inst && inst.domain ? "https://" + inst.domain + "/" : "#";
      return '<div class="hero-status"><span class="big-icon">' + icon("check") + '</span><div><h2>Nothing needs your attention</h2><p>Your subscription and workspace are running normally.</p></div>' +
        '<a class="button" href="' + esc(url) + '" target="_blank" rel="noopener">Open workspace ' + icon("external") + "</a></div>";
    }
    if (s === "provisioning") {
      return '<div class="hero-status info"><span class="big-icon">' + icon("clock") + '</span><div><h2>Your workspace is being prepared</h2><p>This usually takes a few minutes. We will email you when it is ready.</p></div></div>';
    }
    if (s === "failed") {
      return '<div class="hero-status danger"><span class="big-icon">' + icon("warning") + '</span><div><h2>Setup needs attention</h2><p>Something went wrong. SteProTECH has been notified and will contact you.</p></div>' +
        '<a class="button danger" href="mailto:support@steprotech.com">Contact support</a></div>';
    }
    if (s === "pastdue") {
      return '<div class="hero-status warning"><span class="big-icon">' + icon("warning") + '</span><div><h2>Your renewal payment did not go through</h2><p>Renew before ' + esc(fmtDate(acc.paid_until)) + " to keep your workspace running.</p></div>" +
        (openPay ? '<button class="button warning" data-action="pay-now">Renew now</button>'
                 : '<button class="button warning" disabled title="Renewals are paused by SteProTECH">Payments on hold</button>') + '</div>';
    }
    if (s === "cancelled") {
      return '<div class="hero-status warning"><span class="big-icon">' + icon("warning") + '</span><div><h2>Your subscription is cancelled</h2><p>Your workspace remains available until the paid period ends on ' + esc(fmtDate(acc.paid_until)) + ".</p></div>" +
        '<a class="button secondary" href="#/customer/billing">View billing</a></div>';
    }
    if (s === "expired") {
      if (!openPay) {
        return '<div class="hero-status info"><span class="big-icon">' + icon("lock") + '</span><div><h2>Your workspace is on hold</h2><p>Renewals are paused while SteProTECH sets up new users. Your work is safe; access will be restored by our team.</p></div>' +
          '<a class="button secondary" href="mailto:support@steprotech.com">Contact support</a></div>';
      }
      return '<div class="hero-status danger"><span class="big-icon">' + icon("lock") + "</span><div><h2>Your workspace is switched off</h2><p>Renew to switch it back on with all your work intact.</p></div>" +
        '<button class="button danger" data-action="pay-now">Renew now</button></div>';
    }
    // subscription not active yet -> subscribe (unless payments are on hold)
        var planA = state.plans.find(function (x) { return x.active && x.amount_minor !== 50000; });
    var planB = state.plans.find(function (x) { return x.active && x.amount_minor === 50000; });
    var primary = planA || planB || state.plans[0];
    if (!openPay) {
      return '<div class="hero-status info"><span class="big-icon">' + icon("billing") + '</span><div><h2>Welcome to the portal</h2><p>Payments are temporarily on hold while SteProTECH sets up new users. Your workspace can be activated without payment - contact support.</p></div>' + '<a class="button secondary" href="mailto:support@steprotech.com">Contact support</a></div>';
    }
    var btns = '<button class="button" data-action="pay-now" data-plan="' + (primary && primary.amount_minor === 50000 ? "b" : "a") + '">' + (primary ? "Subscribe " + fmtMinor(primary.amount_minor, primary.currency) + "/yr" : "Subscribe") + "</button>";
    if (planA && planB) { btns += ' <button class="button secondary" data-action="pay-now" data-plan="b">Subscribe Plus ' + fmtMinor(planB.amount_minor, planB.currency) + '/yr</button>'; }
    return '<div class="hero-status info"><span class="big-icon">' + icon("billing") + '</span><div><h2>Choose a plan to get started</h2><p>Subscribe to the annual plan and your private workspace is created after payment.</p></div><div class="actions" style="margin-top:14px">' + btns + "</div></div>";
  }

  function subBadgeFor(stateKey) {
    if (stateKey === "pastdue") return statusBadge("pastdue", "Past due");
    if (stateKey === "expired") return statusBadge("expired", "Expired");
    if (stateKey === "cancelled") return statusBadge("cancelled", "Cancelled");
    if (stateKey === "provisioning") return statusBadge("provisioning", "Preparing");
    if (stateKey === "failed") return statusBadge("failed", "Setup failed");
    if (stateKey === "none") return statusBadge("pending", "No subscription");
    return statusBadge("active", "Active");
  }

  function customerDashboard() {
    var acc = state.session ? state.session.account : null;
    var s = subscriptionState();
    var inst = state.session && state.session.instances ? state.session.instances[0] : null;
    var used = state.session ? (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }).length : 0;
    var quota = allowanceFor(acc, state.session ? state.session.instances : []);
    var name = acc ? cap(acc.display_name || acc.username) : "";
    var running = s === "active";
    var provisionState = inst && inst.status === "provisioning";
    var wsBadge = running ? statusBadge("running", "Running")
      : provisionState ? statusBadge("provisioning", "Being set up")
      : inst && inst.status === "failed" ? statusBadge("failed", "Setup failed")
      : s === "expired" || (inst && inst.locked) ? statusBadge("off", "Switched off")
      : statusBadge("off", "Not created yet");

    var wsAction = "";
    if (inst && inst.domain && running) {
      wsAction = '<a class="button small-btn" href="https://' + esc(inst.domain) + '/" target="_blank" rel="noopener">Open workspace ' + icon("external") + "</a>";
    } else if (!inst && acc && acc.subscription_status === "active") {
      wsAction = '<button class="button small-btn" data-action="provision-now">Provision workspace</button>';
    }

    var periodEnd = acc && acc.paid_until ? fmtDate(acc.paid_until) : "not set";
    return appLayout("customer", "Dashboard", "dashboard",
      '<main class="page">' + pageHead((name ? "Hello, " + name : "Dashboard"), "Here is the current state of your account and workspace.") +
      dashboardStatus() +
      '<div class="grid cols-3" style="margin-top:18px">' +
        '<section class="card"><div class="card-head"><h3>Subscription</h3>' + subBadgeFor(s) + "</div>" +
          subscriptionsList(acc, state.session ? (state.session.instances || []) : []) + "</section>" +
        '<section class="card"><div class="card-head"><h3>Workspace quota</h3><span class="muted small">' + used + " of " + quota + " used</span></div>" +
          '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div><strong>' + Math.round(quota ? used / quota * 100 : 0) + "%</strong></div>" +
          '<p class="small muted" style="margin-top:20px">' + (used < quota ? "You can create another workspace." : "Contact support if your business needs a higher limit.") + "</p>" +
          (used < quota && acc && acc.subscription_status === "active"
            ? '<button class="button secondary small-btn" data-action="add-workspace">Add workspace</button>' : "") +
          (function () {
            var pi = state.session && state.session.plan;
            if (!pi) return "";
            var req = state.session.upgradeRequest;
            var out = '<p class="small muted" style="margin-top:14px">Package: <strong>' + esc(pi.label) + "</strong></p>";
            if (!pi.can_request_upgrade) return out;
            if (req) {
              return out + '<p class="small" style="margin-top:8px">' + icon("clock") +
                " Upgrade to " + esc(pi.target_label) + " requested" +
                (req.created_at ? " on " + esc(fmtDate(req.created_at)) : "") +
                ". We will contact you.</p>";
            }
            return out + '<button class="button secondary small-btn" data-action="request-upgrade">' +
              icon("spark") + " Request upgrade to " + esc(pi.target_label) + "</button>";
          })() + "</section>" +
        '<section class="card"><div class="card-head"><h3>Next renewal</h3>' + icon("billing") + "</div>" +
          '<p style="font-family:Manrope;font-size:1.4rem;font-weight:800;margin:12px 0 5px">' + esc(periodEnd) + "</p>" +
          '<p class="small muted">Your payment provider charges your card automatically and emails the result.</p>' +
          '<a class="button ghost small-btn" href="#/customer/billing">View billing details</a></section>' +
      "</div>" +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h2>My workspaces</h2><a class="button secondary small-btn" href="#/customer/workspaces">View all</a></div>' +
        '<div class="workspace-list">' + workspaceItemHtml(inst, wsBadge, wsAction) + "</div></section>" +
      "</main>"
    );
  }

  /* ---------- package identity (0.1.73) ----------
     A package is identified by the API's own `key` ('a' | 'b'), never by "the
     first active plan": that shortcut printed the wrong package on every screen
     for a Plus customer. Older API responses without `key` fall back to price. */
  function planKeyOf(x) {
    var k = x && x.plan;
    return k === "b" ? "b" : "a";
  }
  function planByKey(key) {
    var want = key === "b" ? "b" : "a";
    var list = state.plans || [];
    var byKey = list.find(function (p) { return p.key === want; });
    if (byKey) return byKey;
    var byPrice = list.find(function (p) {
      return want === "b" ? p.amount_minor === 50000 : p.amount_minor !== 50000;
    });
    return byPrice || null;
  }
  function planName(x) {
    var p = planByKey(planKeyOf(x));
    return (p && p.name) || (planKeyOf(x) === "b" ? "n8n Workspace Plus" : "n8n Workspace");
  }
  function planFee(x) {
    var p = planByKey(planKeyOf(x));
    return p ? fmtMinor(p.amount_minor, p.currency) + " / year" : "-";
  }
  // Which packages does this account HOLD? Its own package plus any package a
  // live workspace sits on (each workspace carries its own package since 0.1.70).
  function heldPackages(acc, instances) {
    var held = { a: false, b: false };
    if (acc) held[planKeyOf(acc)] = true;
    (instances || []).forEach(function (i) {
      if (!i || i.status === "deleted" || i.status === "failed") return;
      held[planKeyOf(i)] = true;
    });
    return held;
  }
  /* ---------- packages: change the plan, switch the workspace (0.1.99) ----------
     The package is two different compose templates: standard is 0.30 CPU / 768 MB / heap 384,
     Plus is 0.50 CPU / 1 GB / heap 768. Changing the PLAN is instant and only moves what the
     account is subscribed to; switching the WORKSPACE recreates its container, so it is a
     separate, confirmed action. One control does both directions: it reads "Upgrade to Plus" or
     "Downgrade to standard" from the workspace's own plan, never two code paths. */
  function planLabelOf(v) { return v === "b" ? "Workspace Plus (GHS 500)" : "Workspace (GHS 300)"; }
  function planSpecsOf(v) {
    return v === "b" ? "0.50 CPU · 1 GB memory · node heap 768" : "0.30 CPU · 768 MB memory · node heap 384";
  }

  function changePlanModal(accountId) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(accountId); });
    if (!a) { showToast("Not found", "Could not resolve this account."); return; }
    var cur = a.plan === "b" ? "b" : "a";
    function opt(val) {
      var on = val === cur;
      return '<label style="display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;margin:0">' +
        '<input type="radio" name="chg-plan" value="' + val + '"' + (on ? " checked" : "") + ' style="margin-top:3px">' +
        '<span><strong>' + planLabelOf(val) + "</strong>" +
        '<span class="small muted" style="display:block">' + planSpecsOf(val) + "</span>" +
        (on ? '<span class="small muted" style="display:block">Current package</span>' : "") + "</span></label>";
    }
    showModal('<div class="card-head"><h2>Change package</h2><button class="button ghost small-btn" data-action="close-modal">' + icon("close") + "</button></div>" +
      '<p class="muted">Move ' + esc(a.display_name || a.username) + " to the other package.</p>" +
      '<div style="display:grid;gap:10px;margin:14px 0">' + opt("a") + opt("b") + "</div>" +
      '<div class="consequence" style="background:var(--amber-100)"><strong>What this does and does not do</strong><ul><li>Changes what the account is subscribed to, and which allowance applies to its next workspace</li><li><strong>Does not</strong> change a running workspace: it keeps the CPU and memory it was built with</li><li>Switch the workspace separately if its resources should follow</li><li>Takes no payment and issues no refund</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-plan="' + a.id + '">' + icon("check") + " Change package</button></div>");
  }

  function switchPlanModal(instanceId, targetPlan) {
    var inst = findInstanceById(instanceId);
    if (!inst) { showToast("Not found", "Could not resolve this workspace."); return; }
    api("/admin/instances/" + Number(instanceId) + "/plan-preview?plan=" + encodeURIComponent(targetPlan))
      .then(function (pv) {
        var up = pv.action !== "downgrade";
        var rows = (pv.changes || []).map(function (c) {
          return "<li>" + esc(c.label) + ": <strong>" + esc(c.from) + "</strong> → <strong>" + esc(c.to) + "</strong></li>";
        }).join("");
        showModal('<div class="card-head"><h2>' + (up ? "Upgrade to Plus" : "Downgrade to standard") + "</h2>" +
          '<button class="button ghost small-btn" data-action="close-modal">' + icon("close") + "</button></div>" +
          '<p class="muted">Workspace <strong>' + esc(titleCase(pv.stack_name)) + "</strong> (" + esc(pv.domain || "") + ") moves from " +
          esc(pv.from_label) + " to <strong>" + esc(pv.to_label) + "</strong>.</p>" +
          '<div class="consequence" style="background:var(--amber-100)"><strong>What will change</strong><ul>' + rows + "</ul></div>" +
          '<div class="consequence" style="background:var(--red-100)"><strong>This restarts the workspace</strong><ul><li>The container is recreated with the new limits, which takes a few seconds</li><li>Workflows, executions and credentials are preserved (same data volume)</li><li>Anything running at that moment is interrupted</li><li>A full backup is taken first</li></ul></div>' +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-plan-switch="' + pv.instance_id + '" data-plan="' + esc(pv.to_plan) + '">' +
          icon("check") + " " + (up ? "Upgrade workspace" : "Downgrade workspace") + "</button></div>");
      })
      .catch(function (err) { showToast("Cannot switch this workspace", err.message); });
  }

  function requestUpgrade() {
    var acc = state.session && state.session.account;
    if (!acc) return;
    api("/accounts/" + acc.id + "/upgrade-request", { method: "POST", body: {} })
      .then(function (d) {
        showToast(d.status === "already_requested" ? "Already requested" : "Upgrade requested", d.message);
        refreshSession().then(function () { render(); });
      })
      .catch(function (err) { showToast("Request failed", err.message); });
  }

  function quotaOf(acc, key) {
    var value = acc ? (key === "b" ? acc.quota_b : acc.quota_a) : null;
    if (value === null || value === undefined) value = acc ? acc.quota : null;
    return Number(value || 0) || 1;
  }
  // The allowance the customer really has = one allowance per package held (the
  // provisioning gate is per package). The legacy single `quota` field
  // understated this for anyone holding both packages.
  function allowanceFor(acc, instances) {
    var held = heldPackages(acc, instances);
    var total = 0;
    if (held.a) total += quotaOf(acc, "a");
    if (held.b) total += quotaOf(acc, "b");
    if (total > 0) return total;
    return (acc && acc.quota) || 1;
  }
  // Per-package usage lines ("n8n Workspace: 1 of 1 used") for the workspace page.
  function allowanceBreakdown(acc, instances) {
    var held = heldPackages(acc, instances);
    var counts = { a: 0, b: 0 };
    (instances || []).forEach(function (i) {
      if (!i || i.status === "deleted" || i.status === "failed") return;
      counts[planKeyOf(i)] += 1;
    });
    var rows = [];
    ["a", "b"].forEach(function (k) {
      if (!held[k]) return;
      rows.push(planName({ plan: k }) + ": " + counts[k] + " of " + quotaOf(acc, k) + " used");
    });
    return rows;
  }

  function workspaceItemHtml(inst, badgeHtml, actionHtml) {
    if (!inst) {
      return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
        "<div><h3>No workspace yet</h3><p>Subscribe and provision to create your first workspace.</p></div>" +
        statusBadge("off", "Not created") + "</article>";
    }
    return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
      "<div><h3>" + esc(titleCase(inst.stack_name)) + " workspace" + wsPlanTag(inst) + "</h3><p>" + esc(inst.domain || "") + "</p></div>" +
      (badgeHtml || "") + (actionHtml || "") + (inst ? selfUpdateHtml(inst) : "") +
      (inst ? restartHtml(inst) : "") + "</article>";
  }

  /* ---- self-update (0.1.85) ----------------------------------------------------
   * Workspace Plus customers may ask for an update, but it is ALWAYS queued: one
   * workspace at a time per server, so the row shows where they are in the line
   * instead of pretending it starts on click. Nothing is claimed before
   * /me/updates has answered, and a GHS 300 workspace shows nothing at all.
   */
  function myUpdateFor(inst) {
    if (!state.myUpdatesLoaded || !inst) return null;
    var rows = state.myUpdates || [];
    for (var i = 0; i < rows.length; i++) if (Number(rows[i].instance_id) === Number(inst.id)) return rows[i];
    return null;
  }
  function fmtClock(ms) {
    try { return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return ""; }
  }
  function etaText(minutes) {
    if (minutes === null || minutes === undefined) return "";
    if (minutes <= 1) return "any moment now";
    return "about " + minutes + " minutes (around " + fmtClock(Date.now() + minutes * 60000) + ")";
  }
  function selfUpdateHtml(inst) {
    // Admin-gated exactly like backups (0.1.88): while the account is not permitted, the
    // customer sees NO update UI at all, so nothing is claimed and nothing is promised.
    if (!state.myUpdatesAllowed) return "";
    var info = myUpdateFor(inst);
    if (!info) return "";
    if (info.queued) {
      var q = info.queued;
      if (q.status === "running") {
        return '<span class="status awaiting">Updating now</span>';
      }
      return '<span class="status awaiting" title="' + esc(etaText(q.eta_minutes)) + '">Queued: ' +
        (q.position ? ("position " + q.position + " on your server") : "waiting") + "</span>";
    }
    if (info.can_update) {
      return '<button class="button small-btn" data-action="ws-self-update" data-id="' + inst.id + '">' +
        icon("download") + " Update to " + esc(info.latest_version || "the latest version") + "</button>";
    }
    if (info.updates_unavailable) {
      // the release check is down: nothing can be compared, so this must not read as
      // "on the latest version" or offer a button that the server would refuse
      return '<span class="status muted" title="The version check is unavailable right now.">Version check unavailable</span>';
    }
    if (info.blocked_reason) {
      return '<span class="small muted">' + esc(info.blocked_reason) + "</span>";
    }
    if (info.last && info.last.status === "done" && info.last.to_version) {
      return '<span class="small muted">Updated to ' + esc(info.last.to_version) + "</span>";
    }
    if (!info.needs_update && info.current_version) {
      return '<span class="small muted">On ' + esc(info.current_version) + " (latest)</span>";
    }
    if (info.needs_update) {
      return '<span class="small muted">Update available, ask support</span>';
    }
    return "";
  }
  function restartHtml(inst) {
    /* Restart is NOT an update (Steward, 2026-10-08): the same container starts again on
     * the same image with the same data, so it is offered to everyone on their own
     * workspace and needs no permission and no release check. Nothing is offered while an
     * update is queued or running for it, because the server refuses that on purpose, nor
     * for a switched-off workspace, which has no container to start.
     */
    if (!inst || inst.locked) return "";
    var info = myUpdateFor(inst);
    if (info && info.queued) return "";
    return '<button class="button secondary small-btn" data-action="ws-restart" data-id="' +
      inst.id + '">' + icon("refresh") + " Restart</button>";
  }

  async function ensureMyUpdates(force) {
    if (state.myUpdatesLoaded && !force) return;
    try {
      var d = await api("/me/updates");
      state.myUpdates = (d && d.items) || [];
      state.myUpdatesLatest = (d && d.latest_version) || "";
      state.myUpdatesAllowed = !!(d && d.update_allowed);
    } catch (e) { state.myUpdates = state.myUpdates || []; }
    // marked either way, so a broken read cannot leave the rows blank for ever
    state.myUpdatesLoaded = true;
    if (!modalRoot.children.length) render();
  }
  async function queueUpdates(ids) {
    var body = ids ? { instance_ids: ids } : { all_behind: true };
    try {
      var r = await api("/admin/updates/queue", { method: "POST", body: body });
      var n = (r && r.queued ? r.queued.length : 0);
      var msg = n
        ? n + " workspace(s) queued. One updates at a time per server, then the report arrives when the run finishes."
        : "Nothing new to queue.";
      if (r && r.skipped && r.skipped.length) msg += " Skipped " + r.skipped.length + ".";
      showToast(n ? "Queue updated" : "Nothing to queue", msg);
      state.updSel = {};
      state.adminUpdatesLoaded = false;
      await ensureAdminUpdates(true);
    } catch (e) {
      showToast("Could not queue", (e && e.message) || "Try again.");
    }
  }
  async function ensureAdminUpdates(force) {
    if (state.adminUpdatesLoaded && !force) return;
    try {
      state.adminUpdates = await api("/admin/updates");
    } catch (e) {
      // never leave a half-page: an unanswered load says so, it does not look empty
      state.adminUpdates = state.adminUpdates ||
        { load_error: (e && e.message) || "The update queue could not be loaded." };
    }
    state.adminUpdatesLoaded = true;
    if (!modalRoot.children.length) render();
  }

  function customerWorkspaces() {
    var s = subscriptionState();
    var acc = state.session ? state.session.account : null;
    var insts = state.session ? (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }) : [];
    var used = insts.length;
    var quota = allowanceFor(acc, insts);
    var allowanceRows = allowanceBreakdown(acc, insts);
    var detail = "";
    if (s === "provisioning") {
      detail = '<section class="card" style="margin-top:18px"><div class="hero-status info"><span class="big-icon">' + icon("clock") + '</span><div><h2>Your workspace is being prepared</h2><p>You can close this page. We will email you the moment it is ready.</p></div></div>' +
        '<div class="progress-steps"><div class="step done"><span class="step-dot">' + icon("check") + '</span>Order received</div><div class="step active"><span class="step-dot">2</span>Preparing workspace</div><div class="step"><span class="step-dot">3</span>Configuring services</div><div class="step"><span class="step-dot">4</span>Final checks</div></div>' +
        '<div class="banner">' + icon("clock") + "<div><strong>Several minutes expected</strong><br><span class=\"small\">Setup continues safely in the background.</span></div></div></section>";
    }
    if (s === "failed") {
      detail = '<div class="banner danger" style="margin-top:18px">' + icon("error") + "<div><strong>Setup could not be completed</strong><br><span class=\"small\">SteProTECH has been notified. Our team will investigate and contact you.</span></div></div>";
    }
    if (s === "expired" && insts.length) {
      detail = '<div class="banner danger" style="margin-top:18px">' + icon("lock") + "<div><strong>This workspace is currently unreachable</strong><br><span class=\"small\">Your data is safe. Renew your subscription to restore access and automations.</span></div></div>";
    }
    var addBtn = used < quota && acc && acc.subscription_status === "active"
      ? '<button class="button" data-action="add-workspace">' + icon("plus") + " Add workspace</button>" : "";
    var subscribedNow = acc && acc.subscription_status === "active";
    var emptyCopy = subscribedNow
      ? "Your subscription is active. Add a workspace to start building automations."
      : "Subscribe and provision to create your first workspace.";
    var listHtml = insts.length ? insts.map(function (i) {
      var st = i.locked ? "locked" : i.status;
      var badge = i.locked ? statusBadge("off", "Switched off")
        : i.status === "healthy" ? statusBadge("running", "Running")
        : i.status === "provisioning" ? statusBadge("provisioning", "Being set up")
        : i.status === "failed" ? statusBadge("failed", "Setup failed") : statusBadge("off", cap(st));
      var action = i.status === "healthy" && !i.locked
        ? '<a class="button small-btn" href="https://' + esc(i.domain) + '/" target="_blank" rel="noopener">Open ' + icon("external") + "</a>"
        : '<button class="button secondary small-btn" data-action="workspace-detail" data-id="' + i.id + '">Details</button>';
      return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
        "<div><h3>" + esc(titleCase(i.stack_name)) + " workspace" + wsPlanTag(i) + "</h3><p>https://" + esc(i.domain) + "<br>Created " + esc(fmtDate(i.created_at)) + "</p></div>" +
        badge + action + selfUpdateHtml(i) + "</article>";
    }).join("")
      : '<div class="empty-state" style="min-height:240px"><div><h3>No workspaces yet</h3><p class="muted">' + esc(emptyCopy) + "</p></div></div>";
    return appLayout("customer", "My workspaces", "workspace",
      '<main class="page">' + pageHead("My workspaces", "Open, monitor, and request additional private automation workspaces.", '<div class="actions">' + addBtn + "</div>") +
      '<section class="card flush">' + listHtml + "</section>" + detail +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Workspace allowance</h3><strong>' + used + " of " + quota + " used</strong></div>" +
        '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div>' +
        '<span>' + Math.max(0, quota - used) + " available</span></div>" +
        (allowanceRows.length > 1 ? '<p class="small muted" style="margin:14px 0 0">' + esc(allowanceRows.join(" - ")) + "</p>" : "") +
        '<p class="small muted" style="margin:18px 0 0">Each workspace receives its own secure SteProTECH address. Contact support if you need more than your current allowance.</p></section>' +
      "</main>");
  }

  function customerBackups() {
    var acc = state.session ? state.session.account : null;
    var insts = state.session ? (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }) : [];
    var backups = state.backups || [];
    var instById = {};
    insts.forEach(function (i) { instById[i.id] = i; });
    var rows = backups.map(function (b) {
      var inst = instById[b.instance_id];
      var label = inst ? titleCase(inst.stack_name) : ("#" + b.instance_id);
      var kindLabel = b.kind === "full" ? "Full workspace" : b.kind === "workflows" ? "Workflows" : "Credentials";
      var sizeTxt = b.size_bytes ? fmtBytes(b.size_bytes) : "pending";
      var status = b.status === "ready" ? statusBadge("running", "Ready")
        : b.status === "failed" ? statusBadge("failed", "Failed")
        : statusBadge("provisioning", "Creating");
      var dl = b.status === "ready"
        ? '<a class="button small-btn" href="#" data-action="backup-download" data-id="' + b.id + '" data-kind="' + esc(b.kind) + '">' + icon("download") + " Download</a>" : "";
      return '<div class="workspace-item"><span class="workspace-logo">' + icon("archive") + "</span>" +
        "<div><h3>" + esc(kindLabel) + " &middot; " + esc(label) + "</h3>" +
        "<p>" + esc(fmtDate(b.created_at)) + " &middot; " + esc(sizeTxt) + (b.error ? " &middot; " + esc(b.error) : "") + "</p></div>" +
        status + dl + "</div>";
    }).join("");
    var listHtml = rows ? rows : (state.backupsLoaded
      ? '<div class="empty-state"><div><h3>No backups yet</h3>' +
        '<p class="muted">Create a backup of your workspace below.</p></div></div>'
      : loadingBlock("Loading backups...", "Fetching your workspace backups.", 240));

    var buttonsByInst = insts.map(function (i) {
      var st = i.locked ? "locked" : i.status;
      var disabled = (st !== "running" && st !== "healthy") || i.locked;
      return '<div class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
        "<div><h3>" + esc(titleCase(i.stack_name)) + " workspace</h3><p>https://" + esc(i.domain) + "</p></div>" +
        '<div class="actions end" style="margin-left:auto">' +
        '<button class="button small-btn" data-action="backup-run" data-id="' + i.id + '" data-kind="full"' + (disabled ? " disabled" : "") + '>' + icon("archive") + " Full backup</button>" +
        '<button class="button secondary small-btn" data-action="backup-run" data-id="' + i.id + '" data-kind="workflows"' + (disabled ? " disabled" : "") + '>Export workflows</button>' +
        "</div></div>";
    }).join("");

    return appLayout("customer", "Backups", "backup",
      '<main class="page">' + pageHead("Backups", "Create and download a copy of your workspace data at any time.", "") +
      '<section class="card"><div class="card-head"><h3>Create a backup</h3><p class="small muted" style="margin:0">Choose what to back up for your workspace.</p></div>' +
      (buttonsByInst || '<p class="muted" style="padding:20px">You have no active workspace to back up yet.</p>') + "</section>" +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Backup history</h3><p class="small muted" style="margin:0">You can download any completed backup below.</p></div>' + listHtml + "</section>" +
      "</main>");
  }

  function fmtBytes(n) {
    if (!n) return "0 B";
    var u = ["B", "KB", "MB", "GB"]; var i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 10 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
  }

  // Package copy (0.1.69): distinct contents per package, keyed by price so it
  // works whichever Paystack plan id comes back.
  function isPlusPlan(p) { return !!p && (p.key ? p.key === "b" : p.amount_minor === 50000); }
  function wsPlanTag(i) {
    // Package chip for a workspace row (0.1.70): each workspace carries its own package.
    return i && i.plan === "b" ? ' <span class="status info">Plus</span>' : ' <span class="status neutral">GHS 300</span>';
  }
  function planBlurbFor(p) {
    return isPlusPlan(p)
      ? '<p class="muted">Everything in n8n Workspace, plus Python and hands-on AI assistant setup help.</p>'
      : '<p class="muted">A private, managed n8n environment for your business automations.</p>';
  }
  function planFeaturesFor(p) {
    if (isPlusPlan(p)) {
      return [
        "Everything in n8n Workspace",
        "Python code node - managed Python environment attached by our team",
        "AI assistant setup - guided configuration of your n8n AI Assistant with hands-on help from SteProTECH",
        "Priority email support",
        "Automatic annual renewal"
      ];
    }
    return [
      "Your own private n8n workspace",
      "Your own secure address (https)",
      "Full workflow builder - unlimited workflows",
      "400+ app integrations and AI model nodes",
      "JavaScript code node",
      "Email support",
      "Automatic annual renewal"
    ];
  }

  // Resource specs (0.1.73): rendered straight from the API `specs` list, which
  // the backend derives from the real stack template - so the card can never
  // advertise resources the workspace does not actually get.
  function planSpecsBlock(p) {
    var rows = (p && p.specs) || [];
    if (!rows.length) return "";
    return '<div>' +
      '<p class="small muted" style="margin:24px 0 2px;font-weight:700">Resources included</p>' +
      '<dl class="info-list">' + rows.map(function (r) {
        return '<div class="info-row"><dt class="small">' + esc(r.label) + '</dt>' +
          '<dd class="small" style="text-align:right;margin-left:auto">' + esc(r.value) + "</dd></div>";
      }).join("") + "</dl></div>";
  }

  function subscriptionsList(acc, instances) {
    // One row per package the customer actually holds (0.1.71): account plan +
    // any workspace whose package differs shows as its own subscription line
    // with its own renewal date. Annual interval, Paystack - same for all.
    var live = (instances || []).filter(function (i) { return i.status !== "deleted"; });
    var accPlan = (acc && acc.plan) === "b" ? "b" : "a";
    var periodEnd = acc && acc.paid_until ? fmtDate(acc.paid_until) : "not set";
    var seen = {};
    var rows = [];
    function row(planKey, label, note) {
      if (seen[planKey]) return;
      seen[planKey] = true;
      rows.push('<div class="info-row"><dt>' + esc(label) + '</dt><dd>' + esc(note) +
        '<br><span class="small muted">Annually - renews ' + esc(periodEnd) + ' - Paystack</span></dd></div>');
    }
    var accLabel = accPlan === "b" ? "n8n Workspace Plus subscription (GHS 500/yr)" : "n8n Workspace subscription (GHS 300/yr)";
    row(accPlan, accLabel, "Account package");
    live.forEach(function (i) {
      var wp = i.plan === "b" ? "b" : "a";
      if (wp !== accPlan) {
        row(wp, wp === "b" ? "n8n Workspace Plus subscription (GHS 500/yr)" : "n8n Workspace subscription (GHS 300/yr)",
            "Workspace: " + (i.stack_name || "workspace"));
      }
    });
    if (!rows.length) {
      rows.push('<div class="info-row"><dt>Plan</dt><dd>No active subscription</dd></div>');
    }
    return '<dl class="info-list">' + rows.join("") + "</dl>";
  }

  function customerBilling() {
    var acc = state.session ? state.session.account : null;
    var s = subscriptionState();
    var plans = state.plans;
    var sub = acc ? (acc.subscription_status || "none") : "none";
    // Subscribed = account active AND nothing blocking the workspace. If the
    // workspace is locked/expiring (even briefly before the sweep re-labels the
    // account), the call-to-action must be Renew, never Already subscribed.
    var subscribed = sub === "active" && s !== "expired" && s !== "pastdue" && s !== "cancelled";
    var due = s === "pastdue" || s === "expired" || s === "cancelled" || sub === "locked" || sub === "past_due" || sub === "unpaid";
    var openPay = state.paymentsOpen !== false;
    var held = heldPackages(acc, state.session ? (state.session.instances || []) : []);
    var planCards = plans.map(function (p) {
      var key = p.key || (p.amount_minor === 50000 ? "b" : "a");
      var holdsThis = !!held[key];
      var activePlan = !!p.active;
      if (activePlan) {
        // Only the package the customer actually holds may read "Already
        // subscribed" (Steward 2026-09-27); the other one stays actionable so
        // it can be added later as a second subscription.
        var cta;
        if (holdsThis && subscribed) {
          cta = '<span class="plan-already">' + icon("check") + " Already subscribed</span>";
        } else if (holdsThis && due) {
          cta = '<button class="button warning" data-action="pay-now" data-plan="' + key + '">Renew subscription</button>';
        } else if (!openPay) {
          cta = '<button class="button' + (due ? " warning" : "") + '" disabled title="New payments are paused by SteProTECH">Payments on hold</button>';
        } else {
          cta = '<button class="button" data-action="pay-now" data-plan="' + key + '">Subscribe</button>';
        }
        // NB: read paid_until straight off the account - the page-level
        // `periodEnd` below is declared later and would be undefined here.
        var renewal = acc && acc.paid_until ? fmtDate(acc.paid_until) : "";
        var stateLine = holdsThis
          ? '<p class="small muted" style="margin:14px 0 0">Your package - ' +
            (renewal ? "renews on " + esc(renewal) + " at " : "") +
            esc(fmtMinor(p.amount_minor, p.currency)) + " per year.</p>"
          : '<p class="small muted" style="margin:14px 0 0">Not subscribed yet - you can add it any time. Each package carries its own workspace allowance.</p>';
        return '<article class="plan-card featured"><div class="eyebrow">' + (holdsThis ? "Your package" : "Available now") + "</div><h2>" + esc(p.name) + "</h2>" +
          '<div class="plan-price">' + esc(fmtMinor(p.amount_minor, p.currency)) + ' <span>per year</span></div>' +
          planBlurbFor(p) + stateLine +
          '<ul class="feature-list">' + planFeaturesFor(p).map(function (f) { return "<li>" + icon("check") + "<span>" + esc(f) + "</span></li>"; }).join("") + "</ul>" +
          planSpecsBlock(p) +
          cta + "</article>";
      }
      return '<article class="plan-card disabled"><span class="ribbon">Coming soon</span><div class="eyebrow">Future plan</div><h2>' + esc(p.name) + "</h2>" +
        '<div class="plan-price">' + esc(fmtMinor(p.amount_minor, p.currency)) + ' <span>per year</span></div>' +
        "<p>A future higher tier. Additional features will be announced before it becomes available.</p>" +
        '<button class="button" disabled>Coming soon</button></article>';
    }).join("") || '<p class="muted">Plan information unavailable.</p>';

    var statusRow = sub === "active" ? statusBadge("active", "Active")
      : sub === "past_due" ? statusBadge("pastdue", "Past due")
      : sub === "locked" ? statusBadge("expired", "Expired")
      : sub === "canceled" ? statusBadge("cancelled", "Cancelled") : statusBadge("pending", "No active subscription");
    var periodEnd = acc && acc.paid_until ? fmtDate(acc.paid_until) : "-";

    var holdBanner = openPay ? "" :
      '<div class="banner warning" style="margin-bottom:16px">' + icon("lock") +
      '<div><strong>Payments are on hold</strong><br><span class="small">New subscriptions and renewals are paused right now. If you were onboarded as a free user, your access was arranged by SteProTECH - you do not need to pay.</span></div></div>';
    return appLayout("customer", "Plans & billing", "billing",
      '<main class="page">' + pageHead("Plans & billing", "Manage your annual subscription and understand what happens next.") +
      holdBanner +
      '<div class="plan-grid">' + planCards + "</div>" +
      '<div class="grid cols-2" style="margin-top:20px">' +
        '<section class="card"><div class="card-head"><h2>Subscription details</h2>' + statusRow + "</div>" +
          subscriptionsList(acc, state.session ? (state.session.instances || []) : []) + '</section>' +
        '<section class="card"><h2>How renewal works</h2><div class="timeline">' +
          '<div class="timeline-item"><h3>Your card is charged</h3><p>At the end of the 12-month paid period.</p></div>' +
          '<div class="timeline-item"><h3>You receive the result by email</h3><p>Successful renewal keeps everything running.</p></div>' +
          '<div class="timeline-item"><h3>A grace period protects you</h3><p>If payment fails, you have time to retry before the workspace is switched off.</p></div>' +
          '<div class="timeline-item"><h3>Renewal restores access</h3><p>A later successful payment switches the workspace back on with your work intact.</p></div>' +
        "</div></section>" +
      "</div></main>");
  }

  function supportPage() {
    var acc = state.session ? state.session.account : null;
    var email = acc ? acc.email : "";
    return appLayout("customer", "Support", "support",
      '<main class="page">' + pageHead("How can we help?", "Send a message to the SteProTECH support team.") +
      '<div class="grid cols-2"><section class="card"><h2>Contact support</h2>' +
        '<p class="muted">For portal access, billing, or workspace availability questions.</p>' +
        '<div class="banner" style="margin:6px 0 18px">' + icon("mail") + "<div><strong>support@steprotech.com</strong><br><span class=\"small\">We reply during support hours, Monday to Friday, 8 AM to 5 PM.</span></div></div>" +
        '<a class="button" href="mailto:support@steprotech.com?subject=' + encodeURIComponent("n8n portal support request") + (email ? "&body=" + encodeURIComponent("Account email: " + email + "\n\nDescribe your issue:") : "") + '">' + icon("mail") + " Email support</a></section>" +
        '<aside class="card"><h2>Before you write</h2>' +
          '<div class="banner">' + icon("support") + "<div><strong>Portal and n8n passwords are separate</strong><br><span class=\"small\">Portal password help is handled by SteProTECH. Your n8n workspace has its own password-recovery option.</span></div></div>" +
          '<dl class="info-list" style="margin-top:20px"><div class="info-row"><dt>Email</dt><dd>support@steprotech.com</dd></div>' +
          '<div class="info-row"><dt>Support hours</dt><dd>Mon to Fri, 8 AM to 5 PM</dd></div>' +
          '<div class="info-row"><dt>Account email</dt><dd>' + esc(email || "-") + "</dd></div></dl></aside>" +
      "</div></main>");
  }

  function accountPage() {
    var acc = state.session ? state.session.account : null;
    if (!acc) return signinPage();
    var sub = acc.subscription_status || "none";
    return appLayout("customer", "Account", "account",
      '<main class="page">' + pageHead("Account", "Review your identity and portal access details.") +
      '<div class="grid cols-2">' +
        '<section class="card"><div class="card-head"><h2>Profile</h2></div>' +
          '<dl class="info-list"><div class="info-row"><dt>Name</dt><dd>' + esc(acc.display_name || cap(acc.username)) + "</dd></div>" +
          '<div class="info-row"><dt>Email</dt><dd>' + esc(acc.email) + "</dd></div>" +
          '<div class="info-row"><dt>Username</dt><dd>' + esc(acc.username) + "</dd></div>" +
          '<div class="info-row"><dt>Joined</dt><dd>' + esc(fmtDate(acc.created_at)) + "</dd></div></dl></section>" +
        '<section class="card"><h2>Portal security</h2>' +
          '<p class="muted">Portal password changes are currently handled by the support team.</p>' +
          '<div class="banner">' + icon("shield") + "<div><strong>Your n8n sign-in is separate</strong><br><span class=\"small\">Changing a workspace password does not change this portal password.</span></div></div>" +
          '<a class="button secondary" style="margin-top:18px" href="mailto:support@steprotech.com">Contact support</a></section>' +
      "</div>" +
      '<section class="card" style="margin-top:18px"><h2>Sign out</h2><p class="muted">End your portal session on this device.</p>' +
        '<button class="button secondary" data-action="signout">Sign out</button></section>' +
      "</main>");
  }

  /* ================= admin pages ================= */

  function adminSignin() {
    return authLayout(
      '<div class="center-state"><div class="state-orb warning">' + icon("shield") + "</div>" +
      '<div class="eyebrow">Staff access</div><h1>Admin sign in</h1>' +
      '<p class="muted">Enter the administrator password to continue.</p></div>' +
      '<form data-form="admin-signin"><div class="field"><label>Password</label><div class="input-row">' +
      '<input id="admin-password" name="password" type="password" required>' +
      '<button type="button" class="input-action" data-action="toggle-password" data-target="admin-password">' + icon("eye") + '</button></div></div>' +
      '<button class="button primary-wide" type="submit">Sign in</button></form>' +
      '<p class="small muted" style="text-align:center;margin-top:20px">Customer? <a href="#/entry">Return to customer portal</a></p>'
    );
  }

  function adminOverview() {
    var accounts = state.adminAccounts || [];
    var requests = state.requests || [];
    // 0.1.98: nobody waits for an approval, so the useful number here is how many
    // people opened the form and have not finished registering. The old counter
    // (status 'requested') is dead and would sit at 0 forever.
    var pendingSignup = requests.filter(function (r) { return r.status === "approved" || r.status === "token_sent"; }).length;
    var pastDue = accounts.filter(function (a) { return a.subscription_status === "past_due"; }).length;
    var locked = accounts.filter(function (a) { return a.subscription_status === "locked" || a.subscription_status === "canceled"; }).length;
    var total = accounts.length;

    var requestRows = requests.slice(0, 6);
    // The four counters below are computed from two lists; before those lists
    // answer, a 0 is a claim about the business, not a fact (0.1.84).
    var acctKnown = state.adminAccountsLoaded;
    var reqKnown = state.requestsLoaded;

    return appLayout("admin", "Overview", "overview",
      '<main class="page">' + pageHead("Today at a glance", "See what needs attention and jump directly to the right task.") +
      '<div class="grid cols-4">' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon">' + icon("requests") + "</span>" + statusBadge("approved", "Not registered yet") + "</div>" +
          '<div class="metric-value">' + (reqKnown ? pendingSignup : "...") + '</div><p class="muted">Opened the form, not yet registered</p><a href="#/admin/requests">Review requests ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--amber-100);color:var(--amber-700)">' + icon("warning") + "</span></div>" +
          '<div class="metric-value">' + (acctKnown ? pastDue : "...") + '</div><p class="muted">Accounts past due</p><a href="#/admin/accounts">View accounts ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--green-100);color:var(--green-700)">' + icon("workspace") + "</span></div>" +
          '<div class="metric-value">' + (acctKnown ? locked : "...") + '</div><p class="muted">Locked / cancelled accounts</p><a href="#/admin/accounts">Review ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--slate-100);color:var(--slate-700)">' + icon("account") + "</span></div>" +
          '<div class="metric-value">' + (acctKnown ? total : "...") + '</div><p class="muted">Total customer accounts</p><a href="#/admin/accounts">View accounts ' + icon("arrow") + '</a></article>' +
      "</div>" +
      envHealthCards() +
      '<div class="grid cols-3 overview-row" style="margin-top:18px">' +
        '<section class="card flush span-2" style="padding:0 0 20px"><div class="card-head" style="padding:20px 20px 0"><h2>Access requests</h2><a class="button ghost small-btn" href="#/admin/requests">View all</a></div>' +
          '<div class="table-wrap"><table><thead><tr><th>Email</th><th>Status</th><th>Requested</th></tr></thead><tbody>' +
          (requestRows.length ? requestRows.map(function (r) {
            return "<tr><td><strong>" + esc(r.email) + "</strong></td><td>" + requestStatusLabel(r.status) + "</td><td>" + esc(fmtDateTime(r.created_at)) + "</td></tr>";
          }).join("") : (state.requestsLoaded
            ? '<tr><td colspan="3"><div class="empty-state" style="min-height:160px"><div><h3>No access requests</h3><p class="muted">New requests appear here.</p></div></div></td></tr>'
            : loadingRow(3, "Loading access requests...", "", 160))) +
          "</tbody></table></div></section>" +
        '<aside class="card"><h2>Quick actions</h2><div class="grid">' +
          '<a class="button" href="#/admin/requests">Review access requests</a>' +
          '<a class="button secondary" href="#/admin/accounts">Find a customer</a>' +
          '<a class="button secondary" href="#/admin/maintenance">Run expiry maintenance</a>' +
          '<a class="button secondary" href="#/admin/backups">' + icon("archive") + " Backups &amp; updates</a>" +
        "</div>" +
        "</aside></div></main>");
  }

  function requestStatusLabel(st) {
    if (st === "requested") return statusBadge("awaiting", "Awaiting approval");
    if (st === "approved" || st === "token_sent") return statusBadge("approved", "Approved");
    if (st === "denied") return statusBadge("denied", "Disapproved");
    return statusBadge("registered", "Already registered");
  }

  function adminRequests() {
    var requests = state.requests || [];
    var counts = {
      all: requests.length,
      awaiting: requests.filter(function (r) { return r.status === "requested"; }).length,
      approved: requests.filter(function (r) { return r.status === "approved" || r.status === "token_sent"; }).length,
      denied: requests.filter(function (r) { return r.status === "denied"; }).length
    };
    var filter = state.accessFilter || "all";
    var rows = requests.filter(function (r) {
      if (filter === "all") return true;
      if (filter === "awaiting") return r.status === "requested";
      if (filter === "approved") return r.status === "approved" || r.status === "token_sent";
      return r.status === "denied";
    });
    return appLayout("admin", "Access requests", "requests",
      '<main class="page">' + pageHead("Access requests", "Approve new customers, decline unwanted requests, or remove test/spam entries. Approved members move to the Accounts list once they register.") +
      '<section class="card flush"><div class="tabs">' +
        [["all", "All"], ["awaiting", "Awaiting approval"], ["approved", "Approved"], ["denied", "Disapproved"]].map(function (t) {
          return '<button class="tab' + (filter === t[0] ? " active" : "") + '" data-filter="' + t[0] + '">' + t[1] + " (" + counts[t[0]] + ")</button>";
        }).join("") +
      "</div>" +
      '<div class="table-wrap"><table><thead><tr><th>Email</th><th>Requested</th><th>Status</th><th>Action</th></tr></thead><tbody>' +
        (rows.length ? rows.map(function (r) {
          var canApprove = r.status !== "denied" && r.status !== "registered";
          var canCourse = r.status === "requested" || r.status === "approved" || r.status === "token_sent";
          var action = (canApprove ? '<button class="button small-btn" data-approve="' + r.id + '">Approve</button>' : "") +
            (canCourse ? '<button class="button ghost small-btn" data-request-deny="' + r.id + '">Disapprove</button>' : "") +
            '<button class="button ghost small-btn" data-request-delete="' + r.id + '">Delete</button>';
          return "<tr><td><strong>" + esc(r.email) + "</strong></td><td>" + esc(fmtDateTime(r.created_at)) + "</td><td>" + requestStatusLabel(r.status) + "</td><td>" + action + "</td></tr>";
        }).join("") : (state.requestsLoaded
          ? '<tr><td colspan="4"><div class="empty-state" style="min-height:220px"><div><h3>No matching requests</h3><p class="muted">New requests will appear here after someone enters their email on the portal.</p></div></div></td></tr>'
          : loadingRow(4, "Loading access requests...", "", 220))) +
      "</tbody></table></div></section></main>");
  }

  function archivedAccounts() {
    var accounts = state.adminArchived || [];
    return appLayout("admin", "Archived accounts", "accounts",
      '<main class="page">' + pageHead("Archived accounts", "Archived accounts are hidden from the portal. Nothing is deleted; restore brings an account back (suspended, workspace off) for review.",
        '<div class="actions"><a class="button secondary" href="#/admin/accounts">' + icon("arrow-left") + " All accounts</a></div>") +
      '<section class="card flush"><div class="table-wrap"><table><thead><tr><th>Customer</th><th>Email</th><th>Archived state</th><th></th></tr></thead><tbody>' +
      (accounts.length ? accounts.map(function (a) {
        return "<tr>" +
          '<td><div class="customer-cell"><span class="avatar">' + esc(initialsOf(a.display_name || a.username)) + '</span><span class="cell-text"><strong>' + esc(a.display_name || cap(a.username)) + "</strong></span></div></td>" +
          "<td>" + esc(a.email) + "</td>" +
          "<td>" + statusBadge("archived", "Archived") + "</td>" +
          '<td><button class="button small-btn" data-action="admin-restore" data-id="' + a.id + '">Restore</button></td></tr>';
      }).join("") : (state.adminArchivedLoaded
        ? '<tr><td colspan="4"><div class="empty-state" style="min-height:220px"><div><h3>No archived accounts</h3><p class="muted">Archived accounts move here instead of being deleted.</p></div></div></td></tr>'
        : loadingRow(4, "Loading archived accounts...", "", 220))) +
      "</tbody></table></div></section></main>");
  }

  function securityPage(isAdmin) {
    var path = isAdmin ? "/admin/security" : "/me/security";
    var st = state.security2fa;
    var toggles = "";
    var methods = (st && st.methods) || [];
    function has(m) { return methods.some(function (x) { return x.method === m; }); }
    // TOTP card
    var totpEnabled = st && st.totp_enabled;
    var totpCard =
      '<section class="card"><div class="card-head"><h2>Authenticator app</h2>' +
      (totpEnabled ? statusBadge("active", "On") : statusBadge("pending", "Off")) + "</div>" +
      '<p class="muted">Use an authenticator app (Google Authenticator, Authy, 1Password) to generate a 6-digit code at sign-in.</p>' +
      (totpEnabled
        ? '<button class="button secondary" data-action="sec-totp-disable" data-kind="' + (isAdmin ? "admin" : "user") + '">Turn off authenticator 2FA</button>'
        : '<button class="button" data-action="sec-totp-setup" data-kind="' + (isAdmin ? "admin" : "user") + '">' + icon("shield") + " Set up authenticator</button>") +
      "</section>";
    // Email card
    var emailEnabled = st && st.email_2fa;
    var emailCard =
      '<section class="card"><div class="card-head"><h2>Email code</h2>' +
      (emailEnabled ? statusBadge("active", "On") : statusBadge("pending", "Off")) + "</div>" +
      '<p class="muted">We send a 6-digit code to your email at sign-in.</p>' +
      (emailEnabled
        ? '<button class="button secondary" data-action="sec-email-disable" data-kind="' + (isAdmin ? "admin" : "user") + '">Turn off email 2FA</button>'
        : '<button class="button" data-action="sec-email-setup" data-kind="' + (isAdmin ? "admin" : "user") + '">' + icon("mail") + " Set up email 2FA</button>") +
      "</section>";
    // Passkey card
    var passkeys = (st && st.passkeys) || [];
    var passkeyList = passkeys.length
      ? '<div class="passkey-list" style="margin-top:12px">' + passkeys.map(function (p) {
          return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line)"><span>' + icon("key") + " " + esc(p.name || "Passkey") + ' <span class="muted small">' + esc(p.transports || "") + "</span></span>" +
            '<button class="button danger small-btn" data-action="sec-passkey-delete" data-kind="' + (isAdmin ? "admin" : "user") + '" data-cred="' + esc(p.credential_id) + '">Remove</button></div>';
        }).join("") + "</div>"
      : "";
    var passkeyCard =
      '<section class="card"><div class="card-head"><h2>Passkey</h2>' +
      (passkeys.length ? statusBadge("active", "On") : statusBadge("pending", "Off")) + "</div>" +
      '<p class="muted">Sign in with a passkey (Windows Hello, Touch ID, a security key) instead of a password. Your device stores the private key; we only keep a public key.</p>' +
      '<button class="button" data-action="sec-passkey-setup" data-kind="' + (isAdmin ? "admin" : "user") + '">' + icon("key") + " Add a passkey</button>" +
      passkeyList +
      "</section>";
    // SMS card (0.1.90). Admin only in this release: customers keep the two channels
    // they already have, and SMS needs a phone number per customer plus their opt-in.
    var smsOn = !!(st && st.sms_2fa);
    var hasPhone = !!(st && st.has_phone);
    var smsCard = isAdmin
      ? '<section class="card"><div class="card-head"><h2>Text message (SMS)</h2>' +
        (smsOn ? statusBadge("active", "On") : statusBadge("pending", "Off")) + "</div>" +
        '<p class="muted">We text a 6-digit code to your phone at sign-in' +
        (hasPhone && st.admin_phone_masked ? " (" + esc(st.admin_phone_masked) + ")" : "") + ".</p>" +
        (smsOn
          ? '<button class="button secondary" data-action="sec-sms-disable">Turn off SMS 2FA</button>'
          : (hasPhone
            ? '<button class="button" data-action="sec-sms-setup">' + icon("phone") + " Set up SMS 2FA</button>"
            : '<p class="small muted">Add your phone number in <a href="#/admin/settings">Settings</a> first, then come back here.</p>')) +
        "</section>"
      : "";
    var heading = isAdmin ? "Admin security" : "Account security";
    // "How it works" lives in the second row as a real card (2x2 grid) so the
    // page shows four evenly-spaced cards that look identical to their
    // siblings - not a full-width banner under the grid.
    var howCard =
      '<section class="card"><div class="card-head"><h2>How it works</h2></div>' +
      '<p class="muted">After entering your password, you will be asked for a code. Add at least one factor so you cannot be locked out if you lose a device.</p>' +
      '<p class="muted" style="margin-top:6px">A passkey is a first-factor sign-in: when set up, you can sign in without a password.</p></section>';
    return appLayout(isAdmin ? "admin" : "customer", "Security", "security",
      '<main class="page">' + pageHead(heading, "Add a second factor to your sign-in. You can use any combination; a code from one of them is required at login.") +
      '<div class="grid cols-2" style="margin-top:10px">' + totpCard + emailCard + "</div>" +
      (isAdmin
        ? '<div class="grid cols-2" style="margin-top:10px">' + smsCard + passkeyCard + "</div>" +
          '<div style="margin-top:10px">' + howCard + "</div>"
        : '<div class="grid cols-2" style="margin-top:10px">' + passkeyCard + howCard + "</div>") +
      "</main>");
  }

  function subLabel(sub) {
    return titleCase(String(sub || "none").replace("_", " "));
  }

  function adminAccounts() {
    var accounts = state.adminAccounts || [];
    var archivedCount = (state.adminArchived || []).length;
    return appLayout("admin", "Accounts", "accounts",
      '<main class="page">' + pageHead("Customer accounts", "Review subscriptions, quotas, and workspace availability.", '<div class="actions"><button class="button" data-action="admin-add-user">' + icon("plus") + " Add user</button>" +
        (archivedCount ? '<a class="button secondary" href="#/admin/archived">' + icon("archive") + " Archived (" + archivedCount + ")</a>" : "") +
        '<a class="button secondary" href="#/admin/requests">' + icon("requests") + " New requests</a></div>") +
      '<section class="card flush"><div class="table-tools"><div class="search"><input id="account-search" type="search" placeholder="Search name, email, or username" aria-label="Search accounts"></div></div>' +
      // The header already says Backup / Update, so the buttons only need the verb: the full
      // sentence lives in the tooltip, the confirm popup and the accessible name (Steward,
      // 2026-10-07 - the wide labels were what forced the table to scroll sideways).
      '<div class="table-wrap"><table class="accounts-table"><thead><tr><th>Customer</th><th>Plan</th><th>Subscription</th><th>Period end</th><th>Quota</th><th>Backup</th><th>Update</th><th></th></tr></thead><tbody>' +
        (accounts.length ? accounts.map(function (a) {
          var badge = a.subscription_status === "active" ? statusBadge("active", "Active")
            : a.subscription_status === "past_due" ? statusBadge("pastdue", "Past due")
            : a.subscription_status === "locked" ? statusBadge("locked", "Locked")
            : a.subscription_status === "canceled" ? statusBadge("cancelled", "Cancelled")
            : statusBadge("pending", "No subscription");
          var stateBadge = a.account_state === "suspended" ? statusBadge("suspended", "Suspended")
            : a.account_state === "archived" ? statusBadge("archived", "Archived") : "";
          var bkBadge = a.backup_enabled ? statusBadge("active", "On") : statusBadge("off", "Off");
          var updBadge = a.update_enabled ? statusBadge("active", "On") : statusBadge("off", "Off");
          return "<tr data-account-row=\"" + a.id + "\">" +
            // The name IS the way into the account (Steward, 2026-10-07): a real link, so
            // keyboard users can open it too, and the actions cell no longer needs a
            // "View account" button (which is what pushed the table into a sideways scroll).
            '<td><a class="customer-cell cell-link" href="#/admin/account/' + a.id + '" data-route="/admin/account/' + a.id + '" title="Open this account"><span class="avatar">' + esc(initialsOf(a.display_name || a.username)) + '</span><span class="cell-text"><strong>' + esc(a.display_name || cap(a.username)) + "</strong><span>" + esc(a.email) + "</span></span></a></td>" +
            "<td>" + esc(planName(a)) + "</td>" +
            "<td>" + badge + (stateBadge ? " " + stateBadge : "") + "</td>" +
            "<td>" + esc(fmtDate(a.paid_until)) + "</td>" +
            "<td>" + (a.quota || 1) + "</td>" +
            '<td class="cell-actions-td"><div class="cell-actions fill">' + bkBadge +
            '<button class="button secondary small-btn" data-action="admin-backup-toggle" data-id="' + a.id + '" data-on="' + (a.backup_enabled ? "1" : "0") + '" title="' + (a.backup_enabled ? "Revoke backup access for this account" : "Allow backup access for this account") + '" aria-label="' + (a.backup_enabled ? "Revoke backup access for this account" : "Allow backup access for this account") + '">' + (a.backup_enabled ? "Revoke" : "Allow") + "</button></div></td>" +
            '<td class="cell-actions-td"><div class="cell-actions fill">' + updBadge +
            '<button class="button secondary small-btn" data-action="admin-update-toggle" data-id="' + a.id + '" data-on="' + (a.update_enabled ? "1" : "0") + '" title="' + (a.update_enabled ? "Revoke update access for this account" : "Allow update access for this account") + '" aria-label="' + (a.update_enabled ? "Revoke update access for this account" : "Allow update access for this account") + '">' + (a.update_enabled ? "Revoke" : "Allow") + "</button></div></td>" +
            '<td class="cell-actions-td"><div class="cell-actions">' + (a.account_state === "active" ? '<button class="button secondary small-btn" data-action="admin-impersonate" data-id="' + a.id + '" title="Login as user" aria-label="Login as user">' + icon("eye") + " Login</button>" : "") +
            "</div></td></tr>";
        }).join("") : (state.adminAccountsLoaded
          ? '<tr><td colspan="8"><div class="empty-state" style="min-height:240px"><div><h3>No customer accounts yet</h3><p class="muted">Accounts appear here after registration.</p></div></div></td></tr>'
          : loadingRow(8, "Loading customer accounts...", "", 240))) +
      "</tbody></table></div></section></main>");
  }

  /* ---- admin Updates page (0.1.85, Steward 2026-10-07) -----------------------
   * This page is where the pacing is explained as well as performed: one workspace
   * at a time PER SERVER, five minutes of quiet after each one is verified stable,
   * an admin batch ahead of customer requests, and a single manual update from the
   * workspace row that never waits (it only holds that server's slot).
   */
  function updateStateBadge(w, latestKnown) {
    if (w.queued) {
      return w.queued.status === "running" ? statusBadge("provisioning", "Updating now")
        : statusBadge("awaiting", "Queued" + (w.queued.position ? " (" + w.queued.position + ")" : ""));
    }
    if (w.locked) return statusBadge("off", "Locked");
    if (w.status === "provisioning") return statusBadge("neutral", "Being set up");
    if (latestKnown === false) return statusBadge("warning", "Cannot compare");
    if (w.version_unknown) return statusBadge("neutral", "Version unknown");
    if (w.needs_update) return statusBadge("awaiting", "Needs update");
    return statusBadge("running", "Up to date");
  }
  function updateRowHtml(w, sel, latestKnown) {
    var host = "server " + w.host;
    var last = w.last;
    var lastTxt = last
      ? (last.status === "done" ? "Updated to " + esc(last.to_version || "") + " on " + esc(fmtDate(last.at || 0))
        : last.status === "failed" ? "Failed: " + esc((last.error || "see report"))
        : esc(last.status) + " on " + esc(fmtDate(last.at || 0)))
      : "Never updated from here";
    return "<tr>" +
      '<td style="padding:8px"><input type="checkbox" data-upd-check value="' + w.instance_id + '"' +
        (sel[w.instance_id] ? " checked" : "") +
        (w.queued ? " disabled" : "") + ' aria-label="Select ' + esc(w.stack_name) + '"></td>' +
      "<td style=\"padding:8px\"><b>" + esc(titleCase(w.stack_name)) + "</b><br>" +
        '<span class="small muted">' + esc(w.domain || "") + "</span></td>" +
      '<td style="padding:8px">' + esc(w.account) + '<br><span class="small muted">' + esc(w.plan === "b" ? "Plus (GHS 500)" : "GHS 300") + "</span></td>" +
      '<td style="padding:8px">' + esc(host) + '<br><span class="small muted">' + esc(w.host_name || "") + "</span></td>" +
      '<td style="padding:8px">' + (w.current_version ? esc(w.current_version) : '<span class="muted">unknown</span>') +
        (w.floating_tag ? '<br><span class="small muted">floating tag</span>' : "") + "</td>" +
      '<td style="padding:8px">' + esc(w.latest_version || "") + "</td>" +
      '<td style="padding:8px">' + updateStateBadge(w, latestKnown) + "</td>" +
      '<td style="padding:8px"><span class="small muted">' + lastTxt + "</span></td>" +
      '<td style="padding:8px">' + (w.queued ? ""
        : '<button class="button secondary small-btn" data-action="updates-queue-one" data-id="' + w.instance_id + '">Update</button>') + "</td>" +
      "</tr>";
  }
  function adminUpdates() {
    var head = pageHead("Workspace updates",
      "Move customer workspaces to the latest n8n release, one workspace per server at a time.",
      '<div class="actions"><button class="button secondary small-btn" data-action="updates-refresh">Refresh</button>' +
      '<button class="button secondary small-btn" data-action="updates-select-none">Clear selection</button></div>');
    if (!state.adminUpdatesLoaded) {
      return appLayout("admin", "Updates", "backup",
        '<main class="page">' + head +
        '<section class="card">' +
        loadingBlock("Loading the update queue...", "Asking each server what it is running.", 220) +
        "</section></main>");
    }
    var d = state.adminUpdates || {};
    if (d.load_error) {
      return appLayout("admin", "Updates", "backup",
        '<main class="page">' + head +
        '<section class="card"><p class="muted" role="status" style="margin:0">The update queue could not ' +
        "be loaded: " + esc(d.load_error) + " Use Refresh to try again.</p></section></main>");
    }
    var ws = d.workspaces || [];
    var behind = ws.filter(function (w) { return w.needs_update; });
    var unknown = ws.filter(function (w) { return w.version_unknown; });
    var queue = d.queue || [];
    var waiting = queue.filter(function (q) { return q.status === "queued"; });
    var sel = state.updSel || {};
    var selCount = Object.keys(sel).length;

    var compareWarn = d.compare_note
      ? '<section class="card"><p class="muted" role="status" style="margin:0">' +
        esc(d.compare_note) + "</p></section>"
      : "";
    var ruleCard = compareWarn + '<section class="card"><div class="card-head"><h3>How updates run</h3>' +
      statusBadge(d.worker_enabled ? "running" : "off", d.worker_enabled ? "Queue running" : "Queue paused") + "</div>" +
      '<ul class="small muted" style="margin:10px 0 0 18px">' +
      "<li>One workspace updates at a time <b>on each server</b>, so three servers can move at once and no box ever takes two.</li>" +
      "<li>After a workspace is verified stable that server rests <b>" + esc(String(d.interval_minutes)) +
        (Number(d.interval_minutes) === 1 ? " minute</b>" : " minutes</b>") + " before its next one starts.</li>" +
      "<li>Your batch goes <b>ahead of customer requests</b>.</li>" +
      "<li>A manual update from a workspace row <b>starts immediately</b> and takes that server's slot while it runs.</li>" +
      "<li>Latest n8n release right now: <b>" + esc(d.latest_version || "could not be resolved") + "</b>" +
        (d.latest_source === "release" ? "" : " (source: " + esc(d.latest_source || "unknown") + ")") + "</li>" +
      "<li>Report emails go to <b>" + esc(d.report_to || "") + "</b> when a run finishes, and immediately when one fails.</li>" +
      "<li>No safety backup is taken before an update (your instruction), so a failure is rolled back by image tag instead.</li>" +
      "</ul>" +
      '<div class="actions" style="margin-top:14px">' +
      '<button class="button" data-action="updates-queue-all"' + (behind.length && d.latest_known ? "" : " disabled") + ">Update all that need it (" +
        behind.length + ")</button>" +
      '<button class="button secondary" data-action="updates-queue-selected"' + (selCount ? "" : " disabled") +
        ">Update selected (" + selCount + ")</button>" +
      "</div>" +
      (unknown.length ? '<p class="small muted" style="margin-top:12px">' + unknown.length +
        " workspace(s) report an undetermined version, so they are not claimed as behind. Update them by hand if you want them moved.</p>" : "") +
      "</section>";

    var hostCards = (d.hosts || []).map(function (h) {
      var per = (d.interval_minutes || 5) + (d.typical_minutes || 3);
      return '<section class="card"><div class="card-head"><h3>' + esc(h.name || ("server " + h.environment_id)) +
        ' <span class="muted small">server ' + h.environment_id + "</span></h3>" +
        (h.running ? statusBadge("provisioning", "Updating now") : statusBadge(h.queued ? "awaiting" : "running", h.queued ? "In line" : "Idle")) +
        "</div>" +
        "<p class=\"small muted\" style=\"margin:8px 0 0\">" +
        (h.running ? "Running: <b>" + esc(titleCase(h.running.stack_name || "")) + "</b> since " +
            esc(fmtClock(Date.now() - (h.running.elapsed_seconds || 0) * 1000)) +
            " (" + Math.round((h.running.elapsed_seconds || 0) / 60) + " min)"
          : "Nothing running on this server.") + "</p>" +
        '<p class="small muted" style="margin:6px 0 0">In line: <b>' + h.queued + "</b> (" + h.ahead_admin +
        " from you, " + h.ahead_customer + " from customers)" +
        (h.queued ? " &middot; finishing in about " + (h.queued * per) + " minutes" : "") + "</p>" +
        '<p class="small muted" style="margin:6px 0 0">Next update may start: <b>' +
        (h.next_allowed_at && h.next_allowed_at > Math.floor(Date.now() / 1000)
          ? esc(fmtClock(h.next_allowed_at * 1000)) : "now") + "</b></p></section>";
    }).join("");

    var table = '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Workspaces</h3>' +
      '<span class="small muted">' + ws.length + " total, " + behind.length + " behind</span></div>" +
      (ws.length ? '<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:13px">' +
        '<tr style="background:#f9fafb;text-align:left">' +
        "<th style=\"padding:8px\"> </th><th style=\"padding:8px\">Workspace</th>" +
        "<th style=\"padding:8px\">Account</th><th style=\"padding:8px\">Server</th>" +
        "<th style=\"padding:8px\">Running</th><th style=\"padding:8px\">Latest</th>" +
        "<th style=\"padding:8px\">State</th><th style=\"padding:8px\">Last update</th><th style=\"padding:8px\"></th>" +
        "</tr>" + ws.map(function (w) { return updateRowHtml(w, sel, d.latest_known); }).join("") + "</table></div>"
        : loadingBlock("No workspaces found...", "", 160)) + "</section>";

    var queueCard = '<section class="card" style="margin-top:18px"><div class="card-head"><h3>In the line</h3>' +
      '<span class="small muted">' + waiting.length + " waiting</span></div>" +
      (waiting.length ? '<ol class="small" style="margin:10px 0 0 18px">' + waiting.map(function (q) {
          return "<li>server " + q.host + " &middot; <b>" + esc(titleCase(q.stack_name || "")) + "</b> to " +
            esc(q.target_version || "") + " &middot; " +
            (q.position === 1 ? "next on that server" : "position " + q.position) +
            (q.requested_by === "customer" ? " &middot; customer request" : " &middot; your batch") + "</li>";
        }).join("") + "</ol>" : '<p class="small muted">Nothing is waiting.</p>') +
      ((d.batches || []).filter(function (b) { return b.open; }).map(function (b) {
        return '<button class="button secondary small-btn" style="margin-top:12px" data-action="updates-cancel" data-batch="' +
          esc(b.batch_id) + '">Cancel the remaining ' + b.open + " in your last batch</button>";
      }).join("")) + "</section>";

    var history = (d.history || []).slice(0, 30);
    var historyCard = '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Recent updates</h3>' +
      '<span class="small muted">newest first</span></div>' +
      (history.length ? '<ul class="small" style="margin:10px 0 0 18px">' + history.map(function (h) {
          var badge = h.status === "done" ? statusBadge("running", "Done")
            : h.status === "failed" ? statusBadge("failed", "Failed")
            : h.status === "canceled" ? statusBadge("off", "Canceled") : statusBadge("neutral", cap(h.status));
          return "<li>server " + h.host + " &middot; <b>" + esc(titleCase(h.stack_name || "")) + "</b> " +
            esc(h.from_version || "?") + " to " + esc(h.to_version || "?") + " &middot; " + badge +
            (h.error ? ' <span class="muted">' + esc(h.error.slice(0, 140)) + "</span>" : "") +
            ' <span class="muted">' + esc(fmtDate(h.finished_at || 0)) + "</span></li>";
        }).join("") + "</ul>" : '<p class="small muted">No updates yet.</p>') + "</section>";

    return appLayout("admin", "Updates", "backup",
      '<main class="page">' + head +
      ruleCard +
      '<div class="grid cols-3" style="margin-top:18px">' + hostCards + "</div>" +
      table + queueCard + historyCard +
      "</main>");
  }

  function adminBackups() {
    var backups = state.adminBackups || [];
    var acctMap = {};
    (state.adminAccounts || []).forEach(function (a) { acctMap[a.id] = a; });
    var instMap = {};
    Object.keys(state.accountCache || {}).forEach(function (id) {
      var d = state.accountCache[id];
      (d && d.instances || []).forEach(function (i) { instMap[i.id] = i; });
    });
    // All instances (from GET /admin/instances) so the admin can trigger a backup
    // right here without opening each account page.
    var allInsts = state.adminInstances || [];
    var creatable = allInsts.map(function (i) {
      var disabled = i.status !== "healthy" || i.locked;
      var name = titleCase(i.stack_name);
      var who = i.account_display || (acctMap[i.account_id] ? (acctMap[i.account_id].display_name || acctMap[i.account_id].username) : "customer #" + i.account_id);
      return '<div class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
        "<div><h3>" + esc(name) + " workspace" + updBadge(i) + updateChip(i) + "</h3><p>" + esc(who) + " &middot; " + esc(i.domain || ("env " + i.environment_name)) + (i.locked ? " &middot; switched off" : "") + "</p></div>" +
        '<div class="actions end" style="margin-left:auto;flex-wrap:wrap">' +
        '<button class="button small-btn" data-action="admin-backup-run" data-id="' + i.id + '" data-kind="full"' + (disabled ? " disabled" : "") + '>' + icon("archive") + " Full backup</button>" +
        '<button class="button secondary small-btn" data-action="admin-backup-run" data-id="' + i.id + '" data-kind="workflows"' + (disabled ? " disabled" : "") + '>Export workflows</button>' +
        (i.managed !== 0 ? '<button class="button secondary small-btn" data-action="admin-backup-run" data-id="' + i.id + '" data-kind="credentials"' + (disabled ? " disabled" : "") + '>Export credentials</button>' : "") +
        "</div></div>";
    }).join("");

    var rows = backups.map(function (b) {
      var inst = instMap[b.instance_id] || allInsts.find(function (x) { return String(x.id) === String(b.instance_id); });
      var acct = acctMap[b.account_id];
      var label = inst ? titleCase(inst.stack_name) : ("#" + b.instance_id);
      var who = acct ? (acct.display_name || acct.username) : (inst ? inst.account_display : ("account #" + b.account_id));
      var kindLabel = b.kind === "full" ? "Full workspace" : b.kind === "workflows" ? "Workflows" : "Credentials";
      var sizeTxt = b.size_bytes ? fmtBytes(b.size_bytes) : "pending";
      var status = b.status === "ready" ? statusBadge("running", "Ready")
        : b.status === "failed" ? statusBadge("failed", "Failed")
        : statusBadge("provisioning", "Creating");
      var dl = b.status === "ready"
        ? '<a class="button small-btn" href="#" data-action="backup-download" data-id="' + b.id + '" data-kind="' + esc(b.kind) + '">' + icon("download") + " Download</a>" : "";
      return '<div class="workspace-item"><span class="workspace-logo">' + icon("archive") + "</span>" +
        "<div><h3>" + esc(kindLabel) + " &middot; " + esc(label) + "</h3>" +
        "<p>Customer: " + esc(who) + " &middot; " + esc(fmtDate(b.created_at)) + " &middot; " + esc(sizeTxt) + (b.error ? " &middot; " + esc(b.error) : "") + "</p></div>" +
        status + dl + "</div>";
    }).join("");
    var historySection = '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Backup history</h3><p class="small muted" style="margin:0">Completed backups are downloadable in your browser.</p></div>' +
      (rows || (state.adminBackupsLoaded
        ? '<div class="empty-state" style="min-height:160px"><div><h3>No backups yet</h3><p class="muted">Create one above and it will appear here.</p></div></div>'
        : loadingBlock("Loading backups...", "", 160))) + "</section>";
    var triggerSection = '<section class="card"><div class="card-head"><h3>Create a backup</h3><p class="small muted" style="margin:0">Choose what to back up for each workspace.</p></div>' +
      (creatable || (state.adminInstancesLoaded
        ? '<p class="muted" style="padding:20px">There are no workspaces to back up right now.</p>'
        : loadingBlock("Loading workspaces...", "", 160))) + "</section>";

    // Only load the instance list once per visit (avoid re-fetch churn).
    if (!state.adminInstancesLoaded) {
      api("/admin/instances").then(function (d) {
        state.adminInstances = (d && d.instances) || [];
        state.adminInstancesLoaded = true;
        if (!modalRoot.children.length) render();
      }).catch(function () { state.adminInstances = state.adminInstances || []; });
    }

    return appLayout("admin", "Backups", "backup",
      '<main class="page">' + pageHead("Backups", "Create a full backup or a workflow/credential export for any workspace, or download an existing one.") +
      triggerSection + historySection + "</main>");
  }

  function adminAccountPage(id) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(id); });
    var detail = state.accountCache[id];
    // Fresh page load: the accounts list is still being fetched. Show a loading
    // state, never the error screen (a blank list on boot is NOT "removed" —
    // it produced a full second of "Account not found" on every refresh).
    if (!a && !state.adminAccountsLoaded) {
      return appLayout("admin", "Account details", "accounts",
        '<main class="page"><div class="center-state"><div class="spinner"></div>' +
        '<p class="muted">Loading account...</p></div></main>');
    }
    if (!a) return appLayout("admin", "Account details", "accounts", "<main class=\"page\">" + pageHead("Account not found", "This account may have been removed.") + "</main>");
    var instances = (detail && detail.instances) || [];
    // The workspace list arrives in a SECOND request (the per-account read).
    // Until it lands, the card must not claim the customer has no workspace:
    // that empty state flashed at admins looking at customers who DO have one
    // (Steward's report, 2026-10-06). `accountDetailTried` keeps a FAILED read
    // on the old behaviour instead of loading forever.
    var wsKnown = !!(detail && detail.instances) || !!(state.accountDetailTried || {})[id];
    var sub = a.subscription_status || "none";
    var subBadge = sub === "active" ? statusBadge("active", "Active")
      : sub === "past_due" ? statusBadge("pastdue", "Past due")
      : sub === "locked" ? statusBadge("locked", "Locked")
      : sub === "canceled" ? statusBadge("cancelled", "Cancelled") : statusBadge("pending", "No subscription");
    var acctState = a.account_state || "active";
    var stateBadge = acctState === "suspended" ? statusBadge("suspended", "Suspended")
      : acctState === "archived" ? statusBadge("archived", "Archived") : "";
    // Suspended/archived admins see a restore path, not workspace toggles.
    var lifecycleButtons = "";
    if (acctState === "active") {
      lifecycleButtons =
        '<button class="button secondary" data-action="admin-suspend" data-id="' + a.id + '">' + icon("pause") + " Suspend account</button>" +
        '<button class="button secondary" data-action="admin-archive" data-id="' + a.id + '">' + icon("archive") + " Archive</button>";
    } else if (acctState === "suspended") {
      lifecycleButtons =
        '<button class="button" data-action="admin-unsuspend" data-id="' + a.id + '">' + icon("check") + " Unsuspend</button>" +
        '<button class="button secondary" data-action="admin-archive" data-id="' + a.id + '">' + icon("archive") + " Archive</button>";
    } else {
      lifecycleButtons =
        '<button class="button" data-action="admin-restore" data-id="' + a.id + '">' + icon("check") + " Restore from archive</button>";
    }
    var used = instances.filter(function (i) { return i.status !== "deleted"; }).length;
    var quota = a.quota || 1;

    return appLayout("admin", "Account details", "accounts",
      '<main class="page">' + pageHead(a.display_name || cap(a.username), esc(a.email) + " · " + esc(a.username), '<div class="actions grid-actions">' +
        lifecycleButtons +
        (acctState === "active" ? '<button class="button" data-action="admin-impersonate" data-id="' + a.id + '">' + icon("eye") + " Login as user</button> " +
        '<button class="button secondary" data-action="admin-mark-paid" data-id="' + a.id + '">' + icon("check") + " Mark paid</button>" +
        '<button class="button secondary" data-action="admin-extend-expiry" data-id="' + a.id + '">' + icon("clock") + " Extend expiry</button>" +
        '<button class="button secondary" data-action="admin-attach" data-id="' + a.id + '">' + icon("workspace") + " Attach workspace</button>" +
        '<button class="button secondary" data-action="quota" data-id="' + a.id + '">Change quota</button>' +
        '<button class="button secondary" data-action="admin-change-plan" data-id="' + a.id + '">' + icon("billing") + " Change plan</button>" +
        '<button class="button secondary" data-action="admin-backup-toggle" data-id="' + a.id + '" data-on="' + (a.backup_enabled ? "1" : "0") + '">' + icon("archive") + (a.backup_enabled ? " Revoke backup access" : " Allow backup") + "</button>" +
        (sub === "locked" ? '<button class="button" data-action="admin-unlock" data-id="' + a.id + '">' + icon("check") + " Switch workspace on</button>"
                          : '<button class="button secondary" data-action="admin-lock" data-id="' + a.id + '">' + icon("warning") + " Switch workspace off</button>") : "") +
        "</div>", "wrap") +
      '<div class="grid cols-3">' +
        '<section class="card"><div class="card-head"><h2>Customer profile</h2><span class="badge-stack">' + subBadge + stateBadge + "</span></div>" +
          '<dl class="info-list"><div class="info-row"><dt>Username</dt><dd>' + esc(a.username) + "</dd></div>" +
          '<div class="info-row"><dt>Email</dt><dd>' + esc(a.email) + "</dd></div>" +
          '<div class="info-row"><dt>Signed up</dt><dd>' + esc(fmtDate(a.created_at)) + "</dd></div></dl></section>" +
        '<section class="card"><h2>Subscription</h2><dl class="info-list">' +
          '<div class="info-row"><dt>Plan</dt><dd>' + esc(planName(a)) + "</dd></div>" +
          '<div class="info-row"><dt>Status</dt><dd>' + esc(subLabel(sub)) + "</dd></div>" +
          '<div class="info-row"><dt>Period start</dt><dd>' + esc(fmtDate(a.paid_from)) + "</dd></div>" +
          '<div class="info-row"><dt>Period end</dt><dd>' + esc(fmtDate(a.paid_until)) + "</dd></div>" +
          '<div class="info-row"><dt>Renewal</dt><dd>Automatic</dd></div></dl></section>' +
        '<section class="card"><div class="card-head"><h2>Quota</h2><strong>' + (wsKnown ? used + " / " + quota : "Checking...") + "</strong></div>" +
          (wsKnown ? '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div></div>' : "") +
          '<p class="small muted" style="margin-top:14px">Package allowances: ' + (a.quota_a || 1) + " × Workspace (GHS 300), " + (a.quota_b || 1) + " × Workspace Plus (GHS 500). Each workspace counts against its own package.</p>" +
          (wsKnown ? '<p class="small muted" style="margin-top:8px">' + (used < quota ? "The customer may provision another workspace." : "Quota fully used.") + "</p>" : "") +
          '<button class="button secondary small-btn" data-action="quota" data-id="' + a.id + '">Change allowance</button></section>' +
      "</div>" +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h2>Backup access</h2>' + (a.backup_enabled ? statusBadge("active", "Enabled for customer") : statusBadge("off", "Disabled")) + "</div>" +
        '<p class="small muted" style="margin:0">' + (a.backup_enabled
          ? "This customer can create and download backups from their own portal. You can revoke this at any time."
          : "This customer does not see a backup option. Only the admin can back up this workspace until you enable it.") + "</p>" +
        '<button class="button secondary small-btn" data-action="admin-backup-toggle" data-id="' + a.id + '" data-on="' + (a.backup_enabled ? "1" : "0") + '" style="margin-top:14px">' + (a.backup_enabled ? " Revoke backup access" : " Allow customer to back up") + "</button></section>" +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h2>Update access</h2>' + (a.update_enabled ? statusBadge("active", "Enabled for customer") : statusBadge("off", "Disabled")) + "</div>" +
        '<p class="small muted" style="margin:0">' + (a.update_enabled
          ? "This customer can update their own workspaces from their portal. Updates are paced on the server, one workspace at a time per server, and you can revoke this at any time."
          : "This customer does not see an update option. Only you can update this customer's workspaces until you enable it.") + "</p>" +
        '<button class="button secondary small-btn" data-action="admin-update-toggle" data-id="' + a.id + '" data-on="' + (a.update_enabled ? "1" : "0") + '" style="margin-top:14px">' + (a.update_enabled ? " Revoke update access" : " Allow customer to update") + "</button></section>" +
      '<div class="grid cols-3" style="margin-top:18px"><section class="card span-2"><div class="card-head"><h2>Workspaces</h2>' +
        (used < quota ? '<button class="button secondary small-btn" data-action="admin-add-workspace" data-id="' + a.id + '">Add workspace</button>' : "") + "</div>" +
        '<div class="workspace-list">' +
        (instances.length ? instances.filter(function (i) { return i.status !== "deleted"; }).map(function (i) {
          var badge = i.locked ? statusBadge("off", "Switched off")
            : i.status === "healthy" ? statusBadge("running", "Running")
            : i.status === "provisioning" ? statusBadge("provisioning", "Being set up")
            : i.status === "failed" ? statusBadge("failed", "Setup failed") : statusBadge("neutral", cap(i.status));
          var attachedTag = i.managed === 0 ? ' <span class=\"status info\">Attached</span>' : "";
          var planTag = i.plan === "b" ? ' <span class=\"status info\">Plus</span>' : ' <span class=\"status neutral\">GHS 300</span>';
          return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
            "<div><h3>" + esc(titleCase(i.stack_name)) + " workspace" + attachedTag + planTag + updBadge(i) + updateChip(i) + runnerBadge(i) + "</h3><p>" + esc(i.domain) + "<br>Env " + esc(i.environment_name || i.environment_id) + " · Port " + esc(i.port) + (i.managed === 0 ? " · password unchanged" : "") + "</p></div>" +
            badge + '<button class="button secondary small-btn" data-action="workspace-actions" data-id="' + i.id + '" data-name="' + esc(i.stack_name) + '">Actions</button>' +
              '<div class="wi-actions">' +
              '<button class="button secondary small-btn" data-action="admin-backup" data-id="' + i.id + '">' + icon("archive") + " Back up</button>" +
              '<button class="button secondary small-btn" data-action="admin-update-image" data-id="' + i.id + '"' + updDisabled(i) + '>' + icon("refresh") + (state.instUpdating[i.id] ? " Updating..." : " Update n8n") + "</button>" +
              '<button class="button secondary small-btn" data-action="admin-restart" data-id="' + i.id + '"' + (state.instRestarting[i.id] ? " disabled" : "") + '>' + icon("refresh") + (state.instRestarting[i.id] ? " Restarting..." : " Restart") + "</button>" +
              '<button class="button secondary small-btn" data-action="admin-switch-plan" data-id="' + i.id + '" data-plan="' + (i.plan === "b" ? "a" : "b") + '">' + icon(i.plan === "b" ? "arrow" : "spark") + (i.plan === "b" ? " Downgrade to standard" : " Upgrade to Plus") + "</button>" +
              '<button class="button secondary small-btn" data-action="runner-toggle" data-id="' + i.id + '"' + runDisabled(i) + '>' + icon(i.python_runner ? "close" : "spark") + (state.runnerBusy[i.id] ? (i.python_runner ? " Removing..." : " Attaching...") : (i.python_runner ? " Remove Python" : " Attach Python")) + "</button>" +
              '<button class="button secondary small-btn" data-action="sandbox-toggle" data-id="' + i.id + '"' + sandboxDisabled(i) + '>' + icon(i.ai_sandbox ? "close" : "spark") + (state.sandboxBusy[i.id] ? (i.ai_sandbox ? " Removing..." : " Attaching...") : (i.ai_sandbox ? " Remove AI sandbox" : " Attach AI sandbox")) + "</button>" +
              "</div></article>";
        }).join("") : (wsKnown
              ? '<div class="empty-state" style="min-height:200px"><div><h3>No workspaces yet</h3><p class="muted">The customer has not provisioned a workspace.</p></div></div>'
              : '<div class="empty-state" style="min-height:200px" role="status" aria-live="polite"><div><h3>Loading workspaces...</h3><p class="muted">Checking what workspaces this account has.</p></div></div>')) +
        "</div></section>" +
        '<section class="card"><h2>Subscription timeline</h2><div class="timeline">' +
          '<div class="timeline-item"><h3>Account created</h3><p>' + esc(fmtDate(a.created_at)) + "</p></div>" +
          (a.provisioned_at ? '<div class="timeline-item"><h3>Workspace provisioned</h3><p>' + esc(fmtDate(a.provisioned_at)) + "</p></div>" : "") +
          (a.paid_from ? '<div class="timeline-item"><h3>Subscription started</h3><p>' + esc(fmtDate(a.paid_from)) + "</p></div>" : "") +
          '<div class="timeline-item"><h3>Period end</h3><p>' + esc(fmtDate(a.paid_until)) + "</p></div>" +
        "</div></section></div></main>");
  }

  function maintenancePage() {
    return appLayout("admin", "Billing maintenance", "maintenance",
      '<main class="page">' + pageHead("Billing maintenance", "Find subscriptions whose paid period ended and safely switch off their workspaces.") +
      '<div class="grid cols-3"><section class="card span-2">' +
        '<div class="hero-status warning"><span class="big-icon">' + icon("maintenance") + "</span><div><h2>Expiry maintenance sweep</h2><p>This explicit operation checks every account against its paid period.</p></div></div>" +
        '<div class="consequence" style="background:var(--amber-100)"><strong>This operation will:</strong><ul><li>Find every subscription whose paid period has ended</li><li>Switch off its running workspaces</li><li>Leave already-off workspaces safely off</li><li>Leave active accounts untouched</li></ul></div>' +
        '<button class="button warning" data-action="run-maintenance">Run expiry maintenance</button>' +
      "</section>" +
      '<aside class="card"><h2>Last result</h2><div class="banner success" style="margin-bottom:16px">' + icon("check") +
        "<div><strong>Safe to run again</strong><br><span class=\"small\">The sweep is designed to be repeatable.</span></div></div>" +
        '<p class="small muted" id="maintenance-last">Run the sweep to check for expired subscriptions. Accounts switched off can be restored by unlocking or a successful renewal.</p></aside>' +
      "</div></main>");
  }


  function envLabel(e) {
    if (!e) return "Environment";
    return String(e.display_name || ("n8n Server " + e.display_no) || e.name || e.endpoint_id);
  }
  function envServerLabel(e) {
    if (!e) return "";
    return envLabel(e) + " - " + (e.name || "") + (e.ip ? " (" + e.ip + ")" : "");
  }
  function fmtBytes(n) {
    if (!n && n !== 0) return "-";
    var b = Number(n);
    if (b >= 1073741824) return (b / 1073741824).toFixed(1) + " GB";
    if (b >= 1048576) return (b / 1048576).toFixed(0) + " MB";
    if (b >= 1024) return (b / 1024).toFixed(0) + " KB";
    return b + " B";
  }
  function envHealthCards() {
    var envs = state.adminEnvs || [];
    // Was silently absent until the list arrived; now it says it is loading (0.1.84).
    if (!envs.length && !state.adminEnvsLoaded) {
      return '<section class="card flush" style="margin-top:18px"><div class="card-head" style="padding:20px 20px 0"><h2>Server environments</h2></div>' +
        loadingBlock("Loading servers...", "Asking each n8n server for its state.", 160) + "</section>";
    }
    if (!envs.length) return "";
    return '<section class="card flush" style="margin-top:18px"><div class="card-head" style="padding:20px 20px 0"><h2>Server environments</h2><a class="button ghost small-btn" href="#/admin/settings">Manage</a></div>' +
      '<div class="grid cols-3" style="padding:14px 20px 20px">' +
      envs.map(function (e) {
        var dot = e.reachable ? statusBadge("healthy", "Reachable") : statusBadge("failed", "Unreachable");
        return '<article class="env-card"><div class="env-card-head"><span class="env-badge">' + esc(e.display_no || "") + "</span>" +
          "<div><strong>" + esc(e.name || "Server") + '</strong><div class="small muted">' + esc(e.ip || "") + "</div></div>" +
          dot + "</div>" +
          '<div class="env-stats">' +
          statCell("Running", String(e.running_n8n)) +
          statCell("Storage", fmtBytes(e.storage_bytes)) +
          statCell("Linked", String(e.linked_accounts)) +
          statCell("Unlinked", String(e.unlinked_stacks)) +
          "</div></article>";
      }).join("") +
      "</div></section>";
  }
  function statCell(label, value) {
    return '<div class="env-stat"><strong>' + esc(value) + '</strong><span class="small muted">' + esc(label) + "</span></div>";
  }

  function settingsPage() {
    var envs = state.adminEnvs && state.adminEnvs.length ? state.adminEnvs : [];
    var order = state.envOrder.length ? state.envOrder : [];
    var single = order.length === 1;
    var rows = envs.map(function (e) {
      var id = String(e.endpoint_id);
      var checked = order.indexOf(id) !== -1 ? " checked" : "";
      var dot = e.reachable ? "Reachable" : "Unreachable";
      var dotCls = e.reachable ? "success" : "warning";
      return '<label class="env-check"><input type="checkbox" value="' + esc(id) + '" data-env-check' + checked + '>' +
        '<span class="check-ui"></span><span class="env-badge">' + esc(e.display_no || "") + "</span>" +
        "<span><strong>" + esc(e.name || "Server") + '</strong><span class="small muted">' + esc(e.ip || "") +
        ' - <span class="status ' + dotCls + '">' + dot + "</span></span></span>" +
        '<span class="small muted">' + e.running_n8n + " running, " + esc(fmtBytes(e.storage_bytes)) + "</span></label>";
    }).join("") || (state.adminEnvsLoaded
      ? '<div class="empty-state" style="min-height:160px"><div><h3>No environments found</h3><p class="muted">The n8n servers could not be listed right now.</p></div></div>'
      : loadingBlock("Loading servers...", "Asking each n8n server for its state.", 160));
    return appLayout("admin", "Settings", "settings",
      '<main class="page">' + pageHead("Portal settings", "Choose where new customer workspaces are created and whether customers can pay.", '<button class="button" data-action="save-servers">' + icon("check") + " Save selection</button>") +
      paymentsCard() +
      '<div class="grid cols-2" style="margin-bottom:18px">' + adminProfileCard() + mnotifyCard() + "</div>" +
      '<div class="grid cols-3"><section class="card span-2"><div class="card-head"><div><h2>Available servers</h2><p class="muted small" style="margin:4px 0 0">Only the n8n servers are listed here. The local control host is never used for customer workspaces.</p></div></div>' +
      '<div class="server-list env-check-list">' + rows + "</div></section>" +
      '<aside class="card"><h2>Placement rule</h2>' +
      '<div class="banner" style="margin-bottom:14px">' + icon("server") +
      '<div><strong>One server selected</strong><br><span class="small">Every new workspace goes to that server. It is the source of truth.</span></div></div>' +
      '<div class="banner">' + icon("spark") +
      '<div><strong>Several servers selected</strong><br><span class="small">The system auto-selects the least loaded healthy server for each new workspace.</span></div></div>' +
      '<p class="small muted" style="margin-top:18px">Saved ids: <strong>' + esc(order.join(", ") || "(none)") + "</strong></p>" +
      '<p class="small muted">Running counts and storage refresh each time this page loads.</p></aside></div></main>');
  }

  /* Payments master switch card (Steward 2026-09-03): hold/open customer
   * payments from the admin Settings page while onboarding users who must
   * not pay. Enforced server-side at checkout; existing subscriptions and
   * free renewals (extend expiry) are unaffected. */
  function paymentsCard() {
    var open = state.adminPaymentsOpen !== false;
    return '<section class="card" style="margin-bottom:18px"><div class="card-head">' +
      '<div><h2>Payments &amp; subscriptions</h2><p class="muted small" style="margin:4px 0 0">Master switch for customer subscriptions. Use it while you onboard users who do not pay.</p></div>' +
      '<span class="badge-stack">' + (open ? statusBadge("active", "Accepting payments") : statusBadge("suspended", "Payments on hold")) + '</span></div>' +
      (open
        ? '<div class="banner" style="margin:14px 0">' + icon("check") +
          '<div><strong>Customers can subscribe and renew normally</strong><br><span class="small">Switch this off while you bring on board users who must not pay. While it is off, nobody can start a checkout; existing subscriptions keep running and you can still grant free access per account.</span></div></div>' +
          '<div class="actions end"><button class="button warning" data-action="payments-toggle" data-open="0">' + icon("pause") + " Hold payments</button></div>"
        : '<div class="banner warning" style="margin:14px 0">' + icon("lock") +
          '<div><strong>No customer can pay or subscribe right now</strong><br><span class="small">New checkouts are refused. Give each onboarded user free access with <strong>Extend expiry</strong> on their account (works while payments are held), then open payments again when paid onboarding starts.</span></div></div>' +
          '<div class="actions end"><button class="button" data-action="payments-toggle" data-open="1">' + icon("check") + " Open payments</button></div>") +
      "</section>";
  }

  /* Administrator profile (0.1.90): the address and the phone the admin's own sign-in
   * codes go to. Before this the email was hardcoded in the backend. */
  function adminProfileCard() {
    var s = state.adminSettings || {};
    return '<section class="card"><div class="card-head">' +
      '<div><h2>Administrator profile</h2><p class="muted small" style="margin:4px 0 0">' +
      "Both are used by your second factor at sign-in.</p></div></div>" +
      '<div class="field"><label for="admin-profile-email">Email address (email codes)</label>' +
      '<input id="admin-profile-email" type="email" autocomplete="email" placeholder="you@example.com" value="' +
      esc(s.admin_email || "") + '"></div>' +
      '<div class="field"><label for="admin-profile-phone">Phone number (SMS codes)</label>' +
      '<input id="admin-profile-phone" type="tel" autocomplete="tel" placeholder="024 123 4567" value="' +
      esc(s.admin_phone_pretty || s.admin_phone || "") + '">' +
      '<p class="small muted" style="margin:6px 0 0">Any Ghana format: 024 123 4567, ' +
      "+233 24 123 4567 or 233241234567. Saved as 0241234567.</p></div>" +
      '<div class="actions end"><button class="button" data-action="save-admin-profile">' +
      icon("check") + " Save profile</button></div></section>";
  }

  /* mNotify SMS delivery (0.1.90). The API key is write-only: it goes up, and only
   * "saved ... last four" ever comes back, so a screenshot of this page leaks nothing. */
  /* The verdict on the last test send, read from mNotify's own delivery report (0.1.92).
   * "Accepted by the API" is not delivery: a rejected message still costs a credit. */
  function testOutcome() {
    var t = state.smsTest;
    if (!t) return "";
    var map = {
      DELIVERED: ["", "check", "Delivered", "The handset acknowledged it."],
      SUBMITTED: [" warning", "bell", "Submitted", "Handed to the carrier, not acknowledged yet. Give it a minute."],
      REJECTED: [" warning", "error", "Rejected", "The gateway dropped it. Check the Sender ID's registered purpose against the routing mode above."],
    };
    if (t.checking) {
      return '<p class="small muted" style="margin:0 0 12px">Test message sent. Checking delivery' +
        " with mNotify" + " ..." + "</p>";
    }
    var m = map[t.status] || ["", "bell", t.status || "No report yet", t.reason || "mNotify has no verdict yet."];
    return '<div class="banner' + m[0] + '" style="margin:12px 0">' + icon(m[1]) +
      "<div><strong>Test message: " + esc(m[2]) + "</strong><br><span class=\"small\">" + esc(m[3]) + "</span></div></div>";
  }

  function mnotifyCard() {
    var s = state.adminSettings || {};
    var st = state.smsStatus || null;
    var keyLine = s.sms_api_key_set
      ? statusBadge("active", "Key saved") + ' <span class="small muted">ends ' + esc(s.sms_api_key_last4 || "") + "</span>"
      : statusBadge("pending", "No API key saved");
    var status = "";
    if (st) {
      if (st.error) {
        status = '<div class="banner warning" style="margin:12px 0">' + icon("error") +
          "<div><strong>" + esc(st.error) + '</strong><br><span class="small">Paste your mNotify API key and save it.</span></div></div>';
      } else {
        var sender = st.sender || {};
        var bal = st.balance || {};
        var mismatch = st.purpose_mismatch
          ? '<div class="banner warning" style="margin:0 0 12px">' + icon("error") +
            '<div><strong>Sender ID and routing mode disagree</strong><br><span class="small">' +
            esc(st.purpose_mismatch) + "</span></div></div>"
          : "";
        status = '<div class="banner' + (sender.approved ? "" : " warning") + '" style="margin:12px 0">' + icon(sender.approved ? "check" : "error") +
          "<div><strong>Sender ID " + esc(sender.sender || st.sender_id || "") + ": " + esc(sender.status || "unknown") + "</strong><br>" +
          '<span class="small">' + (sender.approved
            ? "Messages will go out under this name."
            : "mNotify must approve this Sender ID before texts will arrive.") + "</span></div></div>" +
          (bal.balance === null || bal.balance === undefined ? "" :
            '<p class="small muted" style="margin:0 0 12px">SMS credits left: <strong>' + esc(String(bal.balance)) + "</strong>" +
            (bal.bonus ? " plus " + esc(String(bal.bonus)) + " bonus" : "") + "</p>") +
          (st.otp_typing && bal.wallet !== null && bal.wallet !== undefined
            ? '<p class="small muted" style="margin:0 0 12px">Main wallet: <strong>' + esc(String(bal.wallet)) +
              "</strong>" + (bal.wallet > 0 ? "" : " - empty, so OTP-typed sends will be refused. Top it up or turn the OTP route off.") + "</p>"
            : "") +
          (sender.purpose ? '<p class="small muted" style="margin:0 0 12px">Registered for: ' +
            esc(sender.purpose) + "</p>" : "") + mismatch +
          (st.sender_error ? '<p class="small muted" style="margin:0 0 12px">Sender check: ' + esc(st.sender_error) + "</p>" : "") +
          (st.balance_error ? '<p class="small muted" style="margin:0 0 12px">Balance check: ' + esc(st.balance_error) + "</p>" : "");
      }
    }
    return '<section class="card"><div class="card-head">' +
      '<div><h2>Text messages (mNotify)</h2><p class="muted small" style="margin:4px 0 0">' +
      "Delivers your SMS sign-in codes.</p></div>" + keyLine + "</div>" +
      '<div class="field"><label for="sms-api-key">API key</label>' +
      '<input id="sms-api-key" type="password" autocomplete="new-password" placeholder="' +
      (s.sms_api_key_set ? "Saved - paste a new key to replace it" : "Paste your mNotify API key") + '">' +
      '<p class="small muted" style="margin:6px 0 0">Stored on the portal server and never shown again.</p></div>' +
      '<div class="field"><label for="sms-sender-id">Sender ID</label>' +
      '<input id="sms-sender-id" type="text" maxlength="11" value="' + esc(s.sms_sender_id || "StePro-OTP") + '">' +
      '<p class="small muted" style="margin:6px 0 0">Must be approved in mNotify, up to 11 characters. ' +
      "Letters and digits only is safest: a hyphenated name was rejected by the gateway on this account " +
      "while the same name without the hyphen delivers.</p></div>" +
      '<label class="env-check" style="margin-bottom:12px"><input type="checkbox" id="sms-otp-type"' + (s.sms_otp_type === "otp" ? " checked" : "") + ">" +
      '<span class="check-ui"></span><span><strong>Route as an OTP</strong><span class="small muted">' +
      "Off (plain) is what you chose. The OTP route bills 0.035 per campaign from your MAIN WALLET " +
      "(a separate purse from these SMS credits), so it fails if that wallet is empty.</span></span></label>" +
      status + testOutcome() +
      '<div class="actions end"><button class="button secondary" data-action="sms-status">' + icon("bell") + " Check connection</button>" +
      '<button class="button secondary" data-action="sms-test">' + icon("phone") + " Send test SMS</button>" +
      '<button class="button" data-action="save-sms">' + icon("check") + " Save SMS settings</button></div></section>";
  }

  /* ================= router ================= */
  function render() {
    if (!state.busyModal) modalRoot.innerHTML = "";
    var route = (location.hash || "#/entry").slice(1).split("?")[0];
    var parts = route.split("/").filter(Boolean);
    var html = null;

    // While impersonating a customer, force the customer view: the admin panel,
    // sign-in and entry routes stay locked until impersonation is exited.
    if (state.impersonation && parts[0] !== "customer") {
      navigate("/customer/dashboard");
      return;
    }

    /* ---- public / auth pages ---- */
    if (route === "/entry") html = entryPage();
    else if (route === "/request") html = requestPage();
    else if (route === "/denied") html = deniedPage();
    else if (route === "/code") html = codePage();
    else if (route === "/register") html = registerPage();
    else if (route === "/signin") html = signinPage();
    else if (route === "/forgot") html = forgotPage();
    else if (route === "/reset") html = resetPage();
    else if (route === "/mfa") html = mfaPage();

    /* ---- customer app ---- */
    else if (route === "/customer/dashboard") html = requireCustomer(function () { return customerDashboard(); });
    else if (route === "/customer/workspaces") html = requireCustomer(function () { return customerWorkspaces(); });
    else if (route === "/customer/backups") html = requireCustomer(function () {
      var enabled = !!(state.session && state.session.account && state.session.account.backup_enabled);
      if (!enabled) { navigate("/customer/dashboard"); return ""; }
      return customerBackups();
    });
    else if (route === "/customer/billing") html = requireCustomer(function () { return customerBilling(); });
    else if (route === "/customer/support") html = requireCustomer(function () { return supportPage(); });
    else if (route === "/customer/account") html = requireCustomer(function () { return accountPage(); });
    else if (route === "/customer/security") html = requireCustomer(function () { return securityPage(); });

    /* ---- admin app ---- */
    else if (route === "/admin/signin") html = state.adminAuthed ? appLayout("admin", "Overview", "overview", '<main class="page"><p class="muted">Signed in.</p></main>') : adminSignin();
    else if (route === "/admin/mfa") html = state.adminAuthed ? appLayout("admin", "Overview", "overview", '<main class="page"><p class="muted">Signed in.</p></main>') : adminMfaPage();
    else if (route === "/admin/overview") html = requireAdmin(function () { return adminOverview(); });
    else if (route === "/admin/requests") html = requireAdmin(function () { return adminRequests(); });
    else if (route === "/admin/accounts") html = requireAdmin(function () { return adminAccounts(); });
    else if (route === "/admin/backups") html = requireAdmin(function () { return adminBackups(); });
    else if (route === "/admin/updates") html = requireAdmin(function () { return adminUpdates(); });
    else if (route === "/admin/archived") html = requireAdmin(function () { return archivedAccounts(); });
    else if (parts[0] === "admin" && parts[1] === "account") html = requireAdmin(function () { return adminAccountPage(parts[2]); });
    else if (route === "/admin/maintenance") html = requireAdmin(function () { return maintenancePage(); });
    else if (route === "/admin/settings") html = requireAdmin(function () { return settingsPage(); });
    else if (route === "/admin/security") html = requireAdmin(function () { return securityPage(true); });
    else html = entryPage();

    appEl.innerHTML = html;
    var h1 = $("h1");
    document.title = (h1 ? h1.textContent : "Portal") + " | SteProTECH";
    window.scrollTo(0, 0);
    afterRender(route);
  }

  function requireCustomer(fn) {
    if (!localStorage.getItem("portal_token")) { navigate("/entry"); return ""; }
    // First paint of a signed-in customer happens BEFORE /me answers, so every
    // page used to render its zero/empty state as if it were fact: "No
    // workspaces yet", "0 of 1 used", "Subscribe and provision..." for a paying
    // customer (Steward's flash-of-misinformation report, 2026-10-07). Hold the
    // page on an announced loading state until the session is known - or known to
    // have failed (sessionLoaded is set on the failure path too).
    if (!state.sessionLoaded) {
      return appLayout("customer", "Loading", "workspace",
        '<main class="page"><div class="center-state" style="min-height:60vh" role="status" aria-live="polite">' +
        '<p class="muted">Loading your account...</p></div></main>');
    }
    return fn();
  }
  function requireAdmin(fn) {
    if (!state.adminAuthed) { navigate("/admin/signin"); return ""; }
    return fn();
  }

  function afterRender(route) {
    var parts = route.split("/").filter(Boolean);
    var sigBefore = dataSig();
    var done = false;
    function after() {
      if (done) return;
      done = true;
      if (!routeChanged(route) && dataSig() !== sigBefore && !modalRoot.children.length) render();
    }
    if (parts[0] === "customer") { refreshSession().then(after); ensureMyUpdates(); }
    else if (parts[0] === "admin" && route !== "/admin/signin") {
      refreshAdminData().then(after);
      if (route === "/admin/updates") {
        ensureAdminUpdates();
        // the queue moves in the background, so keep this page honest while it is open
        if (!state.updatesPoll) {
          state.updatesPoll = setInterval(function () {
            if ((location.hash || "").slice(1) === "/admin/updates") ensureAdminUpdates(true);
          }, 15000);
        }
      } else if (state.updatesPoll) { clearInterval(state.updatesPoll); state.updatesPoll = null; }
    }
    // security (2FA) setup state: /me/security or /admin/security. Set the state
    // and let dataSig detect the change to re-render ONCE (calling render() here
    // directly caused an infinite render<->fetch loop that prevented the modal
    // from opening on the live site — 2026-09-02).
    if (route === "/customer/security" || route === "/admin/security") {
      var secPath = route.indexOf("admin") === 1 ? "/admin/security" : "/me/security";
      api(secPath).then(function (d) {
        state.security2fa = d;
        if (!modalRoot.children.length && dataSig() !== sigBefore) render();
      }).catch(function () { state.security2fa = null; });
    }
    // note: archivedAccounts() is dispatched in render() via route mapping below
    // username preview on register
    if (route === "/register") {
      var uInput = $("input[name=username]");
      var prev = $("#username-preview");
      function upd() {
        var v = (uInput && uInput.value ? uInput.value : (state.gateEmail || "").split("@")[0] || "")
          .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
        if (prev) prev.textContent = (v || "your-username") + "." + (window.__baseDomain || "steprotech.com");
      }
      if (uInput) { uInput.addEventListener("input", upd); upd(); }
    }
  }

  function routeChanged(route) { return (location.hash || "#/entry").slice(1) !== route; }

  /* ================= data loading ================= */

  /* Data refresh with signature-based re-render:
   * render() -> afterRender() -> refresh() -> re-render ONLY when the data
   * actually changed. After the re-render the signature is equal, so the
   * chain terminates (no infinite refresh loop). */
  function dataSig() {
    var s = state.session;
    var c = s ? JSON.stringify([s.account, s.instances]) : "";
    var a = state.adminAccounts ? JSON.stringify(state.adminAccounts) : "";
    var ar = state.adminArchived ? JSON.stringify(state.adminArchived) : "";
    var r = state.requests ? JSON.stringify(state.requests) : "";
    var e = state.envOrder.join(",");
    var p = state.plans ? JSON.stringify(state.plans) : "";
    var ev = state.adminEnvs ? JSON.stringify(state.adminEnvs) : "";
    var ul = state.unlinkedStacks ? JSON.stringify(state.unlinkedStacks) : "";
    var sec = state.security2fa ? JSON.stringify(state.security2fa) : "";
    var bk = state.backups ? JSON.stringify(state.backups) : "";
    var abk = state.adminBackups ? JSON.stringify(state.adminBackups) : "";
    var mu = state.myUpdates ? JSON.stringify(state.myUpdates) : "";
    var au = state.adminUpdates ? JSON.stringify(state.adminUpdates) : "";
    var route = (location.hash || "").slice(1);
    var m = route.match(/^\/admin\/account\/(\d+)$/);
    var d = m && state.accountCache[m[1]] ? JSON.stringify(state.accountCache[m[1]]) : "";
    return c + "|" + a + "|" + ar + "|" + r + "|" + e + "|" + p + "|" + d + "|" + ev + "|" + ul + "|" + sec + "|" + bk + "|" + abk +
      "|" + mu + "|" + au +
      "|" + (state.paymentsOpen ? "po1" : "po0") + (state.adminPaymentsOpen ? "apo1" : "apo0");
  }

  async function refreshSession() {
    var token = localStorage.getItem("portal_token");
    if (!token) { state.session = null; state.sessionLoaded = true; return null; }
    try {
      var data = await api("/me");
      state.session = { token: token, account: data.account, instances: data.instances || [] };
      state.sessionLoaded = true;
      state.session.plan = data.plan || null;
      state.session.upgradeRequest = data.upgrade_request || null;
      if (!state.plans.length) await loadPlans();
      // lazily load backups when the customer opens the Backups page
      if ((location.hash || "").indexOf("/customer/backups") === 1 || state.backupsLoaded) {
        try {
          var bk = await api("/me/backups");
          state.backups = (bk && bk.backups) || [];
        } catch (e) { state.backups = state.backups || []; }
        // Marked on failure too: the Backups page must not sit on "Loading..."
        // forever if the read breaks (0.1.84).
        state.backupsLoaded = true;
      }
      return state.session;
    } catch (e) {
      if (e.status === 401) {
        localStorage.removeItem("portal_token");
        state.session = null;
        state.backupsLoaded = false; state.backups = [];
        showToast("Session ended", "Please sign in again.");
        navigate("/entry");
      }
      // Marked on failure too, so a broken /me can never leave the customer
      // staring at a loading screen forever (0.1.83).
      state.sessionLoaded = true;
      return null;
    }
  }

  async function refreshAdminData() {
    if (!state.adminAuthed) return;
    try {
      var accts = await api("/admin/accounts");
      state.adminAccounts = accts || [];
      state.adminAccountsLoaded = true;
      var arch = await api("/admin/accounts?include_archived=1");
      state.adminArchived = (arch || []).filter(function (a) { return a.account_state === "archived"; });
      state.adminArchivedLoaded = true;
      var reqs = await api("/admin/access-requests");
      state.requests = reqs || [];
      state.requestsLoaded = true;
      // true running versions (0.1.80): the chip must not trust the image tag
      try {
        var vers = await api("/admin/instance-versions");
        state.n8nVersions = (vers && vers.versions) || {};
      } catch (e) { state.n8nVersions = {}; }
      // account details for the account page currently open (always fresh)
      var route = (location.hash || "").slice(1);
      var m = route.match(/^\/admin\/account\/(\d+)$/);
      if (m) {
        var id = m[1];
        try {
          var detail = await api("/accounts/" + id);
          state.accountCache[id] = detail;
        } catch (e) { /* public per-account read failed */ }
        // Marked whether it succeeded or not, so a failed read can never leave
        // the Workspaces card stuck on its loading state (0.1.82).
        state.accountDetailTried[id] = true;
      }
      var settings = await api("/admin/settings");
      state.envOrder = String(settings.landing_environments || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      state.adminPaymentsOpen = settings.payments_open !== false;
      state.adminSettings = settings;      // admin profile + mNotify card (0.1.90)
      if (!state.environmentList.length) {
        try { state.environmentList = await api("/environments"); } catch (e) { state.environmentList = []; }
      }
      try { state.adminEnvs = await api("/admin/environments"); } catch (e) { state.adminEnvs = []; }
      state.adminEnvsLoaded = true;
      try {
        if ((location.hash || "").indexOf("/admin/backups") === 1 || state.adminBackupsLoaded) {
          var bk = await api("/admin/backups");
          state.adminBackups = (bk && bk.backups) || [];
        }
      } catch (e) { state.adminBackups = state.adminBackups || []; }
      if ((location.hash || "").indexOf("/admin/backups") === 1 || state.adminBackupsLoaded) state.adminBackupsLoaded = true;
    } catch (e) {
      if (e.status === 401) { localStorage.removeItem("admin_token"); state.adminAuthed = false; showToast("Admin session ended", "Please sign in again."); navigate("/admin/signin"); }
      // If the whole refresh breaks, show the lists as they are rather than leaving
      // every table on a loading state for ever (0.1.84, same rule as 0.1.83's /me).
      state.adminAccountsLoaded = true;
      state.requestsLoaded = true;
      state.adminArchivedLoaded = true;
      state.adminEnvsLoaded = true;
      state.adminBackupsLoaded = true;
    }
  }

  async function loadPlans() {
    try {
      var d = await api("/plans");
      state.plans = d.plans || [];
      state.paymentsOpen = d.payments_open !== false;
    } catch (e) { state.plans = []; }
  }

  /* ================= modals / toasts ================= */

  function closeProfileMenu() {
    var chipEl = document.querySelector(".profile-chip");
    var menuEl = document.querySelector(".chip-menu");
    if (menuEl) menuEl.classList.remove("open");
    if (chipEl) { chipEl.classList.remove("open"); chipEl.setAttribute("aria-expanded", "false"); }
  }

  function showToast(title, message) {
    var el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = '<span class="toast-icon">' + icon("check") + '</span><div><strong>' + esc(title) + "</strong><p>" + esc(message) + "</p></div>";
    toastRoot.appendChild(el);
    setTimeout(function () { el.remove(); }, 5000);
  }

  function showModal(content, large) {
    modalRoot.innerHTML = '<div class="modal-backdrop" data-action="close-modal"><section class="modal' + (large ? " large" : "") + '" role="dialog" aria-modal="true" aria-labelledby="modal-title" data-modal>' + content + "</section></div>";
    setTimeout(function () { var f = $(".modal button, .modal input"); if (f) f.focus(); }, 0);
  }
  function closeModal() { modalRoot.innerHTML = ""; }
  function modalHeader(title) {
    return '<div class="modal-head"><h2 id="modal-title">' + esc(title) + '</h2><button class="modal-close" data-action="close-modal" aria-label="Close">' + icon("close") + '</button></div>';
  }

  function paymentModal() {
    // Plan-aware banner (0.1.69): the modal must show the package the user
    // actually clicked, not the first active plan. chosenPlan is set by the
    // pay-now button (data-plan) before this modal opens.
    var plan = state.plans.find(function (p) {
      return p.active && ((chosenPlan === "b") === (p.amount_minor === 50000));
    }) || state.plans.find(function (p) { return p.active; }) || {};
    var isPlus = chosenPlan === "b";
    var pkgLine = isPlus
      ? esc(plan.name || "n8n Workspace Plus") + " - includes Python code node and AI assistant setup support"
      : esc(plan.name || "n8n Workspace");
    showModal(modalHeader("Continue to secure payment") +
      '<p class="muted">You will leave the SteProTECH portal and complete payment securely on the payment provider.</p>' +
      '<div class="banner">' + icon("shield") + "<div><strong>" + esc(fmtMinor(plan.amount_minor, plan.currency)) + " per year</strong><br><span class=\"small\">" + pkgLine + "</span></div></div>" +
      '<div class="actions end" style="margin-top:24px"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-action="payment-continue">Continue to payment ' + icon("external") + "</button></div>");
  }

  function approveModal(id) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(id); });
    if (!r) return;
    var again = (r.status === "approved" || r.status === "token_sent");
    showModal(modalHeader(again ? "Send the approval notice again?" : "Approve access request?") +
      (again ? "<p>Resend the approval email to <strong>" + esc(r.email) + "</strong>.</p>"
             : "<p>Approve <strong>" + esc(r.email) + "</strong> to open the portal for them.</p>") +
      '<div class="consequence" style="background:var(--amber-100)"><strong>What happens next</strong><ul><li>The request is marked <strong>Approved</strong></li><li>No code is generated and none is needed</li><li>They are emailed that their access is approved</li><li>They create their account and are signed in straight away</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-approve="' + id + '">' + (again ? "Resend email" : "Approve and notify") + "</button></div>");
  }


  function denyModal(id) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(id); });
    if (!r) return;
    showModal(modalHeader("Disapprove " + esc(r.email) + "?") +
      "<p>This declines the request. The person will not be able to register with this email.</p>" +
      '<div class="consequence" style="background:var(--red-100)"><strong>What happens next</strong><ul><li>The request is marked <strong>Disapproved</strong></li><li>The approval is withdrawn</li><li>The email is blocked from requesting again</li><li>Delete the request later to let them try again</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-request-deny="' + id + '">' + icon("close") + " Disapprove</button></div>");
  }

  function deleteRequestModal(id) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(id); });
    if (!r) return;
    showModal(modalHeader("Delete this request?") +
      "<p>This permanently removes the request for <strong>" + esc(r.email) + "</strong> from this list.</p>" +
      '<div class="consequence" style="background:var(--red-100)"><strong>This cannot be undone</strong><ul><li>The request row is deleted</li><li>The email is no longer tracked here</li><li>If they enter this email on the portal again, a new request is created</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-request-delete="' + id + '">' + icon("error") + " Delete</button></div>");
  }

  function approvalSuccess(data) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(data.request_id); });
    var em = (r ? r.email : "") || data.email || "";
    showModal('<div class="center-state"><div class="state-orb success">' + icon("check") + '</div><h2 id="modal-title">Access approved</h2>' +
      '<p class="muted"><strong>' + esc(em) + '</strong> can now create their account. No code is needed.</p>' +
      (data.email_sent
        ? '<div class="banner success" style="text-align:left">' + icon("check") + '<div><strong>Email sent</strong><br><span class="small">They were told their access is approved and asked to create their account.</span></div></div>'
        : '<div class="banner warning" style="text-align:left">' + icon("warning") + '<div><strong>Email not sent</strong><br><span class="small">The approval is saved, but the notice did not go out. Tell them directly that they can create their account now.</span></div></div>') +
      '<button class="button" style="margin-top:22px" data-action="close-modal">Done</button></div>');
  }


  function adminAddUserModal() {
    showModal(modalHeader("Add a user") +
      '<p class="muted">Create a portal account directly. A temporary password is generated and emailed to the person. They skip the access-request steps entirely.</p>' +
      '<form data-form="admin-add-user"><div class="grid cols-2">' +
      '<div class="field"><label>First name</label><input name="first_name" required maxlength="50"></div>' +
      '<div class="field"><label>Last name</label><input name="last_name" required maxlength="50"></div></div>' +
      '<div class="field"><label>Email</label><input name="email" type="email" required></div>' +
      '<div class="consequence safe"><strong>What happens</strong><ul><li>An account is created as unpaid / pending</li><li>A temporary portal password is generated and emailed</li><li>You then attach a workspace and mark the account paid</li></ul></div>' +
      '<div class="actions end"><button type="button" class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" type="submit">Create user</button></div></form>');
  }

  function markPaidModal(accountId) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(accountId); });
    var name = a ? (a.display_name || a.username) : "this customer";
    var anchorUsed = false;
    function d(v) {
      return v.getFullYear() + "-" + String(v.getMonth() + 1).padStart(2, "0") + "-" + String(v.getDate()).padStart(2, "0");
    }
    // Defaults: keep the recorded paid dates when renewing, else today -> +1yr.
    // When the account has no dates yet, try the NPM created_on anchor (the
    // proxy host creation is the source of truth for when the service began):
    // start = created_on, expiry = created_on + exactly one year.
    var now = new Date();
    var defFrom = a && a.paid_from ? new Date(Number(a.paid_from) * 1000) : now;
    var defUntil = a && a.paid_until ? new Date(Number(a.paid_until) * 1000)
      : new Date(now.getTime() + 365 * 86400000);

    function build() {
      var note = anchorUsed
        ? '<div class="banner" id="markpaid-anchor" style="margin:14px 0 0">' + icon("clock") + "<div><strong>Dates loaded from Nginx Proxy Manager</strong><br><span class=\"small\">The workspace proxy was created on the loaded start date, so expiry is exactly one year after it.</span></div></div>"
        : "";
      var curPlan = (a && a.plan) || "a";
      function planCard(val, title, sub) {
        var on = curPlan === val;
        return '<label class="field" style="display:flex;align-items:flex-start;gap:10px;border:1px solid ' + (on ? "var(--teal-700)" : "var(--line)") + ';border-radius:10px;padding:12px;cursor:pointer;margin:0">' +
          '<input type="radio" name="paid-plan" value="' + val + '"' + (on ? " checked" : "") + ' style="margin-top:3px">' +
          "<div><strong>" + esc(title) + "</strong><br><span class=\"small muted\">" + esc(sub) + "</span></div></label>";
      }
      showModal(modalHeader("Mark " + esc(name) + " as paid") +
        '<p class="muted">Record that this customer has paid, with the exact subscription dates. You can backdate the start when the customer already paid before being added here.</p>' +
        '<div class="field"><label>Package paid for</label><div class="grid cols-2" style="gap:10px">' +
        planCard("a", "Workspace (GHS 300)", "Standard package. Sets this account\u2019s package and its Workspace allowance.") +
        planCard("b", "Workspace Plus (GHS 500)", "Plus package with more resources for every workspace.") +
        "</div></div>" +
        '<div class="grid cols-2">' +
        '<div class="field"><label>Subscription start</label><input id="paid-from" type="date" value="' + d(defFrom) + '"></div>' +
        '<div class="field"><label>Expiry date</label><input id="paid-until" type="date" value="' + d(defUntil) + '"></div></div>' +
        note +
        '<div class="consequence safe" id="markpaid-note"><strong>What happens</strong><ul><li>Expiry in the future: account becomes active and a stopped workspace is switched on</li><li>Expiry already passed: recorded as unpaid and the workspace stays switched off</li></ul></div>' +
        '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
        '<button class="button" data-confirm-mark-paid="' + accountId + '">' + icon("check") + " Mark as paid</button></div>");
    }
    // If the account has never been marked paid, look for the NPM anchor.
    if (!(a && a.paid_until)) {
      api("/admin/accounts/" + accountId + "/subscription-anchor").then(function (res) {
        var anc = res && res.anchor;
        if (anc && anc.start && anc.expiry) {
          defFrom = new Date(anc.start + "T00:00:00");
          defUntil = new Date(anc.expiry + "T23:59:59");
          anchorUsed = true;
        }
        build();
      }).catch(function () { build(); });
    } else {
      build();
    }
  }

  /* Free renewal (Steward 2026-09-03): +N whole years from the current expiry
   * (or from today when the account has none / it is past). No payment is
   * involved - used to onboard users who must not pay and to renew select
   * users for free. Works while payments are on hold. */
  function extendExpiryModal(accountId) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(accountId); });
    var name = a ? (a.display_name || a.username) : "this customer";
    function addYears(ts, y) {
      var d = ts ? new Date(Number(ts) * 1000) : new Date();
      var base = d.getTime() > Date.now() ? d : new Date();
      return new Date(base.getTime() + y * 365 * 86400000);
    }
    function fmt(d) {
      return d.toDateString();
    }
    function target(y) { return fmt(addYears(a && a.paid_until ? a.paid_until : 0, Number(y))); }
    function build() {
      showModal(modalHeader("Extend " + esc(name) + "'s expiry for free") +
        '<p class="muted">Adds whole years to the current expiry date (or from today when there is no subscription yet). The account is marked active and a switched-off workspace is turned back on.</p>' +
        '<div class="banner" style="margin:4px 0 14px">' + icon("clock") + "<div><strong>Free renewal - no payment</strong><br><span class=\"small\">Use this to onboard users who do not pay or to renew selected customers on you.</span></div></div>" +
        '<div class="field"><label for="extend-years">Add</label><select id="extend-years">' +
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(function (y) {
          return '<option value="' + y + '"' + (y === 2 ? " selected" : "") + ">" + y + (y === 1 ? " year" : " years") + "</option>";
        }).join("") + "</select></div>" +
        '<p class="small muted" id="extend-preview">New expiry: <strong>' + esc(target(2)) + "</strong></p>" +
        '<div class="actions end" style="margin-top:18px"><button class="button secondary" data-action="close-modal">Cancel</button>' +
        '<button class="button" data-confirm-extend="' + accountId + '">' + icon("check") + " Extend expiry</button></div>");
      var sel = document.getElementById("extend-years");
      if (sel) {
        sel.addEventListener("change", function () {
          var pv = document.getElementById("extend-preview");
          if (pv) pv.innerHTML = "New expiry: <strong>" + esc(target(sel.value)) + "</strong>";
        });
      }
    }
    build();
  }

  function attachModal(accountId) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(accountId); });
    var name = a ? (a.display_name || a.username) : "this customer";
    var stacks = (state.unlinkedStacks || []);
    var byEnv = {};
    stacks.forEach(function (st) {
      (byEnv[st.environment_id] = byEnv[st.environment_id] || []).push(st);
    });
    var envNames = {};
    (state.adminEnvs || []).forEach(function (e) { envNames[e.endpoint_id] = e; });
    var options = stacks.map(function (st) {
      var env = envNames[st.environment_id];
      var label = (env ? envLabel(env) : "env-" + st.environment_id) + " - " + st.stack_name +
        (st.domain ? " (" + st.domain + ")" : "") + (st.running ? "" : " [off]");
      return '<option value="' + esc(st.stack_name) + '" data-env="' + st.environment_id + '" data-port="' + (st.port || 0) + '" data-domain="' + esc(st.domain || "") + '">' + esc(label) + "</option>";
    }).join("");
    showModal(modalHeader("Attach a workspace to " + esc(name)) +
      '<p class="muted">Link an existing n8n workspace that is not yet attached to any portal account. The workspace password stays exactly as it is.</p>' +
      (stacks.length
        ? '<div class="field"><label>Existing workspace</label><select id="attach-stack">' + options + "</select>" +
          '<span class="hint">Workspaces that are off are included. Workspaces already attached to an account are hidden.</span></div>' +
          '<div class="consequence safe"><strong>What happens</strong><ul><li>The workspace is bound to this account (running or off)</li><li>Nothing is started or stopped and the password is untouched</li><li>Mark the account paid afterwards to activate the subscription</li></ul></div>' +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-attach="' + accountId + '">Attach workspace</button></div>'
        : '<div class="empty-state" style="min-height:160px"><div><h3>No unattached workspaces</h3><p class="muted">Every known n8n workspace is already linked to an account.</p></div></div>' +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Close</button></div>'));
  }

  function workspaceActionsModal(inst) {
    var name = inst ? titleCase(inst.stack_name) : "Workspace";
    var domain = inst ? inst.domain : "";
    showModal(modalHeader("Workspace actions") +
      "<p class=\"muted\">" + esc(name) + ", " + esc(domain) + "</p>" +
      '<div class="grid">' +
      (inst && inst.locked
        ? '<button class="button secondary" data-action="unlock-workspace" data-id="' + inst.id + '">' + icon("check") + " Switch workspace on</button>"
        : '<button class="button secondary" data-action="lock-workspace" data-id="' + inst.id + '">' + icon("warning") + " Switch workspace off</button>") +
      (inst && inst.managed === 0
        ? '<button class="button secondary" data-action="workspace-detail" data-id="' + inst.id + '">' + icon("server") + " View details</button>"
        : '<button class="button secondary" data-action="reset-password" data-id="' + inst.id + '">' + icon("lock") + " Reset workspace password</button>") +
      '<button class="button secondary" data-action="admin-backup" data-id="' + inst.id + '">' + icon("archive") + " Back up</button>" +
      (inst ? '<button class="button secondary" data-action="admin-update-image" data-id="' + inst.id + '"' + updDisabled(inst) + '>' + icon("refresh") + (state.instUpdating[inst.id] ? " Updating..." : " Update n8n") + "</button>" : "") +
      (inst ? '<button class="button secondary" data-action="admin-restart" data-id="' + inst.id + '"' + (state.instRestarting[inst.id] ? " disabled" : "") + '>' + icon("refresh") + (state.instRestarting[inst.id] ? " Restarting..." : " Restart") + "</button>" : "") +
      (inst ? '<button class="button secondary" data-action="runner-toggle" data-id="' + inst.id + '"' + runDisabled(inst) + '>' + icon(inst.python_runner ? "close" : "spark") + (inst.python_runner ? " Remove Python runner" : " Attach Python runner") + "</button>" : "") +
      (inst ? '<button class="button secondary" data-action="sandbox-toggle" data-id="' + inst.id + '"' + sandboxDisabled(inst) + '>' + icon(inst.ai_sandbox ? "close" : "spark") + (inst.ai_sandbox ? " Remove AI sandbox" : " Attach AI sandbox") + "</button>" : "") +
      "</div>");
  }

  function confirmLock(instanceId) {
    showModal(modalHeader("Switch workspace off?") +
      "<p>While off, the customer and their automations will be completely unreachable.</p>" +
      '<div class="consequence"><strong>This action will:</strong><ul><li>Make the workspace address unreachable</li><li>Stop active automations and scheduled workflows</li><li>Preserve the customer data for later restoration</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-lock="' + instanceId + '">Confirm switch off</button></div>');
  }
  function confirmUnlock(instanceId) {
    showModal(modalHeader("Switch workspace on?") +
      "<p>Restore the customer workspace and automations exactly as they were.</p>" +
      '<div class="consequence safe"><strong>This action will:</strong><ul><li>Restore access to the workspace address</li><li>Restart automations and scheduled workflows</li><li>Keep all existing customer data</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-unlock="' + instanceId + '">Confirm switch on</button></div>');
  }
  function resetPasswordModal(instanceId) {
    showModal(modalHeader("Reset workspace password?") +
      "<p>The password inside the customer n8n workspace will change.</p>" +
      '<div class="consequence"><strong>Important consequence</strong><ul><li>The current n8n password stops working immediately</li><li>The new password is emailed to the customer</li><li>No password will be displayed in this portal</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-reset="' + instanceId + '">Reset and email password</button></div>');
  }
  function quotaModal(accountId) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(accountId); });
    var qa = (a && a.quota_a) || 1;
    var qb = (a && a.quota_b) || 1;
    showModal(modalHeader("Workspace allowances") +
      '<p class="muted">Set how many workspaces this customer may own, per package. A workspace counts against the allowance of its own package.</p>' +
      '<div class="grid cols-2" style="gap:12px">' +
      '<div class="field" style="border:1px solid var(--line);border-radius:10px;padding:14px;margin:0"><label>Workspace (GHS 300)</label>' +
      '<input id="quota-input-a" type="number" min="1" max="50" value="' + qa + '">' +
      '<span class="hint">Allowance for workspaces on the standard package.</span></div>' +
      '<div class="field" style="border:1px solid var(--line);border-radius:10px;padding:14px;margin:0"><label>Workspace Plus (GHS 500)</label>' +
      '<input id="quota-input-b" type="number" min="1" max="50" value="' + qb + '">' +
      '<span class="hint">Allowance for workspaces on the Plus package.</span></div></div>' +
      '<span class="hint">Raising an allowance lets the customer provision more workspaces of that package. Reducing it does not delete existing workspaces.</span>' +
      '<div class="actions end" style="margin-top:22px"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-quota="' + accountId + '">Update allowances</button></div>');
  }

  function maintenanceModal() {
    showModal(modalHeader("Run expiry maintenance?") +
      "<p>This will switch off every workspace whose subscription has ended.</p>" +
      '<div class="consequence"><strong>The sweep will:</strong><ul><li>Check every customer account</li><li>Switch off expired workspaces</li><li>Leave active accounts untouched</li><li>Safely confirm already-off workspaces</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-action="maintenance-run">Run maintenance</button></div>');
  }

  /* ---- extra workspace form (Steward 2026-09-03) ----
   * When an account is granted more quota, adding a workspace reuses the same
   * form as the first one: owner email, first/last name, a UNIQUE username
   * (which becomes the workspace address) and a password. The username is
   * checked live and the customer is alerted the instant it is not available. */
  function baseDomainFor() { return window.__baseDomain || "steprotech.com"; }

  function workspacePwError(pw) {
    if (!pw || pw.length < 8) return "Password needs at least 8 characters.";
    if (!/[A-Z]/.test(pw)) return "Password needs one uppercase letter.";
    if (!/[a-z]/.test(pw)) return "Password needs one lowercase letter.";
    if (!/[0-9]/.test(pw)) return "Password needs one number.";
    return "";
  }

  function addWorkspaceModal() {
    var acc = state.session ? state.session.account : null;
    if (!acc) return;
    var email = acc.email || "";
    showModal(modalHeader("Add another workspace") +
      '<p class="muted">Your account can own up to ' + allowanceFor(acc, state.session ? state.session.instances : []) + ' workspaces. Each workspace gets its own owner, its own password, and a unique address from the username you choose.</p>' +
      '<div class="form-grid">' +
        '<div class="field full"><label>Owner email</label>' +
        '<input id="ws-email" type="email" value="' + esc(email) + '" autocomplete="off" spellcheck="false">' +
        '<span class="hint">Login details for the new workspace are emailed to this address.</span></div>' +
        '<div class="field"><label>First name</label><input id="ws-first" type="text" maxlength="50" placeholder="Owner first name"></div>' +
        '<div class="field"><label>Last name</label><input id="ws-last" type="text" maxlength="50" placeholder="Owner last name"></div>' +
        '<div class="field full"><label>Username <span class="muted" id="ws-username-state"></span></label>' +
        '<input id="ws-username" type="text" pattern="[a-z0-9-]+" maxlength="62" placeholder="lowercase letters, numbers, hyphens" autocomplete="off" spellcheck="false" autocapitalize="none">' +
        '<span class="hint" id="ws-username-hint">Becomes your workspace address: <strong id="ws-preview"></strong></span>' +
        '<span class="uname-check" id="ws-availability" style="display:none"></span></div>' +
        '<div class="field full"><label>Password</label><div class="input-row">' +
        '<input id="ws-pass" type="password" autocomplete="new-password" minlength="8" maxlength="128">' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="ws-pass" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="field full"><label>Confirm password</label><div class="input-row">' +
        '<input id="ws-pass2" type="password" autocomplete="new-password">' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="ws-pass2" aria-label="Show password">' + icon("eye") + '</button></div></div>' +
        '<div class="checks full" id="ws-pw-msg" style="display:none"><span class="check"></span></div>' +
      '</div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-action="add-workspace-submit">' + icon("plus") + " Create workspace</button></div>");
    bindWorkspaceForm();
  }

  function bindWorkspaceForm() {
    var u = document.getElementById("ws-username");
    var stateEl = document.getElementById("ws-username-state");
    var hintEl = document.getElementById("ws-availability");
    var prevEl = document.getElementById("ws-preview");
    var okFlag = { ok: false, busy: false, value: "" };
    u.dataset.ok = "0";
    function showAvailability(res) {
      okFlag.ok = res.available;
      okFlag.busy = false;
      u.dataset.ok = res.available ? "1" : "0";
      if (!hintEl) return;
      hintEl.style.display = "";
      hintEl.className = "uname-check " + (res.available ? "ok" : "bad");
      hintEl.textContent = res.available
        ? ("Available: https://" + res.username + "." + baseDomainFor() + "/")
        : (res.message || "That username is not available.");
    }
    var timer = null;
    if (u) u.addEventListener("input", function () {
      var v = u.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
      if (v !== u.value) u.value = v;
      var preview = (v || "your-username") + "." + baseDomainFor();
      if (prevEl) prevEl.textContent = "https://" + preview + "/";
      if (stateEl) stateEl.textContent = "";
      u.dataset.ok = "0";
      if (hintEl) { hintEl.style.display = "none"; }
      clearTimeout(timer);
      if (!v) return;
      okFlag.busy = true;
      timer = setTimeout(function () {
        api("/username-available?username=" + encodeURIComponent(v)).then(showAvailability)
          .catch(function () { okFlag.busy = false; });
      }, 350);
    });
    var p1 = document.getElementById("ws-pass");
    var p2 = document.getElementById("ws-pass2");
    var msg = document.getElementById("ws-pw-msg");
    function pwUpdate() {
      if (!msg) return;
      var e1 = p1 ? workspacePwError(p1.value) : "Password is required.";
      var mismatch = p1 && p2 && p1.value !== p2.value;
      if (p1 && p1.value && !e1 && mismatch) e1 = "Passwords do not match.";
      if (!p1 || !p1.value) { msg.style.display = "none"; return; }
      msg.style.display = "";
      msg.firstChild.className = "check " + (e1 ? "bad" : "ok");
      msg.firstChild.textContent = e1 || "Password meets the requirements.";
    }
    if (p1) p1.addEventListener("input", pwUpdate);
    if (p2) p2.addEventListener("input", pwUpdate);
    if (prevEl) prevEl.textContent = "your-username." + baseDomainFor();
  }

  async function submitWorkspaceProvision() {
    var acc = state.session ? state.session.account : null;
    if (!acc) return;
    var email = String((document.getElementById("ws-email") || {}).value || "").trim();
    var first = String((document.getElementById("ws-first") || {}).value || "").trim();
    var last = String((document.getElementById("ws-last") || {}).value || "").trim();
    var username = String((document.getElementById("ws-username") || {}).value || "").trim().toLowerCase();
    var pw = (document.getElementById("ws-pass") || {}).value || "";
    var pw2 = (document.getElementById("ws-pass2") || {}).value || "";
    var uEl = document.getElementById("ws-username");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showToast("Email needed", "Enter a valid owner email for this workspace."); return; }
    if (!first || !last) { showToast("Name needed", "Enter the owner first and last name."); return; }
    if (!/^[a-z0-9]([a-z0-9-]{0,60}[a-z0-9])?$/.test(username)) { showToast("Username invalid", "Use lowercase letters, numbers and hyphens only, 2-62 characters."); return; }
    var pwErr = workspacePwError(pw);
    if (pwErr) { showToast("Password needs work", pwErr); return; }
    if (pw !== pw2) { showToast("Passwords do not match", "Confirm the password again."); return; }
    if (!uEl || uEl.dataset.ok !== "1") {
      // Immediate availability alert: always re-confirm before submitting.
      try {
        var chk = await api("/username-available?username=" + encodeURIComponent(username));
        uEl.dataset.ok = chk.available ? "1" : "0";
        if (!chk.available) {
          showToast("Username not available", chk.message || "Pick a different username.");
          return;
        }
      } catch (e) {
        showToast("Could not check username", e.message);
        return;
      }
    }
    var prevCount = (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }).length;
    var btn = document.querySelector("[data-action=add-workspace-submit]");
    if (btn) { btn.disabled = true; btn.textContent = "Creating workspace..."; }
    try {
      await api("/accounts/" + acc.id + "/provision", {
        method: "POST",
        body: { password: pw, username: username, owner_email: email,
                first_name: first, last_name: last }
      });
      closeModal();
      showToast("Workspace being created", "https://" + username + "." + baseDomainFor() + "/ will be ready in a few minutes. We will email the access details.");
      navigate("/customer/workspaces");
      startWorkspacePoll(prevCount);
    } catch (err) {
      showToast("Could not start workspace", err.message);
      if (btn) { btn.disabled = false; btn.innerHTML = icon("plus") + " Create workspace"; }
    }
  }

  function startWorkspacePoll(prevCount) {
    stopPolling();
    state.pollTimer = setInterval(function () {
      refreshSession().then(function () {
        var insts = state.session ? (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }) : [];
        var provisioning = insts.some(function (i) { return i.status === "provisioning"; });
        var failedNow = insts.length > prevCount && insts.some(function (i) { return i.status === "failed"; });
        if (insts.length > prevCount && !provisioning) {
          stopPolling();
          render();
          if (failedNow) { showToast("Workspace setup needs attention", "SteProTECH has been notified and will contact you."); }
          else { showToast("Workspace ready", "Your new workspace is up. Check your email for the access details."); }
        } else {
          render();
        }
      });
    }, 10000);
  }

  var chosenPlan = "a";  // plan picked at subscribe time: a (300) or b (500 Plus)

  /* ================= actions / forms ================= */

  async function doPayment() {
    var acc = state.session ? state.session.account : null;
    if (!acc) { navigate("/signin"); return; }
    var id = acc.id;
    try {
      var data = await api("/accounts/" + id + "/checkout", { method: "POST", body: { plan: chosenPlan || "a" } });
      if (data.gateway === "mock") {
        // E2E mock mode: simulate the charge.success webhook, exactly like the
        // backend E2E scripts do; then provision with the stored password.
        var hook = await api("/webhook/mock", { method: "POST", body: { mock: true, type: "charge.success", data: { metadata: { account_id: String(id) } } } });
        if (hook && hook.status === "active") {
          showToast("Payment successful", "Your subscription is active. Preparing your workspace...");
          closeModal();
          await refreshSession();
          stopPolling();
          await autoProvision();
          navigate("/customer/dashboard");
        } else {
          showToast("Payment not confirmed", "The payment simulation did not complete. Please try again.");
        }
      } else {
        // real gateway (paystack / stripe): redirect to hosted checkout
        window.location.href = data.url;
      }
    } catch (err) {
      showToast("Payment could not start", err.message);
    }
  }

  async function autoProvision() {
    var acc = state.session ? state.session.account : null;
    if (!acc) return;
    var insts = (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; });
    if (insts.length) { render(); return; } // already provisioned
    var pw = state.provisioningPw || localStorage.getItem("portal_pw") || "";
    if (!pw) {
      // Provisioning needs the signup password (it becomes the n8n owner
      // password). If we don't have it in this browser, ask for it.
      showModal(modalHeader("Provision your workspace") +
        '<p class="muted">Provisioning needs the password you chose at signup so your n8n owner account can be created with it.</p>' +
        '<div class="field"><label>Signup password</label><div class="input-row">' +
        '<input id="provision-pw" type="password" autocomplete="off">' +
        '<button type="button" class="input-action" data-action="toggle-password" data-target="provision-pw" aria-label="Show password">' + icon("eye") + '</button></div>' +
        '<span class="hint">The same password will be set inside your n8n workspace.</span></div>' +
        '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
        '<button class="button" data-action="provision-confirm">Provision workspace</button></div>');
      return;
    }
    try {
      await api("/accounts/" + acc.id + "/provision", { method: "POST", body: pw ? { password: pw } : {} });
      showToast("Provisioning started", "Your workspace is being prepared. We will email you when it is ready.");
    } catch (err) {
      showToast("Provisioning could not start", err.message);
    }
    startPolling();
    render();
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(function () {
      refreshSession().then(function () {
        var insts = state.session ? (state.session.instances || []) : [];
        var healthy = insts.some(function (i) { return i.status === "healthy"; });
        var failed = insts.some(function (i) { return i.status === "failed"; });
        var provisioning = insts.some(function (i) { return i.status === "provisioning"; });
        if (!provisioning) { stopPolling(); render(); if (healthy) showToast("Workspace ready", "Your workspace is up. Check your email for details."); }
        else render();
      });
    }, 10000);
  }
  function stopPolling() { if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; } }

  async function adminAction(path, successTitle, successMsg) {
    try {
      var data = await api(path, { method: "POST" });
      showToast(successTitle, successMsg || JSON.stringify(data));
      return data;
    } catch (err) {
      showToast("Action failed", err.message);
      return null;
    }
  }

  /* ---- admin impersonation (login as a customer) ---- */
  function impersonateTarget(id) {
    var all = (state.adminAccounts || []).concat(state.adminArchived || []);
    return all.find(function (x) { return String(x.id) === String(id); });
  }

  function impersonateConfirm(accountId) {
    var a = impersonateTarget(accountId);
    if (!a) { showToast("Login as user", "Account not found."); return; }
    var name = esc(a.display_name || cap(a.username));
    showModal(modalHeader("Login as " + name + "?") +
      "<p>You will see the portal exactly as this customer sees it, for troubleshooting.</p>" +
      '<div class="consequence"><strong>What you should know</strong><ul>' +
      "<li>The session lasts " + state.impTtlMinutes + " minutes and ends when you exit</li>" +
      "<li>Starting and ending the session is recorded on the security trail</li>" +
      "<li>Changes you make here apply to the customer's account, like their own session would</li>" +
      "<li>Exit returns you straight to this admin panel</li></ul></div>" +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-impersonate="' + accountId + '">' + icon("eye") + " Login as " + name + "</button></div>");
  }

  function beginImpersonation(accountId, r) {
    var a = impersonateTarget(accountId);
    var label = a ? (a.display_name || a.username || a.email)
                  : (r.account ? (r.account.display_name || r.account.username) : ("#" + accountId));
    var adminTok = localStorage.getItem("admin_token");
    // Park the admin session, then step into the customer session.
    if (adminTok) localStorage.setItem("admin_token_backup", adminTok);
    localStorage.removeItem("admin_token");
    localStorage.setItem("portal_token", r.token);
    localStorage.setItem("impersonation", JSON.stringify({ id: Number(accountId), label: String(label) }));
    state.adminAuthed = false;
    state.impersonation = { id: Number(accountId), label: String(label) };
    state.impTtlMinutes = r.expires_minutes || 60;
    state.session = null;
    state.sessionLoaded = false;
    state.backupsLoaded = false; state.backups = [];
    state.menuOpen = false;
    stopPolling();
    showToast("Viewing as " + label, "Exit impersonation from the banner at any time.");
    navigate("/customer/dashboard");
  }

  function exitImpersonation() {
    var imp = state.impersonation;
    var label = imp ? (imp.label || "customer") : "customer";
    // Best-effort audit of the exit (an already-expired token is fine to ignore).
    api("/auth/impersonate-end", { method: "POST" }).catch(function () {});
    var adminTok = localStorage.getItem("admin_token_backup");
    if (adminTok) {
      localStorage.setItem("admin_token", adminTok);
      state.adminAuthed = true;
    }
    localStorage.removeItem("admin_token_backup");
    localStorage.removeItem("portal_token");
    localStorage.removeItem("impersonation");
    state.impersonation = null;
    state.session = null;
    state.sessionLoaded = false;
    state.backupsLoaded = false; state.backups = [];
    state.provisioningPw = null;
    stopPolling();
    showToast("Returned to admin", "You are no longer viewing as " + label + ".");
    navigate("/admin/accounts");
  }

  /* ---- n8n image updates (0.1.50): always pull + latest-release aware ---- */
  var n8nReleasesCache = null;
  function fetchN8nReleases(force) {
    var now = Date.now();
    if (!force && n8nReleasesCache && now - n8nReleasesCache.at < 15 * 60 * 1000) {
      return Promise.resolve(n8nReleasesCache.data);
    }
    return api("/admin/n8n-releases").then(function (d) {
      n8nReleasesCache = { at: now, data: d };
      return d;
    }).catch(function () { return null; });
  }
  // The version a workspace really runs. The image reference is often the
  // floating "latest" tag, which is not a version: never treat that as current.
  function instVersion(inst) {
    if (!inst) return "";
    var m = (state.n8nVersions || {})[inst.id];
    if (m && m.version) return m.version;
    var tag = imageTagOf(inst.image || "");
    return semverTuple(tag) ? tag : "";
  }
  function imageTagOf(img) {
    var s = String(img || "");
    var i = s.lastIndexOf(":");
    return i >= 0 ? s.slice(i + 1) : s;
  }
  function semverTuple(t) {
    var m = String(t || "").trim().match(/^(\d+)\.(\d+)\.(\d+)/);
    return m ? [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)] : null;
  }
  function isVersionBehind(current, latest) {
    var a = semverTuple(imageTagOf(current));
    var b = semverTuple(imageTagOf(latest));
    if (!a || !b) return false;
    return a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2])));
  }
  function releaseDateLabel(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  function updateChip(inst) {
    if (!inst || !state.n8nLatestTag) return "";
    var v = instVersion(inst);
    if (!v) return "";   // unknown version: say nothing rather than claim "up to date"
    if (isVersionBehind(v, state.n8nLatestTag)) {
      return ' <span class="status warning">n8n ' + esc(state.n8nLatestTag) + " available</span>";
    }
    return "";
  }
  function updBadge(inst) {
    var t = (inst && state.instUpdating[inst.id]) ? state.instUpdating[inst.id] : "";
    return t ? ' <span class="status info">Updating to ' + esc(t) + "...</span>" : "";
  }
  function updDisabled(inst) {
    return (inst && state.instUpdating[inst.id]) ? " disabled" : "";
  }
  function runDisabled(inst) {
    // Two stack mutations must never race: block runner changes while an n8n
    // image update runs on the same workspace, and vice versa.
    return (inst && (state.runnerBusy[inst.id] || state.instUpdating[inst.id])) ? " disabled" : "";
  }
  function runnerBadge(inst) {
    return (inst && inst.python_runner && !state.runnerBusy[inst.id]) ? ' <span class="status info">Python attached</span>' : "";
  }
  function confirmAttachRunner(instanceId) {
    var inst = (state.adminInstances || []).find(function (x) { return x.id === Number(instanceId); }) ||
               findInstanceById(instanceId);
    var wname = inst ? titleCase(inst.stack_name) : "this workspace";
    showModal(modalHeader("Attach Python runner?") +
      "<p>This adds n8n\u2019s official Python helper container to " + esc(wname) + " only. No other workspace is affected.</p>" +
      '<div class="consequence"><strong>This action will:</strong><ul>' +
      "<li>Install the runner beside this workspace (official n8n image, matched to its version)</li>" +
      "<li>Restart the workspace; it is back in a few minutes and all workflows and data are preserved</li>" +
      "<li>Make Python available in the Code node, with the standard library included</li>" +
      "<li>Keep third-party Python packages (pandas, numpy and similar) unavailable for now</li>" +
      "<li>Stay removable at any time from this same menu</li></ul></div>" +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-runner-attach="' + instanceId + '">Attach Python</button></div>');
  }
  function confirmDetachRunner(instanceId) {
    var inst = (state.adminInstances || []).find(function (x) { return x.id === Number(instanceId); }) ||
               findInstanceById(instanceId);
    var wname = inst ? titleCase(inst.stack_name) : "this workspace";
    showModal(modalHeader("Remove Python runner?") +
      "<p>The Python helper container is removed from " + esc(wname) + ".</p>" +
      '<div class="consequence" style="background:var(--amber-100)"><strong>This action will:</strong><ul>' +
      "<li>Restart the workspace; data is preserved</li>" +
      "<li>Stop Python Code nodes from running; workflows that use them will fail until re-attached</li>" +
      "<li>Leave JavaScript Code nodes working exactly as before</li></ul></div>" +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-runner-detach="' + instanceId + '">Remove Python</button></div>');
  }
  function sandboxDisabled(inst) {
    // Same rule as the runner: never run two stack mutations against one workspace at once.
    return (inst && (state.sandboxBusy[inst.id] || state.runnerBusy[inst.id] ||
                     state.instUpdating[inst.id] || state.instRestarting[inst.id])) ? " disabled" : "";
  }
  function confirmAttachSandbox(instanceId) {
    var inst = (state.adminInstances || []).find(function (x) { return x.id === Number(instanceId); }) ||
               findInstanceById(instanceId);
    var wname = inst ? titleCase(inst.stack_name) : "this workspace";
    showModal(modalHeader("Attach AI sandbox?") +
      "<p>This gives " + esc(wname) + " access to the shared n8n Assistant sandbox. No other workspace is affected.</p>" +
      '<div class="consequence"><strong>This action will:</strong><ul>' +
      "<li>Create a sandbox key used only by this workspace, so removing it later cannot affect anyone else</li>" +
      "<li>Restart the workspace; it is back in a few minutes and all workflows and data are preserved</li>" +
      "<li>Turn on the Assistant sandbox and the shared web search for this workspace</li>" +
      "<li>Leave the model choice to the user, who adds their own key in the Assistant settings</li>" +
      "<li>Stay removable at any time from this same menu</li></ul></div>" +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-sandbox-attach="' + instanceId + '">Attach sandbox</button></div>');
  }
  function confirmDetachSandbox(instanceId) {
    var inst = (state.adminInstances || []).find(function (x) { return x.id === Number(instanceId); }) ||
               findInstanceById(instanceId);
    var wname = inst ? titleCase(inst.stack_name) : "this workspace";
    showModal(modalHeader("Remove AI sandbox?") +
      "<p>The shared sandbox is taken away from " + esc(wname) + " and its key is revoked.</p>" +
      '<div class="consequence" style="background:var(--amber-100)"><strong>This action will:</strong><ul>' +
      "<li>Restart the workspace; data is preserved</li>" +
      "<li>Stop the Assistant from running code until a sandbox is attached again</li>" +
      "<li>Leave every other workspace untouched</li></ul></div>" +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-confirm-sandbox-detach="' + instanceId + '">Remove sandbox</button></div>');
  }
  function runSandboxToggle(instanceId, mode) {
    var attaching = mode === "attach";
    state.sandboxBusy[instanceId] = mode;
    state.busyModal = true;
    render();
    showModal(modalHeader(attaching ? "Attaching AI sandbox" : "Removing AI sandbox") + '<div class="spinner"></div>' +
      '<p class="muted">Recreating the workspace ' + (attaching ? "with" : "without") + ' the sandbox settings. This can take a few minutes; the window updates when it finishes.</p>');
    api("/admin/instances/" + instanceId + "/ai-sandbox/" + mode, { method: "POST" }).then(function () {
      state.busyModal = false;
      delete state.sandboxBusy[instanceId];
      closeModal();
      showToast(attaching ? "AI sandbox attached" : "AI sandbox removed",
        attaching ? "The user can now add their model key in the Assistant settings and the sandbox will run their code."
                  : "This workspace no longer has a sandbox; the Assistant cannot run code until one is attached again.");
      refreshAdminData().then(function () { render(); });
    }).catch(function (err) {
      state.busyModal = false;
      delete state.sandboxBusy[instanceId];
      closeModal();
      showToast(attaching ? "Attach failed" : "Remove failed", err.message);
      refreshAdminData().then(function () { render(); });
    });
  }
  function runRunnerToggle(instanceId, mode) {
    var attaching = mode === "attach";
    state.runnerBusy[instanceId] = attaching ? "attach" : "detach";
    state.busyModal = true;
    render();
    showModal(modalHeader(attaching ? "Attaching Python runner" : "Removing Python runner") + '<div class="spinner"></div>' +
      '<p class="muted">Recreating the workspace with' + (attaching ? "" : "out") + " the Python helper container. This can take a few minutes; the window updates when it finishes.</p>");
    api("/admin/instances/" + instanceId + "/python-runner/" + mode, { method: "POST" }).then(function () {
      state.busyModal = false;
      delete state.runnerBusy[instanceId];
      closeModal();
      showToast(attaching ? "Python attached" : "Python runner removed",
        attaching ? "Python is now available in this workspace\u2019s Code node. Third-party packages stay unavailable for now."
                  : "The workspace no longer has a Python runner. Python Code nodes will fail until you attach one again.");
      refreshAdminData().then(function () { render(); });
    }).catch(function (err) {
      state.busyModal = false;
      delete state.runnerBusy[instanceId];
      closeModal();
      showToast(attaching ? "Attach failed" : "Remove failed", err.message);
      refreshAdminData().then(function () { render(); });
    });
  }
  function updateImageModal(uInst) {
    var wname = titleCase(uInst.stack_name);
    showModal(modalHeader("Update n8n for " + wname) + '<div class="spinner"></div><p class="muted">Checking the current version and the latest n8n release...</p>');
    var curP = api("/admin/instances/" + uInst.id + "/image").then(function (d) {
      return d || {};
    }).catch(function () { return {}; });
    var relP = fetchN8nReleases();
    Promise.all([curP, relP]).then(function (res) {
      var info = res[0] || {};
      var currentImage = info.image || "";
      // Prefer the version the container actually runs; fall back to an explicit
      // tag in the reference. "latest" is NOT a version, and pretending it was
      // produced the false "already on the latest release" report (0.1.80).
      var current = info.version || (semverTuple(imageTagOf(currentImage)) ? imageTagOf(currentImage) : "");
      var known = !!semverTuple(current);
      var rel = res[1];
      var latestTag = (rel && rel.latest && rel.latest.tag) || "";
      var latestIso = (rel && rel.latest && rel.latest.published_at) || "";
      var behind = !!(latestTag && known && isVersionBehind(current, latestTag));
      var onLatest = !!(latestTag && known && !isVersionBehind(current, latestTag));
      var showCurrent = !!current;
      var latestBanner = rel && rel.latest
        ? '<div class="banner' + (behind ? " success" : "") + '" style="margin-bottom:14px">' + icon("spark") +
          "<div><strong>Latest n8n release: " + esc(latestTag) + "</strong><br><span class=\"small\">" +
          (latestIso ? "Published " + esc(releaseDateLabel(latestIso)) + ". " : "") +
          (behind ? "This workspace is on " + esc(current) + " and can be updated."
                  : onLatest ? "This workspace is already on the latest release."
                             : "The current version could not be determined. You can still update to the latest release.") +
          "</span></div>" +
          (behind ? '<button class="button" data-action="update-image-now" data-id="' + uInst.id + '" data-tag="' + esc(latestTag) + '" style="margin-left:auto">' + icon("refresh") + " Update to " + esc(latestTag) + "</button>" : "") +
          "</div>"
        : '<div class="banner warning" style="margin-bottom:14px">' + icon("warning") +
          "<div><strong>Could not check for new releases</strong><br><span class=\"small\">The release list is unreachable right now. You can still update to a specific version below.</span></div></div>";
      showModal(modalHeader("Update n8n for " + wname) +
        latestBanner +
        (showCurrent ? '<div class="field"><label>Current version</label><input class="mono" value="' + esc(current) + '" readonly>' +
            (currentImage && imageTagOf(currentImage) !== current
              ? '<span class="hint">Stack image: ' + esc(currentImage) + ' (a floating tag; the version above is what the container really runs)</span>'
              : "") +
            '</div>' : "") +
        '<div class="field"><label>Update to a specific version</label><div class="input-row">' +
        '<span class="input-suffix">n8nio/n8n:</span>' +
        '<input id="img-tag" type="text" placeholder="e.g. ' + esc(latestTag || "2.31.6") + '" autocomplete="off" spellcheck="false">' +
        '</div><span class="hint">Type the exact version tag to update to (for example ' + esc(latestTag || "2.31.6") + '). Every update pulls the image from Docker Hub first, so a brand-new version works right away. Workflows and credentials are preserved.</span></div>' +
        '<div class="actions end" style="justify-content:flex-start;flex-wrap:wrap">' +
        '<button class="button" data-action="update-to-version" data-id="' + uInst.id + '">' + icon("refresh") + " Update n8n</button>" +
        (showCurrent && !behind ? '<button class="button secondary" data-action="repull-version" data-id="' + uInst.id + '" data-tag="' + esc(current) + '">Re-pull ' + esc(current) + "</button>" : "") +
        '<button class="button secondary" data-action="close-modal">Cancel</button></div>');
    });
  }
  function runImageUpdate(instanceId, tag) {
    // Paint an immediate "Updating to X..." state on the workspace rows and keep
    // the progress modal alive (render() skips wiping it while busyModal is set).
    state.instUpdating[instanceId] = tag;
    state.busyModal = true;
    render();
    showModal(modalHeader("Updating n8n workspace") + '<div class="spinner"></div>' +
      '<p class="muted">Pulling n8nio/n8n:' + esc(tag) + " and recreating the workspace. This can take a few minutes; the window updates when it finishes.</p>");
    api("/admin/instances/" + instanceId + "/update-image", { method: "POST", body: { image: tag } }).then(function (d) {
      state.busyModal = false;
      delete state.instUpdating[instanceId];
      closeModal();
      showToast("n8n updated", "The workspace is now on " + d.image + ". It may take a minute to come back online.");
      state.n8nLatestTag = "";
      refreshAdminData().then(function () { render(); });
    }).catch(function (err) {
      state.busyModal = false;
      delete state.instUpdating[instanceId];
      closeModal();
      showToast("Update failed", err.message);
      refreshAdminData().then(function () { render(); });
    });
  }

  function eventHandlers() {
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        var wasOpen = document.querySelector(".chip-menu.open");
        if (wasOpen) { e.preventDefault(); closeProfileMenu(); var ch = document.querySelector(".profile-chip"); if (ch) ch.focus(); }
        return;
      }
      var t = e.target;
      if ((e.key === "Enter" || e.key === " ") && t && t.classList && t.classList.contains("profile-chip")) {
        e.preventDefault();
        t.click();
      }
    });

    document.addEventListener("click", function (event) {
      // profile-chip menu: any click outside the chip/menu dismisses it
      if (!event.target.closest(".profile-chip, .chip-menu")) closeProfileMenu();

      var route = event.target.closest("[data-route]");
      if (route) { navigate(route.dataset.route); return; }

      var act = event.target.closest("[data-action]");
      var action = act && act.dataset.action;

      if (action === "profile-menu") {
        event.preventDefault();
        var chipEl = document.querySelector(".profile-chip");
        var menuEl = document.querySelector(".chip-menu");
        if (!menuEl) return;
        var willOpen = !menuEl.classList.contains("open");
        closeProfileMenu();
        if (willOpen && menuEl) {
          menuEl.classList.add("open");
          if (chipEl) { chipEl.classList.add("open"); chipEl.setAttribute("aria-expanded", "true"); }
          if (event.detail === 0) { // keyboard activation -> move focus into the menu
            var first = menuEl.querySelector(".chip-menu-item");
            if (first) first.focus();
          }
        }
        return;
      }

      if (action === "menu") { state.menuOpen = !state.menuOpen; render(); }
      else if (action === "side-close") { if (state.menuOpen) { state.menuOpen = false; render(); } }
      else if (action === "close-modal" &&
               (event.target === act || !act.classList.contains("modal-backdrop"))) closeModal();
      else if (action === "signout") {
        if (state.impersonation) { exitImpersonation(); }
        else { localStorage.removeItem("portal_token"); state.session = null; state.sessionLoaded = false; state.backupsLoaded = false; state.backups = []; state.provisioningPw = null; stopPolling(); showToast("Signed out", "You have been signed out of the portal."); navigate("/entry"); }
      }
      else if (action === "signout-route") {
        if (currentKind() === "admin") {
          localStorage.removeItem("admin_token"); state.adminAuthed = false;
          state.adminAccounts = []; state.adminAccountsLoaded = false; state.adminArchived = []; state.requests = []; state.accountCache = {}; state.accountDetailTried = {}; state.adminInstances = []; state.adminInstancesLoaded = false;
          state.requests = []; state.requestsLoaded = false; state.adminArchived = []; state.adminArchivedLoaded = false;
          state.adminEnvs = []; state.adminEnvsLoaded = false; state.adminBackups = []; state.adminBackupsLoaded = false;
          showToast("Signed out", "Admin session ended."); navigate("/admin/signin");
        } else if (state.impersonation) {
          exitImpersonation();
        } else {
          if (window.confirm("Sign out of the portal?")) { localStorage.removeItem("portal_token"); state.session = null; state.sessionLoaded = false; state.backupsLoaded = false; state.backups = []; state.provisioningPw = null; stopPolling(); navigate("/entry"); }
        }
      }
      else if (action === "toggle-password") {
        var inp = document.getElementById(act.dataset.target);
        if (inp) {
          inp.type = inp.type === "password" ? "text" : "password";
          act.innerHTML = icon(inp.type === "password" ? "eye" : "eyeoff");
        }
      }
      else if (action === "mfa-passkey" || action === "admin-mfa-passkey") {
        var isAdminMfa = action === "admin-mfa-passkey";
        var mfaState = isAdminMfa ? state.adminMfa : state.mfa;
        if (!mfaState) { navigate(isAdminMfa ? "/admin/signin" : "/entry"); return; }
        var pkStart = isAdminMfa ? "/admin/passkey/mfa/start" : "/auth/passkey/mfa/start";
        var pkVerify = isAdminMfa ? "/admin/passkey/mfa/verify" : "/auth/passkey/mfa/verify";
        if (!window.PublicKeyCredential) {
          showToast("Passkeys not supported", "This browser does not support WebAuthn. Use a modern browser, or enter a code.");
          return;
        }
        showModal(modalHeader(isAdminMfa ? "Admin passkey" : "Use a passkey") + '<div class="spinner"></div>');
        // Second factor: require the MFA challenge (password already proven).
        api(pkStart, { method: "POST", body: { challenge: mfaState.challenge } }).then(function (opts) {
          var raw = typeof opts === "string" ? JSON.parse(opts) : opts;
          var publicKey = Object.assign({}, raw);
          publicKey.challenge = _b64urlToBuf(raw.challenge);
          if (publicKey.allowCredentials) {
            publicKey.allowCredentials = publicKey.allowCredentials.map(function (c) {
              return { type: c.type, id: _b64urlToBuf(c.id), transports: c.transports };
            });
          }
          return navigator.credentials.get({ publicKey: publicKey }).then(function (cred) {
            var credentialJson = {
              id: cred.id, rawId: _bufToB64url(cred.rawId), type: cred.type,
              response: {
                clientDataJSON: _bufToB64url(cred.response.clientDataJSON),
                authenticatorData: _bufToB64url(cred.response.authenticatorData),
                signature: _bufToB64url(cred.response.signature),
                userHandle: cred.response.userHandle ? _bufToB64url(cred.response.userHandle) : undefined,
              },
            };
            return api(pkVerify, { method: "POST", body: { challenge: mfaState.challenge, credential: credentialJson } });
          });
        }).then(function (r) {
          closeModal();
          var tok = r && r.token;
          if (isAdminMfa) {
            localStorage.setItem("admin_token", tok);
            state.adminAuthed = true;
            state.adminMfa = null;
            refreshAdminData();
            showToast("Welcome back", "Signed in to the admin portal.");
            navigate("/admin/overview");
          } else {
            localStorage.setItem("portal_token", tok);
            state.session = { token: tok, account: r.account, instances: [] };
            state.mfa = null;
            loadPlans();
            navigate("/customer/dashboard");
          }
        }).catch(function (err) {
          closeModal();
          if (err && (err.name === "AbortError" || (err.message && err.message.indexOf("abort") !== -1))) {
            showToast("Passkey cancelled", "");
          } else {
            showToast("Passkey failed", err.message || String(err));
          }
        });
      }
      else if (action === "mfa-resend") {
        event.preventDefault();
        var ms = state.mfa;
        if (!ms) return;
        api("/auth/mfa-send-otp", { method: "POST", body: { email: ms.email, challenge: ms.challenge } }).then(function () {
          showToast("Code sent", "A new code has been emailed to you.");
        }).catch(function (err) { showToast("Could not send", err.message); });
      }
      else if (action === "admin-mfa-send" || action === "admin-mfa-resend") {
        event.preventDefault();
        var ams = state.adminMfa;
        if (!ams) return;
        var chan = (act.dataset && act.dataset.channel) || "email";
        api("/admin/mfa-send-otp", { method: "POST", body: { channel: chan, challenge: ams.challenge } }).then(function (r) {
          state.adminMfaSent = { channel: r.channel || chan, to: r.to || "" };
          showToast(chan === "sms" ? "Code texted" : "Code sent",
            (chan === "sms" ? "A text message is on its way to " : "A new code has been emailed to ") + (r.to || "you") + ".");
          render();
        }).catch(function (err) { showToast("Could not send", err.message); });
      }
      else if (action === "payment-continue") { doPayment(); }
      else if (action === "pay-now") { chosenPlan = (act.dataset && act.dataset.plan) || "a"; paymentModal(); }
      else if (action === "provision-now") { autoProvision(); }
      else if (action === "backup-run") {
        event.preventDefault();
        var bid = act.dataset.id, kind = act.dataset.kind || "full";
        var kindLabel = kind === "full" ? "full backup" : kind === "workflows" ? "workflow export" : "credential export";
        var instance = null;
        (state.session && state.session.instances || []).some(function (i) { if (i.id === Number(bid)) { instance = i; return true; } return false; });
        if (!instance) return;
        api("/me/instances/" + bid + "/backup?kind=" + kind, { method: "POST" }).then(function (d) {
          showToast("Backup started", "Creating a " + kindLabel + " of your " + titleCase(instance.stack_name) + " workspace. It will be ready to download in a few seconds.");
          // refresh the backups list shortly after
          setTimeout(function () {
            api("/me/backups").then(function (bk) { state.backups = (bk && bk.backups) || []; state.backupsLoaded = true; render(); })
              .catch(function () { state.backups = state.backups || []; state.backupsLoaded = true; render(); });
          }, 4000);
        }).catch(function (err) { showToast("Could not start backup", err.message); });
      }
      else if (action === "backup-download") {
        event.preventDefault();
        var bkid = act.dataset.id;
        var token = localStorage.getItem("portal_token") || localStorage.getItem("admin_token");
        var isAdmin = currentKind() === "admin";
        var base = isAdmin ? "/admin/backups" : "/me/backups";
        fetch(API + base + "/" + bkid + "/download", {
          headers: { "Authorization": "Bearer " + token }
        }).then(function (res) {
          if (!res.ok) { return res.json().then(function (d) { throw new Error(d.detail || "Download failed (" + res.status + ")"); }); }
          var name = (act.dataset.kind === "workflows" ? "workflows.json"
            : act.dataset.kind === "credentials" ? "credentials.json"
            : "n8n-data.tar.gz");
          var disp = res.headers.get("Content-Disposition") || "";
          var m = disp.match(/filename="?([^";]+)"?/);
          if (m) name = m[1];
          return res.blob().then(function (blob) {
            var url = URL.createObjectURL(blob);
            var a = document.createElement("a");
            a.href = url; a.download = name;
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
          });
        }).catch(function (err) { showToast("Download failed", err.message); });
      }
      else if (action === "sec-totp-setup") {
        showModal(modalHeader("Set up authenticator 2FA") + '<div class="spinner"></div>');
        api(act.dataset.kind === "admin" ? "/admin/security/totp/setup" : "/me/security/totp/setup", { method: "POST" }).then(function (d) {
          var qr = (d && d.qr) ? d.qr : "";
          showModal(modalHeader("Scan with your authenticator app") +
            '<div class="sec-qr">' + (qr ? '<img src="' + esc(qr) + '" alt="QR code" width="220" height="220">' : "") + "</div>" +
            '<p class="muted" style="text-align:center">Open your authenticator app and scan the QR, or type this key:</p>' +
            '<p class="sec-secret" style="text-align:center;font-family:monospace">' + esc(d.secret) + "</p>" +
            '<form data-form="sec-totp-enable"><div class="field"><label>Enter the 6-digit code</label>' +
            '<input name="code" type="text" inputmode="numeric" required maxlength="6" placeholder="123456" style="text-align:center;letter-spacing:2px"></div>' +
            '<button class="button primary-wide" type="submit">Confirm &amp; enable</button></form>' +
            '<p class="small muted" style="text-align:center;margin-top:14px">The code changes every 30 seconds.</p>');
        }).catch(function (err) { showToast("Setup failed", err.message); closeModal(); });
      }
      else if (action === "sec-passkey-setup") {
        var pkBase = act.dataset.kind === "admin" ? "/admin/security/passkey" : "/me/security/passkey";
        if (!window.PublicKeyCredential) {
          showToast("Passkeys not supported", "This browser does not support WebAuthn. Use a modern browser, or set up an authenticator method instead.");
          return;
        }
        showModal(modalHeader("Add a passkey") + '<div class="spinner"></div>');
        api(pkBase + "/register", { method: "POST" }).then(function (opts) {
          var raw = typeof opts === "string" ? JSON.parse(opts) : opts;
          var publicKey = Object.assign({}, raw);
          // The library returns already-fetchable fields; browser needs them as-is.
          publicKey.challenge = _b64urlToBuf(raw.challenge);
          publicKey.user.id = _b64urlToBuf(raw.user.id);
          if (publicKey.excludeCredentials) {
            publicKey.excludeCredentials = publicKey.excludeCredentials.map(function (c) {
              return { type: c.type, id: _b64urlToBuf(c.id), transports: c.transports };
            });
          }
          if (!publicKey.rp) publicKey.rp = { id: location.hostname, name: "SteProTECH Portal" };
          navigator.credentials.create({ publicKey: publicKey }).then(function (cred) {
            var credentialJson = {
              id: cred.id, rawId: _bufToB64url(cred.rawId),
              type: cred.type,
              response: {
                clientDataJSON: _bufToB64url(cred.response.clientDataJSON),
                attestationObject: _bufToB64url(cred.response.attestationObject),
              },
            };
            if (cred.response.transports) credentialJson.transports = Array.from(cred.response.transports);
            return api(pkBase + "/verify", { method: "POST", body: { credential: credentialJson } });
          }).then(function () {
            closeModal();
            showToast("Passkey added", "You can now sign in with this device.");
            refreshSecurity();
          }).catch(function (err) {
            // AbortError = user cancelled the prompt
            if (err && (err.name === "AbortError" || (err.message && err.message.indexOf("abort") !== -1))) {
              closeModal(); showToast("Passkey setup cancelled", "");
            } else {
              showToast("Passkey setup failed", err.message || String(err));
            }
          });
        }).catch(function (err) { showToast("Setup failed", err.message); closeModal(); });
      }
      else if (action === "sec-passkey-delete") {
        var dk2 = act.dataset.kind === "admin" ? "/admin/security/passkeys/" : "/me/security/passkeys/";
        showModal(modalHeader("Remove this passkey?") + "<p>You will no longer be able to sign in with this device's passkey.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-sec-passkey-delete="' + esc(act.dataset.kind) + '" data-cred="' + esc(act.dataset.cred) + '">Remove passkey</button></div>');
      }
      else if (action === "sec-totp-disable") {
        var dk = act.dataset.kind === "admin" ? "/admin/security/totp/disable" : "/me/security/totp/disable";
        showModal(modalHeader("Turn off authenticator 2FA?") + "<p>You will no longer be asked for an authenticator code at sign-in.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-sec-totp-disable="' + dk + '">Turn off</button></div>');
      }
      else if (action === "sec-email-setup") {
        var ek = act.dataset.kind === "admin" ? "/admin/security/email/send" : "/me/security/email/send";
        showModal(modalHeader("Set up email 2FA") + '<div class="spinner"></div>');
        api(ek, { method: "POST" }).then(function () {
          showModal(modalHeader("Enter the email code") +
            '<p class="muted">We emailed a 6-digit code to your account. Enter it below to confirm email 2FA.</p>' +
            '<form data-form="sec-email-enable"><div class="field"><label>Verification code</label>' +
            '<input name="code" type="text" inputmode="numeric" required maxlength="6" placeholder="123456" style="text-align:center;letter-spacing:2px"></div>' +
            '<button class="button primary-wide" type="submit">Confirm &amp; enable</button></form>' +
            '<p class="small muted" style="text-align:center;margin-top:14px">Did not arrive? <a href="#" data-action="sec-email-send"' + (act.dataset.kind === "admin" ? ' data-kind="admin"' : "") + '>Resend</a></p>');
        }).catch(function (err) { showToast("Could not send", err.message); closeModal(); });
      }
      else if (action === "sec-email-send") {
        event.preventDefault();
        var es = act.dataset.kind === "admin" ? "/admin/security/email/send" : "/me/security/email/send";
        api(es, { method: "POST" }).then(function () { showToast("Code sent", "A new code has been emailed to you."); })
          .catch(function (err) { showToast("Could not send", err.message); });
      }
      else if (action === "sec-sms-setup") {
        // Mirror of the email flow: prove the phone can receive a code before switching it on.
        showModal(modalHeader("Set up SMS 2FA") + '<div class="spinner"></div>');
        api("/admin/security/sms/send", { method: "POST" }).then(function (r) {
          showModal(modalHeader("Enter the texted code") +
            '<p class="muted">We texted a sign-in code to ' + esc(r.to || "your phone") + ". Enter it below to confirm SMS 2FA.</p>" +
            '<form data-form="sec-sms-enable"><div class="field"><label>Verification code</label>' +
            '<input name="code" type="text" inputmode="text" autocapitalize="characters" autocorrect="off" spellcheck="false" ' +
            'autocomplete="one-time-code" required maxlength="6" placeholder="Code" style="text-align:center;letter-spacing:.3em;text-transform:uppercase">' +
            "</div>" +
            '<button class="button primary-wide" type="submit">Confirm &amp; enable</button></form>' +
            '<p class="small muted" style="text-align:center;margin-top:14px">Did not arrive? <a href="#" data-action="sec-sms-send">Send another code</a></p>');
        }).catch(function (err) { showToast("Could not send", err.message); closeModal(); });
      }
      else if (action === "sec-sms-send") {
        event.preventDefault();
        api("/admin/security/sms/send", { method: "POST" }).then(function (r) {
          showToast("Code texted", "A new code is on its way to " + (r.to || "your phone") + ".");
        }).catch(function (err) { showToast("Could not send", err.message); });
      }
      else if (action === "sec-sms-disable") {
        showModal(modalHeader("Turn off SMS 2FA?") + "<p>You will no longer be texted a sign-in code.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-sec-sms-disable="1">Turn off</button></div>');
      }
      else if (action === "sec-email-disable") {
        var ed = act.dataset.kind === "admin" ? "/admin/security/email/disable" : "/me/security/email/disable";
        showModal(modalHeader("Turn off email 2FA?") + "<p>You will no longer be sent a sign-in code by email.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-sec-email-disable="' + ed + '">Turn off</button></div>');
      }
      else if (action === "provision-confirm") {
        var pv = String((document.getElementById("provision-pw") || {}).value || "");
        closeModal();
        if (!pv) { showToast("Password required", "Enter the password you chose at signup."); autoProvision(); return; }
        state.provisioningPw = pv;
        localStorage.setItem("portal_pw", pv);
        autoProvision();
      }
      else if (action === "add-workspace") addWorkspaceModal();
      else if (action === "add-workspace-submit") { event.preventDefault(); submitWorkspaceProvision(); }

      /* admin */
      else if (action === "admin-impersonate") impersonateConfirm(act.dataset.id);
      else if (action === "exit-impersonation") { exitImpersonation(); }
      else if (action === "quota") quotaModal(act.dataset.id);
      else if (action === "admin-lock") {
        showModal(modalHeader("Switch workspace off?") + "<p>This will stop the customer workspace until it is switched back on or renewed.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-account-lock="' + act.dataset.id + '">Confirm switch off</button></div>');
      }
      else if (action === "admin-unlock") {
        showModal(modalHeader("Switch workspace on?") + "<p>Restore the customer workspace and automations.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-account-unlock="' + act.dataset.id + '">Confirm switch on</button></div>');
      }
      else if (action === "admin-suspend") {
        showModal(modalHeader("Suspend this account?") +
          '<div class="consequence warn"><strong>What happens now</strong><ul>' +
          "<li>The workspace is switched off immediately (everything stops)</li>" +
          "<li>The customer cannot sign in to the portal</li>" +
          "<li>Subscription dates are kept; nothing is deleted</li>" +
          "<li>Unsuspend later: access stays off until you switch the workspace on or mark it paid</li></ul></div>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-account-state="suspend" data-id="' + act.dataset.id + '">' + icon("pause") + " Suspend account</button></div>");
      }
      else if (action === "admin-unsuspend") {
        showModal(modalHeader("Unsuspend this account?") +
          '<div class="consequence safe"><strong>What happens</strong><ul>' +
          "<li>The customer can sign in again</li>" +
          "<li>The workspace stays switched off until you switch it on or mark it paid</li></ul></div>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-account-state="unsuspend" data-id="' + act.dataset.id + '">' + icon("check") + " Unsuspend</button></div>");
      }
      else if (action === "admin-archive") {
        showModal(modalHeader("Archive this account?") +
          '<div class="consequence warn"><strong>What happens now</strong><ul>' +
          "<li>The workspace is switched off immediately</li>" +
          "<li>The account is hidden from the accounts list; nothing is deleted</li>" +
          "<li>Restore later moves it back (suspended, workspace off) for review</li></ul></div>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button danger" data-confirm-account-state="archive" data-id="' + act.dataset.id + '">' + icon("archive") + " Archive account</button></div>");
      }
      else if (action === "admin-restore") {
        showModal(modalHeader("Restore this archived account?") +
          '<div class="consequence safe"><strong>What happens</strong><ul>' +
          "<li>The account returns as suspended (workspace stays off)</li>" +
          "<li>Review it, then unsuspend and switch the workspace on when ready</li></ul></div>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-account-state="restore" data-id="' + act.dataset.id + '">' + icon("check") + " Restore account</button></div>");
      }
      else if (action === "admin-add-user") { adminAddUserModal(); }
      else if (action === "admin-mark-paid") { markPaidModal(act.dataset.id); }
      else if (action === "admin-extend-expiry") { extendExpiryModal(act.dataset.id); }
      else if (action === "payments-toggle") {
        var openTo = act.dataset.open === "1";
        var label = openTo ? "Open payments for customers?" : "Hold payments?";
        showModal(modalHeader(label) +
          (openTo
            ? '<div class="consequence safe"><strong>What happens</strong><ul><li>Customers can subscribe and renew again</li><li>New checkouts are enabled immediately</li></ul></div>'
            : '<div class="consequence"><strong>What happens</strong><ul><li>Nobody can start a subscription or renewal checkout</li><li>Existing subscriptions keep running untouched</li><li>Free access is still granted per account with Extend expiry</li><li>You can open payments again any time</li></ul></div>') +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button' + (openTo ? "" : " danger") + '" data-confirm-payments="1" data-open="' + (openTo ? "1" : "0") + '">' +
          (openTo ? icon("check") + " Open payments" : icon("pause") + " Hold payments") + "</button></div>");
      }
      else if (action === "updates-refresh") {
        state.adminUpdatesLoaded = false;
        ensureAdminUpdates(true);
      }
      else if (action === "updates-select-none") {
        state.updSel = {};
        render();
      }
      else if (action === "updates-queue-one") {
        queueUpdates([Number(act.dataset.id)]);
      }
      else if (action === "updates-queue-selected") {
        var picked = [].slice.call(document.querySelectorAll("[data-upd-check]"))
          .filter(function (cb) { return cb.checked; })
          .map(function (cb) { return Number(cb.value); });
        if (!picked.length) { showToast("Nothing selected", "Tick at least one workspace first."); }
        else { queueUpdates(picked); }
      }
      else if (action === "updates-queue-all") {
        queueUpdates(null);
      }
      else if (action === "updates-cancel") {
        api("/admin/updates/cancel?batch_id=" + encodeURIComponent(act.dataset.batch || ""), { method: "POST" })
          .then(function (r) {
            showToast("Batch cancelled", ((r && r.canceled) || 0) +
              " queued update(s) removed. Anything already running still finishes.");
            state.adminUpdatesLoaded = false;
            return ensureAdminUpdates(true);
          })
          .catch(function (e) { showToast("Could not cancel", (e && e.message) || "Try again."); });
      }
      else if (action === "ws-self-update") {
        // The customer's own button: it QUEUES, it never starts on click, and the
        // toast says so with their place in their server's line.
        act.disabled = true;
        api("/me/instances/" + Number(act.dataset.id) + "/update", { method: "POST" })
          .then(function (r) {
            showToast("Update requested", "Your workspace is in the line: position " + ((r && r.position) || 1) +
              ((r && r.eta_minutes) ? ", " + etaText(r.eta_minutes) : "") +
              ". We will email you the moment it is done.");
            state.myUpdatesLoaded = false;
            return ensureMyUpdates(true);
          })
          .catch(function (e) {
            act.disabled = false;
            showToast("Could not request the update", (e && e.message) || "Try again later.");
          });
      }
      else if (action === "ws-restart") {
        // Same container, same version, same data: the customer is told what to expect
        // and the server paces it, refusing with its own reason when it does not agree.
        act.disabled = true;
        api("/me/instances/" + Number(act.dataset.id) + "/restart", { method: "POST" })
          .then(function (r) {
            showToast("Restarting your workspace", (r && r.message) || "It is usually back in about half a minute.");
            state.myUpdatesLoaded = false;
            return ensureMyUpdates(true);
          })
          .catch(function (e) {
            act.disabled = false;
            showToast("Could not restart", (e && e.message) || "Try again in a moment.");
          });
      }
      else if (action === "admin-restart") {
        // The admin is not paced: restart is available whenever it is needed, and the
        // workspace it applies to is named so a wrong click is visible immediately.
        var rid = Number(act.dataset.id);
        state.instRestarting[rid] = true;
        render();
        api("/admin/instances/" + rid + "/restart", { method: "POST" })
          .then(function (r) {
            state.instRestarting[rid] = false;
            showToast("Restarting workspace", (r && r.message) || "It is usually back in about half a minute.");
            return refreshAdminData().then(function () { render(); });
          })
          .catch(function (e) {
            state.instRestarting[rid] = false;
            render();
            showToast("Could not restart", (e && e.message) || "Try again in a moment.");
          });
      }
      else if (action === "admin-attach") {
        var attId = act.dataset.id;
        // The scan walks every n8n server, so say so instead of appearing dead.
        showModal(modalHeader("Finding workspaces") + '<p class="muted" role="status" aria-live="polite">Checking the n8n servers for workspaces that are not attached to an account yet...</p>');
        (async function () {
          try {
            state.unlinkedStacks = await api("/admin/stacks/unlinked");
          } catch (e) { state.unlinkedStacks = []; }
          attachModal(attId);
        })();
      }
      else if (action === "after-add-attach") {
        var aaId = act.dataset.id;
        showModal(modalHeader("Finding workspaces") + '<p class="muted" role="status" aria-live="polite">Checking the n8n servers for workspaces that are not attached to an account yet...</p>');
        (async function () {
          try { state.unlinkedStacks = await api("/admin/stacks/unlinked"); } catch (e) { state.unlinkedStacks = []; }
          attachModal(aaId);
        })();
      }
      else if (action === "workspace-actions") {
        var accId = null;
        var inst = findInstanceById(act.dataset.id);
        workspaceActionsModal(inst || { id: act.dataset.id, stack_name: "Workspace" });
      }
      else if (action === "admin-backup") {
        event.preventDefault();
        var bInst = findInstanceById(act.dataset.id);
        if (!bInst) { showToast("Backup", "Workspace not found."); return; }
        var disabled = bInst.status !== "healthy" || bInst.locked;
        showModal(modalHeader("Back up " + titleCase(bInst.stack_name) + " workspace") +
          "<p class=\"muted\">Choose what to back up for this workspace.</p>" +
          '<div class="actions end" style="justify-content:flex-start;flex-wrap:wrap">' +
          '<button class="button" data-action="admin-backup-run" data-id="' + bInst.id + '" data-kind="full"' + (disabled ? " disabled" : "") + '>' + icon("archive") + " Full backup</button>" +
          '<button class="button secondary" data-action="admin-backup-run" data-id="' + bInst.id + '" data-kind="workflows"' + (disabled ? " disabled" : "") + '>Export workflows</button>' +
          '<button class="button secondary" data-action="admin-backup-run" data-id="' + bInst.id + '" data-kind="credentials"' + (disabled ? " disabled" : "") + '>Export credentials</button>' +
          "</div>" +
          '<p class="small muted" style="margin-top:16px">Downloads appear in your browser. Backup files stay on the portal for 30 days.</p>');
      }
      else if (action === "admin-backup-run") {
        event.preventDefault();
        var abInst = findInstanceById(act.dataset.id);
        var abKind = act.dataset.kind || "full";
        api("/admin/instances/" + act.dataset.id + "/backup?kind=" + abKind, { method: "POST" }).then(function (d) {
          showToast("Backup started", "Creating a " + (abKind === "full" ? "full backup" : abKind + " export") + ". Check the portal Backups page shortly.");
          refreshAdminData();
        }).catch(function (err) { showToast("Could not start backup", err.message); });
      }
      else if (action === "admin-backup-toggle") {
        event.preventDefault();
        var bktId = act.dataset.id, bktOn = act.dataset.on === "1";
        if (bktOn) {
          showModal(modalHeader("Revoke backup access?") +
            "<p>The customer will no longer see the backup option in their portal. The admin can still back this workspace up.</p>" +
            '<div class="consequence"><strong>This action will:</strong><ul><li>Hide the backup option from this customer</li><li>Not delete any existing backups</li><li>Be reversible (you can allow backup again)</li></ul></div>' +
            '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
            '<button class="button danger" data-confirm-admin-backup-toggle="' + bktId + '" data-on="0">Revoke access</button></div>');
        } else {
          showModal(modalHeader("Allow this customer to back up?") +
            "<p>Showing the backup option lets this customer create and download backups from their own portal. The admin can always back up this workspace too.</p>" +
            '<div class="consequence safe"><strong>This action will:</strong><ul><li>Show the backup option in the customer portal</li><li>Let the customer download their own backups</li><li>Be reversible (you can revoke at any time)</li></ul></div>' +
            '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
            '<button class="button" data-confirm-admin-backup-toggle="' + bktId + '" data-on="1">Allow backup</button></div>');
        }
      }
      else if (action === "admin-update-toggle") {
        event.preventDefault();
        var updId = act.dataset.id, updOn = act.dataset.on === "1";
        if (updOn) {
          showModal(modalHeader("Revoke update access?") +
            "<p>The customer will no longer see an update option in their portal. You can still update their workspaces yourself from the Updates page.</p>" +
            '<div class="consequence"><strong>This action will:</strong><ul><li>Hide the update option from this customer</li><li>Not stop an update that is already in the line</li><li>Be reversible (you can allow updates again)</li></ul></div>' +
            '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
            '<button class="button danger" data-confirm-admin-update-toggle="' + updId + '" data-on="0">Revoke access</button></div>');
        } else {
          showModal(modalHeader("Allow this customer to update?") +
            "<p>Showing the update option lets this customer update their own workspaces from their portal. Updates stay paced on the server: one workspace at a time per server, with a few quiet minutes after each one.</p>" +
            '<div class="consequence safe"><strong>This action will:</strong><ul><li>Show the update option on this customer workspace rows</li><li>Let them join the update line at any time</li><li>Be reversible (you can revoke at any time)</li></ul></div>' +
            '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
            '<button class="button" data-confirm-admin-update-toggle="' + updId + '" data-on="1">Allow update</button></div>');
        }
      }
      else if (action === "admin-update-image") {
        event.preventDefault();
        var uInst = findInstanceById(act.dataset.id);
        if (!uInst) { showToast("Update n8n", "Workspace not found."); return; }
        updateImageModal(uInst);
      }
      else if (action === "update-to-version") {
        event.preventDefault();
        var tv = (($("#img-tag") || {}).value || "").trim();
        if (!tv) { showToast("Enter a version", "Type the n8n version tag to update to."); return; }
        runImageUpdate(act.dataset.id, tv);
      }
      else if (action === "repull-version") {
        event.preventDefault();
        if (!act.dataset.tag) { showToast("Current version unknown", "Re-open the update dialog and try again."); return; }
        runImageUpdate(act.dataset.id, act.dataset.tag);
      }
      else if (action === "admin-change-plan") changePlanModal(act.dataset.id);
      else if (action === "admin-switch-plan") switchPlanModal(act.dataset.id, act.dataset.plan);
      else if (action === "request-upgrade") requestUpgrade();
      else if (action === "runner-toggle") {
        var rInst = (state.adminInstances || []).find(function (x) { return x.id === Number(act.dataset.id); }) ||
                    findInstanceById(act.dataset.id);
        if (!rInst) { showToast("Not found", "Could not resolve this workspace."); return; }
        if (rInst.python_runner) confirmDetachRunner(rInst.id); else confirmAttachRunner(rInst.id);
      }
      else if (action === "sandbox-toggle") {
        var sInst = (state.adminInstances || []).find(function (x) { return x.id === Number(act.dataset.id); }) ||
                    findInstanceById(act.dataset.id);
        if (!sInst) { showToast("Not found", "Could not resolve this workspace."); return; }
        if (sInst.ai_sandbox) confirmDetachSandbox(sInst.id); else confirmAttachSandbox(sInst.id);
      }
      else if (action === "update-image-now") {
        event.preventDefault();
        if (!act.dataset.tag) { showToast("Update n8n", "No target version."); return; }
        runImageUpdate(act.dataset.id, act.dataset.tag);
      }
      else if (action === "lock-workspace") confirmLock(act.dataset.id);
      else if (action === "unlock-workspace") confirmUnlock(act.dataset.id);
      else if (action === "reset-password") resetPasswordModal(act.dataset.id);
      else if (action === "admin-add-workspace") {
        var awId = act.dataset.id;
        var awAcc = (state.adminAccounts || []).find(function (x) { return x.id === Number(awId); });
        var curPlan = (awAcc && awAcc.plan) === "b" ? "b" : "a";
        var planName = function (v) { return v === "b" ? "Workspace Plus (GHS 500)" : "Workspace (GHS 300)"; };
        function awCard(val) {
          var on = val === curPlan;
          return '<label style="display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;margin:0">' +
            '<input type="radio" name="aw-plan" value="' + val + '"' + (on ? " checked" : "") + ' style="margin-top:3px">' +
            "<div><strong>" + esc(planName(val)) + "</strong><br><span class=\"small muted\">" +
            (val === "b"
              ? "Plus package: Python code node + AI assistant setup support."
              : "Standard package.") +
            (on ? " <strong>(customer\u2019s current package)</strong>" : " <em>(moving the subscription to this package)</em>") + "</span></div></label>";
        }
        showModal(modalHeader("Add workspace for " + esc(awAcc ? (awAcc.display_name || awAcc.username) : "customer")) +
          '<div class="banner" style="margin-bottom:14px">' + icon("info") + "<div><strong>Current package: " + esc(planName(curPlan)) + "</strong><br><span class=\"small\">Pick which package this new workspace should be. Choosing the other package also moves the customer\u2019s subscription to it.</span></div></div>" +
          '<div class="field"><label>Workspace package</label><div class="grid cols-2" style="gap:10px" id="aw-plan-picker">' + awCard("a") + awCard("b") + "</div></div>" +
          "<p>Access details are emailed to the customer.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-admin-provision="' + awId + '">Provision workspace</button></div>');
      }
      else if (action === "workspace-detail") {
        var i = findInstanceById(act.dataset.id);
        if (!i) return;
        showModal(modalHeader(titleCase(i.stack_name) + " workspace") +
          '<dl class="info-list"><div class="info-row"><dt>Address</dt><dd>' + esc(i.domain) + "</dd></div>" +
          '<div class="info-row"><dt>Status</dt><dd>' + esc(i.locked ? "Switched off" : cap(i.status)) + "</dd></div>" +
          '<div class="info-row"><dt>Created</dt><dd>' + esc(fmtDate(i.created_at)) + "</dd></div>" +
          '<div class="info-row"><dt>Environment</dt><dd>' + esc(i.environment_name || i.environment_id) + "</dd></div></dl>" +
          '<div class="actions end" style="margin-top:20px"><button class="button secondary" data-action="close-modal">Close</button>' +
          (i.status === "healthy" && !i.locked ? '<a class="button" href="https://' + esc(i.domain) + '/" target="_blank" rel="noopener">Open workspace</a>' : "") + "</div>");
      }
      else if (action === "run-maintenance") maintenanceModal();
      else if (action === "maintenance-run") {
        closeModal();
        adminAction("/admin/billing/sweep-expired", "Maintenance complete", null).then(function (data) {
          if (data) {
            var n = (data.locked || []).length;
            var last = $("#maintenance-last");
            if (last) last.textContent = "Last run: " + n + " expired workspace(s) switched off.";
            showModal('<div class="center-state"><div class="state-orb success">' + icon("check") + '</div><h2 id="modal-title">' + n + " workspace(s) switched off</h2>" +
              '<p class="muted">The maintenance sweep completed successfully.</p>' +
              '<button class="button" style="margin-top:22px" data-action="close-modal">Done</button></div>');
            refreshAdminData();
          }
        });
      }
      else if (action === "save-servers") {
        (async function () {
          try {
            var checks = Array.prototype.slice.call(document.querySelectorAll("[data-env-check]"));
            var picked = checks.filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
            if (!picked.length) { showToast("Select a server", "Pick at least one n8n server for new workspaces."); return; }
            state.envOrder = picked;
            await api("/admin/settings", { method: "PUT", body: { landing_environments: picked.join(",") } });
            showToast("Selection saved", picked.length === 1
              ? "Every new workspace will be created on the selected server."
              : "New workspaces are auto-placed on the least loaded selected server.");
            render();
          } catch (err) { showToast("Save failed", err.message); }
        })();
      }
      else if (action === "save-admin-profile") {
        (async function () {
          try {
            var em = String((document.getElementById("admin-profile-email") || {}).value || "").trim();
            var ph = String((document.getElementById("admin-profile-phone") || {}).value || "").trim();
            var res = await api("/admin/settings", { method: "PUT", body: { admin_email: em, admin_phone: ph } });
            state.adminSettings = Object.assign({}, state.adminSettings || {}, res);
            showToast("Profile saved", ph ? "Codes will go to " + (res.admin_phone_pretty || ph) + "." : "Email codes only for now.");
            render();
          } catch (err) { showToast("Could not save", err.message); }
        })();
      }
      else if (action === "save-sms") {
        (async function () {
          try {
            var keyEl = document.getElementById("sms-api-key");
            var senderEl = document.getElementById("sms-sender-id");
            var otpEl = document.getElementById("sms-otp-type");
            var body = {
              sms_sender_id: String((senderEl || {}).value || "").trim(),
              sms_otp_type: otpEl && otpEl.checked ? "otp" : "",
            };
            var typed = String((keyEl || {}).value || "").trim();
            if (typed) body.sms_api_key = typed;      // empty means "leave the saved key alone"
            var res = await api("/admin/settings", { method: "PUT", body: body });
            if (keyEl) keyEl.value = "";
            state.adminSettings = Object.assign({}, state.adminSettings || {}, res);
            showToast("SMS settings saved", res.sms_api_key_set ? "Key saved. Send a test SMS to be sure." : "Saved.");
            render();
          } catch (err) { showToast("Could not save", err.message); }
        })();
      }
      else if (action === "sms-status") {
        (async function () {
          try {
            state.smsStatus = await api("/admin/sms/status");
            render();
          } catch (err) { showToast("Check failed", err.message); }
        })();
      }
      else if (action === "sms-test") {
        (async function () {
          try {
            var r = await api("/admin/sms/test", { method: "POST" });
            showToast("Test text sent", "Sent to " + (r.to || "your phone") +
              (r.credit_left !== null && r.credit_left !== undefined ? " - " + r.credit_left + " credits left." : ".") +
              " Checking whether it actually delivers.");
            state.smsTest = { checking: true };
            render();
            // mNotify takes a moment to report, so ask a few times before giving a verdict.
            for (var i = 0; i < 4; i++) {
              await new Promise(function (res) { setTimeout(res, i === 0 ? 6000 : 10000); });
              try {
                var d = await api("/admin/sms/delivery?campaign_id=" + encodeURIComponent(r.campaign_id || ""));
                if (d && d.found) {
                  state.smsTest = d;
                  if (d.status === "REJECTED" || d.status === "DELIVERED") break;
                }
              } catch (e) { /* keep waiting */ }
            }
            if (state.smsTest && state.smsTest.checking) {
              state.smsTest = { status: "", reason: "mNotify has not reported yet. Check connection again in a minute." };
            }
            render();
          } catch (err) { showToast("Test failed", err.message); }
        })();
      }
      else if (action === "copy-code") {
        var code = act.dataset.code;
        if (navigator.clipboard && code) navigator.clipboard.writeText(code);
        showToast("Code copied", "The one-time code is ready to share securely.");
      }

      /* request approve */
      var approveBtn = event.target.closest("[data-approve]");
      if (approveBtn) approveModal(approveBtn.dataset.approve);
      var reqView = event.target.closest("[data-request-state]");
      if (reqView) {
        var rr = (state.requests || []).find(function (x) { return x.id === Number(reqView.dataset.requestState); });
        if (rr) {
          var isRegistered = rr.status === "registered";
          showModal(modalHeader(isRegistered ? "Already registered" : "Already approved") +
            '<div class="banner ' + (isRegistered ? "" : "success") + '">' + icon(isRegistered ? "account" : "check") +
            "<div><strong>Nothing else was sent</strong><br><span class=\"small\">" + esc(rr.email) + " " +
            (isRegistered ? "already has a customer portal account." : "was already approved and can create their account.") + "</span></div></div>" +
            '<button class="button" style="margin-top:20px" data-action="close-modal">Done</button>');
        }
      }

      var confirmApprove = event.target.closest("[data-confirm-approve]");
      if (confirmApprove) {
        var rid = confirmApprove.dataset.confirmApprove;
        (async function () {
          try {
            var data = await api("/admin/access-requests/" + rid + "/approve", { method: "POST" });
            closeModal();
            approvalSuccess(data);
            refreshAdminData();
          } catch (err) { showToast("Could not approve", err.message); }
        })();
      }

      /* request disapprove + delete */
      var denyBtn = event.target.closest("[data-request-deny]");
      if (denyBtn) denyModal(denyBtn.dataset.requestDeny);
      var delBtn = event.target.closest("[data-request-delete]");
      if (delBtn) deleteRequestModal(delBtn.dataset.requestDelete);

      var confirmDeny = event.target.closest("[data-confirm-request-deny]");
      if (confirmDeny) {
        var rid2 = confirmDeny.dataset.confirmRequestDeny;
        (async function () {
          try {
            closeModal();
            showToast("Disapproving\u2026", "Marking the request as disapproved.");
            await api("/admin/access-requests/" + rid2 + "/deny", { method: "POST" });
            showToast("Request disapproved", "The email cannot register.");
            refreshAdminData().then(function () { render(); });
          } catch (err) { showToast("Could not disapprove", err.message); }
        })();
      }
      var confirmDel = event.target.closest("[data-confirm-request-delete]");
      if (confirmDel) {
        var rid3 = confirmDel.dataset.confirmRequestDelete;
        (async function () {
          try {
            closeModal();
            showToast("Deleting request\u2026");
            await api("/admin/access-requests/" + rid3, { method: "DELETE" });
            showToast("Request deleted");
            refreshAdminData().then(function () { render(); });
          } catch (err) { showToast("Could not delete", err.message); }
        })();
      }

      var cState = event.target.closest("[data-confirm-account-state]");
      if (cState) {
        var stId = cState.dataset.id;
        var stAction = cState.dataset.confirmAccountState;
        closeModal();
        var stMsg = {
          suspend:   ["Account suspended", "The workspace is off and the customer cannot sign in."],
          unsuspend: ["Account unsuspended", "The customer can sign in; switch the workspace on when ready."],
          archive:   ["Account archived", "Hidden from the accounts list. Find it under Archived to restore."],
          restore:   ["Account restored", "Back as suspended; review, then unsuspend when ready."]
        }[stAction];
        (async function () {
          try {
            await api("/admin/accounts/" + stId + "/state", { method: "POST", body: { action: stAction } });
            showToast(stMsg[0], stMsg[1]);
            refreshAdminData().then(function () {
              if (stAction === "archive") { navigate("/admin/archived"); }
              else if (stAction === "restore") { navigate("/admin/accounts"); }
            });
          } catch (err) { showToast("Action failed", err.message); }
        })();
      }

      var confirmLockBtn = event.target.closest("[data-confirm-lock]");
      if (confirmLockBtn) {
        closeModal();
        var lockAid = instanceAccountId(confirmLockBtn.dataset.confirmLock);
        if (!lockAid) { showToast("Action failed", "Could not resolve the account for this workspace."); }
        else {
          adminAction("/admin/accounts/" + lockAid + "/lock", "Workspace switched off", "The customer and their automations are now unreachable.").then(function () { refreshAdminData(); });
        }
      }

      var cUnlock = event.target.closest("[data-confirm-unlock]");
      if (cUnlock) {
        closeModal();
        var unlockAid = instanceAccountId(cUnlock.dataset.confirmUnlock);
        if (!unlockAid) { showToast("Action failed", "Could not resolve the account for this workspace."); }
        else {
          adminAction("/admin/accounts/" + unlockAid + "/unlock", "Workspace switched on", "Access and automations have been restored.").then(function () { refreshAdminData(); });
        }
      }

      var cRAttach = event.target.closest("[data-confirm-runner-attach]");
      if (cRAttach) {
        closeModal();
        runRunnerToggle(Number(cRAttach.dataset.confirmRunnerAttach), "attach");
      }

      var cRDetach = event.target.closest("[data-confirm-runner-detach]");
      if (cRDetach) {
        closeModal();
        runRunnerToggle(Number(cRDetach.dataset.confirmRunnerDetach), "detach");
      }

      var cSAttach = event.target.closest("[data-confirm-sandbox-attach]");
      if (cSAttach) {
        closeModal();
        runSandboxToggle(Number(cSAttach.dataset.confirmSandboxAttach), "attach");
      }

      var cSDetach = event.target.closest("[data-confirm-sandbox-detach]");
      if (cSDetach) {
        closeModal();
        runSandboxToggle(Number(cSDetach.dataset.confirmSandboxDetach), "detach");
      }

      var cImp = event.target.closest("[data-confirm-impersonate]");
      if (cImp) {
        closeModal();
        var impId = cImp.dataset.confirmImpersonate;
        (async function () {
          try {
            var ir = await api("/admin/accounts/" + impId + "/impersonate", { method: "POST" });
            beginImpersonation(impId, ir);
          } catch (err) { showToast("Could not login as user", err.message); }
        })();
      }
      var cReset = event.target.closest("[data-confirm-reset]");
      if (cReset) {
        closeModal();
        adminAction("/instances/" + cReset.dataset.confirmReset + "/reset-password", "Password reset started", "The new n8n password will be emailed to the customer.").then(refreshAdminData);
      }
      // (n8n image updates moved to the updateImageModal flow + actions, 0.1.50)
      var cQuota = event.target.closest("[data-confirm-quota]");
      if (cQuota) {
        var qaVal = parseInt(($("#quota-input-a") || {}).value || "1", 10);
        var qbVal = parseInt(($("#quota-input-b") || {}).value || "1", 10);
        if ((!qaVal || qaVal < 1 || qaVal > 50) || (!qbVal || qbVal < 1 || qbVal > 50)) { showToast("Invalid allowance", "Allowances must be 1 to 50."); return; }
        closeModal();
        (async function () {
          var id = cQuota.dataset.confirmQuota;
          try {
            await api("/admin/accounts/" + id + "/quota", { method: "PUT", body: { quota: qaVal, plan: "a" } });
            await api("/admin/accounts/" + id + "/quota", { method: "PUT", body: { quota: qbVal, plan: "b" } });
            showToast("Allowances updated", qaVal + " Workspace (GHS 300) and " + qbVal + " Workspace Plus (GHS 500).");
            refreshAdminData();
          } catch (err) { showToast("Allowance update failed", err.message); }
        })();
      }
      var cBackupToggle = event.target.closest("[data-confirm-admin-backup-toggle]");
      if (cBackupToggle) {
        var bktId = cBackupToggle.dataset.confirmAdminBackupToggle;
        var bktEnabled = cBackupToggle.dataset.on === "1";
        closeModal();
        api("/admin/accounts/" + bktId + "/backup", { method: "PUT", body: { backup_enabled: bktEnabled } }).then(function () {
          showToast(bktEnabled ? "Backup access granted" : "Backup access revoked", bktEnabled
            ? "The customer can now create and download backups from their portal."
            : "The customer no longer sees a backup option.");
          refreshAdminData();
          render();
        }).catch(function (err) { showToast("Update failed", err.message); });
      }
      var cUpdateToggle = event.target.closest("[data-confirm-admin-update-toggle]");
      if (cUpdateToggle) {
        var updTId = cUpdateToggle.dataset.confirmAdminUpdateToggle;
        var updTOn = cUpdateToggle.dataset.on === "1";
        closeModal();
        api("/admin/accounts/" + updTId + "/update-access", { method: "PUT", body: { update_enabled: updTOn } }).then(function () {
          showToast(updTOn ? "Update access granted" : "Update access revoked", updTOn
            ? "The customer can now update their own workspaces from their portal."
            : "The customer no longer sees an update option.");
          refreshAdminData();
          render();
        }).catch(function (err) { showToast("Update failed", err.message); });
      }
      var cExtend = event.target.closest("[data-confirm-extend]");
      if (cExtend) {
        var exId = cExtend.dataset.confirmExtend;
        var exSel = document.getElementById("extend-years");
        var years = exSel ? Number(exSel.value) : 1;
        if (!years || years < 1 || years > 10) { showToast("Extend failed", "Pick between 1 and 10 years."); return; }
        closeModal();
        (async function () {
          try {
            var data = await api("/admin/accounts/" + exId + "/extend", { method: "POST", body: { years: years } });
            var newDate = data.paid_until ? fmtDate(data.paid_until) : "the new date";
            showToast("Expiry extended", "Free renewal: account active until " + newDate + ".");
            refreshAdminData();
          } catch (err) { showToast("Extend failed", err.message); }
        })();
      }
      var cPays = event.target.closest("[data-confirm-payments]");
      if (cPays) {
        var openNow = cPays.dataset.open === "1";
        closeModal();
        api("/admin/settings", { method: "PUT", body: { payments_open: openNow } }).then(function (d) {
          state.adminPaymentsOpen = d.payments_open !== false;
          showToast(openNow ? "Payments opened" : "Payments on hold",
            openNow ? "Customers can subscribe and renew again." : "New subscriptions and renewals are refused. Extend expiry to onboard users for free.");
          refreshAdminData();
          render();
        }).catch(function (err) { showToast("Update failed", err.message); });
      }
      var cPlan = event.target.closest("[data-confirm-plan]");
      if (cPlan) {
        var chosen = document.querySelector('input[name="chg-plan"]:checked');
        if (!chosen) { showToast("Pick a package", "Choose which package to move the account to."); return; }
        var cpId = cPlan.dataset.confirmPlan, cpVal = chosen.value;
        closeModal();
        api("/admin/accounts/" + cpId + "/plan", { method: "PUT", body: { plan: cpVal } })
          .then(function (d) {
            showToast(d.changed ? "Package changed" : "No change",
              d.changed ? (d.note || ("The account is now on " + d.label + ".")) : d.note);
            refreshAdminData();
          })
          .catch(function (err) { showToast("Change failed", err.message); });
      }
      var cSwitch = event.target.closest("[data-confirm-plan-switch]");
      if (cSwitch) {
        var swId = cSwitch.dataset.confirmPlanSwitch, swPlan = cSwitch.dataset.plan;
        closeModal();
        api("/admin/instances/" + swId + "/plan", { method: "POST", body: { plan: swPlan, backup_first: true } })
          .then(function (d) {
            showToast(d.changed ? (d.action === "downgrade" ? "Workspace downgraded" : "Workspace upgraded") : "Already matching",
              d.changed ? ("Now running " + d.to_label + ". The workspace restarted on the same data volume.") : (d.note || ""));
            refreshAdminData();
          })
          .catch(function (err) { showToast("Switch failed", err.message); });
      }
      var cMarkPaid = event.target.closest("[data-confirm-mark-paid]");
      if (cMarkPaid) {
        var mpId = cMarkPaid.dataset.confirmMarkPaid;
        var paidFrom = (document.getElementById("paid-from") || {}).value;
        var paidUntil = (document.getElementById("paid-until") || {}).value;
        var planSel = document.querySelector('input[name="paid-plan"]:checked');
        var planVal = planSel ? planSel.value : "";
        function toEpoch(dateStr) { var d = new Date(dateStr + "T23:59:59"); return Math.floor(d.getTime() / 1000); }
        if (!paidFrom || !paidUntil) { showToast("Dates required", "Pick both the start and expiry dates."); return; }
        if (!planVal) { showToast("Package required", "Choose which package was paid for."); return; }
        var until = toEpoch(paidUntil);
        var from = toEpoch(paidFrom);
        closeModal();
        (async function () {
          try {
            var data = await api("/admin/accounts/" + mpId + "/mark-paid", { method: "POST", body: { paid_until: until, paid_from: from, plan: planVal } });
            showToast(data.subscription_status === "active" ? "Marked as paid" : "Recorded as expired",
              data.subscription_status === "active" ? "Subscription active until " + fmtDate(until) + " on the " + (planVal === "b" ? "Plus (GHS 500)" : "Workspace (GHS 300)") + " package." : "Expiry was in the past; the workspace stays off.");
            refreshAdminData();
          } catch (err) { showToast("Mark-paid failed", err.message); }
        })();
      }
      var cAttach = event.target.closest("[data-confirm-attach]");
      if (cAttach) {
        var atId = cAttach.dataset.confirmAttach;
        var sel = document.getElementById("attach-stack");
        var opt = sel ? sel.options[sel.selectedIndex] : null;
        if (!opt) return;
        var payload = { environment_id: Number(opt.dataset.env), stack_name: opt.value, port: Number(opt.dataset.port || 0), domain: opt.dataset.domain || "" };
        closeModal();
        (async function () {
          try {
            var data = await api("/admin/accounts/" + atId + "/attach", { method: "POST", body: payload });
            showToast("Workspace attached", data.stack_name + " is now linked to this account (" + (data.running ? "running" : "off") + ").");
            state.unlinkedStacks = [];
            refreshAdminData();
          } catch (err) { showToast("Attach failed", err.message); }
        })();
      }
      var cAccountLock = event.target.closest("[data-confirm-account-lock]");
      if (cAccountLock) {
        closeModal();
        adminAction("/admin/accounts/" + cAccountLock.dataset.confirmAccountLock + "/lock", "Workspace switched off", "The customer and their automations are now unreachable.").then(refreshAdminData);
      }
      var cAccountUnlock = event.target.closest("[data-confirm-account-unlock]");
      if (cAccountUnlock) {
        closeModal();
        adminAction("/admin/accounts/" + cAccountUnlock.dataset.confirmAccountUnlock + "/unlock", "Workspace switched on", "Access and automations have been restored.").then(refreshAdminData);
      }
      var cAdminProv = event.target.closest("[data-confirm-admin-provision]");
      if (cAdminProv) {
        var awRadio = document.querySelector('input[name="aw-plan"]:checked');
        var awPlan = awRadio ? awRadio.value : "";
        closeModal();
        (async function () {
          try {
            await api("/accounts/" + cAdminProv.dataset.confirmAdminProvision + "/provision", { method: "POST", body: awPlan ? { plan: awPlan } : {} });
            showToast("Provisioning queued", awPlan === "b" ? "Workspace Plus (GHS 500) is being prepared." : awPlan === "a" ? "Workspace (GHS 300) is being prepared." : "The customer will receive an email when the workspace is ready.");
            refreshAdminData();
          } catch (err) { showToast("Provisioning failed", err.message); }
        })();
      }

      // sec-* confirm buttons (2FA toggle-off + passkey remove)
      var cSecTotp = event.target.closest("[data-confirm-sec-totp-disable]");
      if (cSecTotp) {
        closeModal();
        api(cSecTotp.dataset.confirmSecTotpDisable, { method: "POST" }).then(function () {
          showToast("Authenticator 2FA off", "");
          refreshSecurity();
        }).catch(function (err) { showToast("Could not disable", err.message); });
      }
      var cSecEmail = event.target.closest("[data-confirm-sec-email-disable]");
      if (cSecEmail) {
        closeModal();
        api(cSecEmail.dataset.confirmSecEmailDisable, { method: "POST" }).then(function () {
          showToast("Email 2FA off", "");
          refreshSecurity();
        }).catch(function (err) { showToast("Could not disable", err.message); });
      }
      var cSecSms = event.target.closest("[data-confirm-sec-sms-disable]");
      if (cSecSms) {
        closeModal();
        api("/admin/security/sms/disable", { method: "POST" }).then(function () {
          showToast("SMS 2FA off", "");
          refreshSecurity();
        }).catch(function (err) { showToast("Could not disable", err.message); });
      }
      var cSecPk = event.target.closest("[data-confirm-sec-passkey-delete]");
      if (cSecPk) {
        closeModal();
        var pkKind = cSecPk.dataset.confirmSecPasskeyDelete; // 'admin' | 'user'
        var pkCred = cSecPk.dataset.cred;
        var pkPath = (pkKind === "admin" ? "/admin/security/passkeys/" : "/me/security/passkeys/") + pkCred;
        api(pkPath, { method: "DELETE" }).then(function () {
          showToast("Passkey removed", "");
          refreshSecurity();
        }).catch(function (err) { showToast("Could not remove passkey", err.message); });
      }

      /* filters */
      var filter = event.target.closest("[data-filter]");
      if (filter) { state.accessFilter = filter.dataset.filter; render(); }

      /* (server reorder removed - placement is now checkbox selection) */
    });

    document.addEventListener("change", function (event) {
      // Updates page: remember which workspaces are ticked so the 15 second refresh
      // cannot silently clear a selection the admin is still building.
      var t = event.target;
      if (!t || !t.matches || !t.matches("[data-upd-check]")) return;
      state.updSel = state.updSel || {};
      if (t.checked) state.updSel[t.value] = true; else delete state.updSel[t.value];
      var label = document.querySelector("[data-action=\"updates-queue-selected\"]");
      if (label) {
        var n = Object.keys(state.updSel).length;
        label.textContent = "Update selected (" + n + ")";
        label.disabled = !n;
      }
    });
    document.addEventListener("input", function (event) {
      if (event.target.id === "account-search") {
        var q = event.target.value.toLowerCase();
        document.querySelectorAll("[data-account-row]").forEach(function (tr) {
          tr.style.display = tr.textContent.toLowerCase().indexOf(q) !== -1 ? "" : "none";
        });
      }
      // live password policy checks on the register page
      var pw = document.getElementById("reg-password");
      var confirm = document.getElementById("reg-confirm");
      if (event.target.id === "reg-password" || event.target.id === "reg-confirm") {
        var v = pw ? pw.value : "";
        var set = function (name, ok) {
          var el = document.querySelector('[data-check="' + name + '"]');
          if (el) { el.className = "check" + (ok ? " ok" : ""); el.innerHTML = (ok ? icon("check") + " " : "") + esc(el.dataset.label); }
        };
        [["len", v.length >= 8], ["upper", /[A-Z]/.test(v)], ["lower", /[a-z]/.test(v)], ["digit", /\d/.test(v)],
         ["match", !!confirm && !!v && confirm.value === v]].forEach(function (c) { set(c[0], c[1]); });
      }
    });

    document.addEventListener("submit", function (event) {
      var form = event.target.closest("form");
      if (!form) return;
      event.preventDefault();
      var type = form.dataset.form;

      if (type === "admin-add-user") {
        var fd = new FormData(form);
        var fname = String(fd.get("first_name") || "").trim();
        var lname = String(fd.get("last_name") || "").trim();
        var email = String(fd.get("email") || "").trim().toLowerCase();
        if (!fname || !lname || !email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showToast("Check the details", "First name, last name and a valid email are required."); return; }
        (async function () {
          try {
            var data = await api("/admin/accounts", { method: "POST", body: { email: email, first_name: fname, last_name: lname } });
            closeModal();
            showModal('<div class="center-state"><div class="state-orb success">' + icon("check") + '</div><h2 id="modal-title">User created</h2>' +
              '<p class="muted">' + esc(email) + " now has a portal account (unpaid / pending).</p>" +
              '<div class="card" style="margin:22px 0"><div class="eyebrow">Temporary password</div>' +
              '<div class="mono" style="font-size:1.4rem;letter-spacing:.12em;font-weight:800;margin:8px 0">' + esc(data.password_once || "") + "</div>" +
              '<button class="button secondary small-btn" data-action="copy-code" data-code="' + esc(data.password_once || "") + '">' + icon("copy") + " Copy password</button></div>" +
              '<div class="banner success" style="text-align:left">' + icon("check") + "<div><strong>Password emailed</strong><br><span class=\"small\">The same password was sent to the new user's inbox.</span></div></div>" +
              '<div class="actions" style="margin-top:20px"><button class="button secondary" data-action="after-add-attach" data-id="' + data.account_id + '">Attach workspace</button>' +
              '<button class="button" data-action="close-modal">Done</button></div></div>');
            refreshAdminData();
          } catch (err) { showToast("Could not create user", err.message); }
        })();
      }
      else if (type === "entry") {
        var email = String(new FormData(form).get("email") || "").trim().toLowerCase();
        if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showToast("Check your email", "Enter a valid email address."); return; }
        state.gateEmail = email;
        api("/auth/check", { method: "POST", body: { email: email } }).then(function (r) {
          state.gateEmail = r.email;
          if (r.action === "login") navigate("/signin");
          else if (r.action === "approved" || r.action === "token") navigate("/code");
          else if (r.action === "denied") navigate("/denied");
          else navigate("/request"); // requested | waiting -> same screen copy
        }).catch(function (err) { showToast("Could not continue", err.message); });
      }
      else if (type === "code") {
        var fd0 = new FormData(form);
        var first = String(fd0.get("first_name") || "").trim();
        var last = String(fd0.get("last_name") || "").trim();
        var pw2 = String(fd0.get("password") || "");
        var conf = String((document.getElementById("reg-confirm") || {}).value || "");
        if (!first || !last) { showToast("Missing details", "Enter your first and last name."); return; }
        if (pw2 !== conf) { showToast("Passwords do not match", "Re-enter the same password in both fields."); return; }
        if (!/^.*(?=.*[A-Z])(?=.*[a-z])(?=.*\d).{8,}$/.test(pw2)) { showToast("Password too weak", "Use 8+ characters with uppercase, lowercase, and a digit."); return; }
        var btn0 = form.querySelector("button[type=submit]");
        if (btn0) btn0.disabled = true;
        api("/accounts", { method: "POST", body: {
          email: state.gateEmail, username: null,
          first_name: first, last_name: last,
          password: pw2
        } }).then(function (r) {
          localStorage.setItem("portal_token", r.token);
          state.provisioningPw = pw2;
          localStorage.setItem("portal_pw", pw2);
          state.session = { token: r.token, account: r.account, instances: [] };
          showToast("Account created", "You are signed in. Choose a plan to create your workspace.");
          navigate("/customer/billing");
        }).catch(function (err) {
          showToast("Could not create account", err.message);
          if (btn0) btn0.disabled = false;
        });
      }
else if (type === "register") {
        var fd = new FormData(form);
        var first = String(fd.get("first_name") || "").trim();
        var last = String(fd.get("last_name") || "").trim();
        var uname = String(fd.get("username") || "").trim().toLowerCase() || null;
        var pw2 = String(fd.get("password") || "");
        var conf = String((document.getElementById("reg-confirm") || {}).value || "");
        if (!first || !last) { showToast("Missing details", "Enter your first and last name."); return; }
        if (pw2 !== conf) { showToast("Passwords do not match", "Re-enter the same password in both fields."); return; }
        if (!/^.*(?=.*[A-Z])(?=.*[a-z])(?=.*\d).{8,}$/.test(pw2)) { showToast("Password too weak", "Use 8+ characters with uppercase, lowercase, and a digit."); return; }
        if (uname && !/^[a-z0-9]([a-z0-9-]{0,60}[a-z0-9])?$/.test(uname)) { showToast("Invalid username", "Lowercase letters, digits, and hyphens only (no dots, no leading/trailing hyphen)."); return; }
        var btn = form.querySelector("button[type=submit]");
        if (btn) btn.disabled = true;
        api("/accounts", { method: "POST", body: {
          email: state.gateEmail, username: uname,
          first_name: first, last_name: last,
          password: pw2, access_token: state.gateToken
        } }).then(function (r) {
          localStorage.setItem("portal_token", r.token);
          state.provisioningPw = pw2;
          localStorage.setItem("portal_pw", pw2);
          state.session = { token: r.token, account: r.account, instances: [] };
          showToast("Account created", "You are signed in. Choose a plan to create your workspace.");
          navigate("/customer/billing");
        }).catch(function (err) {
          showToast("Could not create account", err.message);
          if (btn) btn.disabled = false;
        });
      }
      else if (type === "signin") {
        var f2 = new FormData(form);
        var em = String(f2.get("email") || "").trim().toLowerCase();
        api("/auth/login", { method: "POST", body: { email: em, password: String(f2.get("password") || "") } }).then(function (r) {
          if (r.mfa) {
            // 2FA required: stash the challenge + methods, show the second-factor step
            state.mfa = { email: em, challenge: r.mfa.challenge, methods: r.mfa.methods || [], codeSent: false };
            navigate("/mfa");
            return;
          }
          localStorage.setItem("portal_token", r.token);
          state.session = { token: r.token, account: r.account, instances: [] };
          loadPlans();
          navigate("/customer/dashboard");
        }).catch(function (err) { showToast("Sign in failed", err.message); });
      }
      else if (type === "forgot") {
        var fe = String(new FormData(form).get("email") || "").trim().toLowerCase();
        api("/auth/forgot-password", { method: "POST", body: { email: fe } }).then(function () {
          var done = document.getElementById("forgot-done");
          if (done) done.style.display = "flex";
          showToast("Reset link sent", "Check your email if an account exists for that address.");
        }).catch(function (err) { showToast("Could not send", err.message); });
      }
      else if (type === "reset") {
        var params = new URLSearchParams(location.hash.split("?")[1] || "");
        var tok = params.get("token") || "";
        var np = String(new FormData(form).get("password") || "");
        var btn = form.querySelector("button[type=submit]");
        if (btn) btn.disabled = true;
        api("/auth/reset-password", { method: "POST", body: { token: tok, password: np } }).then(function () {
          showToast("Password updated", "You can now sign in with your new password.");
          navigate("/entry");
        }).catch(function (err) {
          showToast("Reset failed", err.message);
          if (btn) btn.disabled = false;
        });
      }
      else if (type === "mfa") {
        var ms = state.mfa;
        if (!ms) { navigate("/entry"); return; }
        var code = String(new FormData(form).get("code") || "").trim();
        var btnm = form.querySelector("button[type=submit]");
        if (btnm) btnm.disabled = true;
        api("/auth/mfa-verify", { method: "POST", body: { email: ms.email, code: code, challenge: ms.challenge } }).then(function (r) {
          localStorage.setItem("portal_token", r.token);
          state.session = { token: r.token, account: r.account, instances: [] };
          state.mfa = null;
          loadPlans();
          navigate("/customer/dashboard");
        }).catch(function (err) {
          showToast("Code not accepted", err.message);
          if (btnm) btnm.disabled = false;
        });
      }
      else if (type === "sec-totp-enable") {
        var isAdm = (location.hash || "").indexOf("/admin/security") === 1;
        var tsPath = isAdm ? "/admin/security/totp/enable" : "/me/security/totp/enable";
        var tc = String(new FormData(form).get("code") || "").trim();
        api(tsPath, { method: "POST", body: { code: tc } }).then(function () {
          closeModal();
          showToast("Authenticator 2FA on", "A code from your app is now required at sign-in.");
          refreshSecurity();
        }).catch(function (err) { showToast("Code not accepted", err.message); });
      }
      else if (type === "sec-email-enable") {
        var isAdm2 = (location.hash || "").indexOf("/admin/security") === 1;
        var esPath = isAdm2 ? "/admin/security/email/enable" : "/me/security/email/enable";
        var ec = String(new FormData(form).get("code") || "").trim();
        api(esPath, { method: "POST", body: { code: ec } }).then(function () {
          closeModal();
          showToast("Email 2FA on", "A code is now emailed to you at sign-in.");
          refreshSecurity();
        }).catch(function (err) { showToast("Code not accepted", err.message); });
      }
      else if (type === "sec-sms-enable") {
        var scCode = String(new FormData(form).get("code") || "").trim();
        api("/admin/security/sms/enable", { method: "POST", body: { code: scCode } }).then(function () {
          closeModal();
          showToast("SMS 2FA on", "A code is now texted to you at sign-in.");
          refreshSecurity();
        }).catch(function (err) { showToast("Code not accepted", err.message); });
      }
      else if (type === "admin-signin") {
        var pwA = String(new FormData(form).get("password") || "");
        var btnA = form.querySelector("button[type=submit]");
        if (btnA) btnA.disabled = true;
        api("/admin/login", { method: "POST", body: { password: pwA } }).then(function (r) {
          if (r.mfa) {
            // Admin second factor required (authenticator/email/passkey).
            state.adminMfa = { challenge: r.mfa.challenge, methods: r.mfa.methods || [], codeSent: false };
            state.adminMfaSent = null;
            navigate("/admin/mfa");
            return;
          }
          localStorage.setItem("admin_token", r.token);
          state.adminAuthed = true;
          refreshAdminData();
          showToast("Welcome back", "Signed in to the admin portal.");
          navigate("/admin/overview");
        }).catch(function (err) {
          showToast("Sign in failed", err.message);
          if (btnA) btnA.disabled = false;
        });
      }
      else if (type === "admin-mfa") {
        var am = state.adminMfa;
        if (!am) { navigate("/admin/signin"); return; }
        var acode = String(new FormData(form).get("code") || "").trim();
        var abtn = form.querySelector("button[type=submit]");
        if (abtn) abtn.disabled = true;
        api("/admin/mfa-verify", { method: "POST", body: { challenge: am.challenge, code: acode } }).then(function (r) {
          localStorage.setItem("admin_token", r.token);
          state.adminAuthed = true;
          state.adminMfa = null;
          refreshAdminData();
          showToast("Welcome back", "Signed in to the admin portal.");
          navigate("/admin/overview");
        }).catch(function (err) {
          showToast("Code not accepted", err.message);
          if (abtn) abtn.disabled = false;
        });
      }
    });
  }

  function refreshSecurity() {
    // Re-fetch the 2FA state after enable/disable and re-render only if the
    // security data actually changed (guard avoids the render<->fetch loop).
    var route = (location.hash || "").slice(1);
    var secPath = route.indexOf("admin") === 1 ? "/admin/security" : "/me/security";
    api(secPath).then(function (d) {
      state.security2fa = d;
      if (!modalRoot.children.length) render();
    }).catch(function () { state.security2fa = null; if (!modalRoot.children.length) render(); });
  }

  function qrDataUrl(uri) {
    // The backend returns the QR as a data URI in the setup response (d.qr);
    // this is a passthrough when a caller passes the URI directly.
    return (uri && uri.indexOf("data:") === 0) ? uri : "";
  }

  // ---- passkey helpers (WebAuthn base64url <-> ArrayBuffer) ----
  function _b64urlToBuf(str) {
    if (!str) return new Uint8Array(0);
    var b64 = String(str).replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    var bin = atob(b64);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function _bufToB64url(buf) {
    var bytes = new Uint8Array(buf);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function findInstanceById(id) {
    var insts = [];
    var acc = state.session ? state.session.instances : [];
    if (acc.length) insts = insts.concat(acc);
    Object.keys(state.accountCache).forEach(function (k) {
      insts = insts.concat(state.accountCache[k].instances || []);
    });
    state.adminAccounts = state.adminAccounts || [];
    return insts.find(function (x) { return String(x.id) === String(id); });
  }
  function instanceAccountId(instanceId) {
    var i = findInstanceById(instanceId);
    if (i) return i.account_id;
    var route = (location.hash || "").slice(1);
    var m = route.match(/^\/admin\/account\/(\d+)$/);
    return m ? m[1] : null;
  }
  function accountLockPath(instanceId) {
    var aid = instanceAccountId(instanceId);
    return aid ? "/admin/accounts/" + aid + "/lock" : null;
  }

  /* ================= boot ================= */
  function _validStoredToken(tok) {
    // Only trust a real session JWT. The broken pre-0.1.40 flow could write the
    // literal '__mfa__' sentinel (which must NEVER be treated as a session), so
    // reject anything that is not a 3-part JWT. A stale/garbage token would
    // otherwise make boot() think we're authed, then the next admin API call
    // 401s and the session is torn down -> the "locks me in then locks me out"
    // symptom Steward hit 2026-09-03.
    if (!tok || typeof tok !== "string") return false;
    if (tok === "__mfa__") return false;
    var parts = tok.split(".");
    if (parts.length !== 3) return false;
    return true;
  }

  function boot() {
    loadPlans();
    var storedAdmin = localStorage.getItem("admin_token");
    // Drop a stale/invalid admin token so boot() never treats it as a live session.
    if (!_validStoredToken(storedAdmin)) {
      if (storedAdmin) localStorage.removeItem("admin_token");
      storedAdmin = null;
    }
    state.adminAuthed = !!storedAdmin;
    var portalToken = localStorage.getItem("portal_token");
    // Rehydrate an in-flight admin impersonation across reloads (banner + exit
    // stay available). Clean stale impersonation/backup keys otherwise.
    var impRaw = localStorage.getItem("impersonation");
    if (impRaw && portalToken && _validStoredToken(portalToken)) {
      try { state.impersonation = JSON.parse(impRaw); } catch (e) { state.impersonation = null; }
      if (!state.impersonation || !state.impersonation.id) state.impersonation = null;
    }
    if (!state.impersonation) {
      localStorage.removeItem("impersonation");
      localStorage.removeItem("admin_token_backup");
    }
    // Seed the latest n8n release tag for admin "update available" chips.
    if (state.adminAuthed) {
      fetchN8nReleases().then(function (d) { if (d && d.latest) state.n8nLatestTag = d.latest.tag; });
    }
    // handle payment return: ?status=success after Paystack redirect (0.1.71):
    // the webhook can land a second or two AFTER the browser returns, so poll
    // /me briefly until the subscription flips active, then auto-provision.
    // If this browser has no portal token (customer paid elsewhere), the
    // server-side sweep still provisions; nothing is lost.
    var params = new URLSearchParams(location.search);
    if (params.get("status") === "success" && portalToken) {
      history.replaceState(null, "", location.pathname + location.hash);
      if (params.get("checkout") !== "mock") {
        (async function () {
          var sess = null;
          for (var attempt = 0; attempt < 10; attempt++) {
            sess = await refreshSession();
            if (sess && sess.account && sess.account.subscription_status === "active") break;
            await new Promise(function (res) { setTimeout(res, 2000); });
          }
          if (sess && sess.account && sess.account.subscription_status === "active") {
            showToast("Payment received", "Your subscription is active. Preparing your workspace...");
            autoProvision();
            navigate("/customer/dashboard");
          } else {
            showToast("Payment received", "We are confirming your payment; your workspace will be prepared automatically.");
            navigate("/customer/billing");
          }
        })();
      }
    }
    eventHandlers();
    window.addEventListener("hashchange", render);
    render();
  }

  boot();
})();
