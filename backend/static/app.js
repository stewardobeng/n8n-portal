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
    gateEmail: null,
    gateToken: null,
    environmentList: [],  // [{id, name, status}] from GET /environments
    envOrder: [],         // ordered env ids from admin settings
    accountCache: {},     // admin: accountId -> {account, instances}
    requests: [],         // admin: access requests
    adminEnvs: [],        // admin: n8n Server 1..N + health (GET /admin/environments)
    unlinkedStacks: [],   // admin: attach candidates (GET /admin/stacks/unlinked)
    menuOpen: false,
    pollTimer: null,
    provisioningPw: null  // signup password held in-session for owner auto-create
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
  settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /> <circle cx="12" cy="12" r="3" />',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" /> <path d="m9 12 2 2 4-4" />',
  signout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /> <polyline points="16 17 21 12 16 7" /> <line x1="21" x2="9" y1="12" y2="12" />',
  spark: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" /> <path d="M20 3v4" /> <path d="M22 5h-4" /> <path d="M4 17v2" /> <path d="M5 18H3" />',
  support: '<circle cx="12" cy="12" r="10" /> <path d="m4.93 4.93 4.24 4.24" /> <path d="m14.83 9.17 4.24-4.24" /> <path d="m14.83 14.83 4.24 4.24" /> <path d="m9.17 14.83-4.24 4.24" /> <circle cx="12" cy="12" r="4" />',
  up: '<path d="m18 15-6-6-6 6" />',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /> <circle cx="9" cy="7" r="4" /> <path d="M22 21v-2a4 4 0 0 0-3-3.87" /> <path d="M16 3.13a4 4 0 0 1 0 7.75" />',
  warning: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" /> <path d="M12 9v4" /> <path d="M12 17h.01" />',
  workspace: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />',
};

  function icon(name) {
    var inner = ICONS[name] || ICONS.plus;
    return '<svg class="icon-svg" viewBox="0 0 24 24" aria-hidden="true">' + inner + '</svg>';
  }

  function statusBadge(type, label) {
    var map = { active: "success", running: "success", approved: "success", awaiting: "warning",
      pastdue: "warning", provisioning: "info", registered: "info", failed: "danger",
      expired: "danger", off: "neutral", cancelled: "neutral", locked: "danger",
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
    ["billing", "Plans & billing", "/customer/billing"],
    ["support", "Support", "/customer/support"],
    ["account", "Account", "/customer/account"]
  ];
  var adminNav = [
    ["overview", "Overview", "/admin/overview"],
    ["requests", "Access requests", "/admin/requests"],
    ["accounts", "Accounts", "/admin/accounts"],
    ["maintenance", "Billing maintenance", "/admin/maintenance"],
    ["settings", "Settings", "/admin/settings"]
  ];

  function profileChip() {
    var isAdmin = currentKind() === "admin";
    var acc = state.session ? state.session.account : null;
    var name = isAdmin ? "Administrator" : (acc ? (acc.display_name || cap(acc.username)) : "Account");
    var sub = isAdmin ? "SteProTECH staff" : (acc ? acc.email : "");
    var initials = isAdmin ? "SO" : (acc ? initialsOf(acc.display_name || acc.username) : "?");
    return '<div class="profile-chip"><span class="avatar">' + esc(initials) + "</span><span><strong>" + esc(name) + "</strong><small>" + esc(sub) + "</small></span></div>";
  }
  function initialsOf(name) {
    var words = String(name || "?").split(/[\s._-]+/).filter(function (w) { return w.length; });
    var a = (words[0] || "?").charAt(0).toUpperCase();
    var b = words.length > 1 ? (words[1] || "").charAt(0).toUpperCase() : "";
    return a + b;
  }
  function currentKind() {
    var h = location.hash || "";
    return h.indexOf("/admin/") === 0 ? "admin" : "customer";
  }

  function appLayout(kind, title, active, content) {
    var isAdmin = kind === "admin";
    var nav = isAdmin ? adminNav : customerNav;
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
      '<div class="main-area">' +
        '<header class="topbar"><div class="actions">' +
          '<button class="top-icon mobile-menu" data-action="menu" aria-label="Open menu">' + icon("menu") + "</button>" +
          "<h2>" + esc(title) + "</h2></div>" +
          '<div class="top-actions"><button class="top-icon" data-action="signout-route" aria-label="Sign out" title="Sign out">' + icon("signout") + "</button></div>" +
        "</header>" + content +
      "</div></div>";
  }

  function pageHead(title, description, actions) {
    return '<div class="page-head"><div><h1>' + esc(title) + '</h1><p class="muted">' + esc(description || "") + "</p></div>" + (actions || "") + "</div>";
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

  function requestPage() {
    var email = state.gateEmail || "your address";
    return authLayout(
      '<div class="center-state"><div class="state-orb">' + icon("mail") + "</div>" +
      '<div class="eyebrow">Request received</div><h1>Your request is awaiting approval</h1>' +
      '<p class="muted">A SteProTECH team member will review it shortly. We will email a one-time access code to <strong>' + esc(email) + "</strong>.</p>" +
      '<div class="banner" style="margin:26px 0;text-align:left">' + icon("clock") +
        "<div><strong>Watch your inbox</strong><br><span class=\"small\">Approval is handled by a person. You do not need to submit another request.</span></div></div>" +
      '<div class="actions" style="justify-content:center"><button class="button secondary" data-route="/entry">Use a different email</button></div></div>'
    );
  }

  function codePage() {
    var email = state.gateEmail || "";
    return authLayout(
      '<div class="eyebrow">Email approved</div><h1>Enter your access code</h1>' +
      '<p class="muted">We sent an access code to <strong>' + esc(email) + '</strong>. It expires after 72 hours. The code looks like <span class="mono">K7FQ-2MXP</span>.</p>' +
      '<form data-form="code"><div class="field"><label for="access-token">Access code</label>' +
      '<input id="access-token" name="token" type="text" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="XXXX-XXXX" maxlength="9" style="text-transform:uppercase;letter-spacing:.12em;font-family:ui-monospace,monospace;font-weight:700">' +
      '<span class="hint" id="token-hint"></span></div>' +
      '<button class="button primary-wide" type="submit">Verify code</button></form>' +
      '<p class="small muted" style="text-align:center;margin-top:18px">Havent received it? <a href="#/entry">Use a different email</a> or contact support@steprotech.com</p>'
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

  function signinPage() {
    return authLayout(
      '<div class="eyebrow">Welcome back</div><h1>Sign in to your portal</h1>' +
      '<p class="muted">Use your portal password to manage billing and open your workspace.</p>' +
      '<form data-form="signin"><div class="field"><label>Email address</label><input name="email" type="email" required></div>' +
      '<div class="field"><label>Password</label><div class="input-row">' +
      '<input id="signin-password" name="password" type="password" required>' +
      '<button type="button" class="input-action" data-action="toggle-password" data-target="signin-password">' + icon("eye") + '</button></div></div>' +
      '<button class="button primary-wide" type="submit">Sign in</button></form>' +
      '<div class="banner" style="margin-top:24px">' + icon("support") + "<div><strong>Forgot your portal password?</strong><br><span class=\"small\">Contact support@steprotech.com for help. Password recovery inside n8n is separate.</span></div></div>" +
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
        '<button class="button warning" data-action="pay-now">Renew now</button></div>';
    }
    if (s === "cancelled") {
      return '<div class="hero-status warning"><span class="big-icon">' + icon("warning") + '</span><div><h2>Your subscription is cancelled</h2><p>Your workspace remains available until the paid period ends on ' + esc(fmtDate(acc.paid_until)) + ".</p></div>" +
        '<a class="button secondary" href="#/customer/billing">View billing</a></div>';
    }
    if (s === "expired") {
      return '<div class="hero-status danger"><span class="big-icon">' + icon("lock") + "</span><div><h2>Your workspace is switched off</h2><p>Renew to switch it back on with all your work intact.</p></div>" +
        '<button class="button danger" data-action="pay-now">Renew now</button></div>';
    }
    // subscription not active yet -> subscribe
    var plan = state.plans.find(function (p) { return p.active; }) || state.plans[0];
    return '<div class="hero-status info"><span class="big-icon">' + icon("billing") + '</span><div><h2>Choose a plan to get started</h2><p>Subscribe to the annual plan and your private workspace is created after payment.</p></div>' +
      '<button class="button" data-action="pay-now">' + (plan ? "Subscribe " + fmtMinor(plan.amount_minor, plan.currency) + "/yr" : "Subscribe") + "</button></div>";
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
    var quota = acc ? (acc.quota || 1) : 1;
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
          '<dl class="info-list"><div class="info-row"><dt>Plan</dt><dd>' + esc(planName(acc)) + "</dd></div>" +
          '<div class="info-row"><dt>Annual fee</dt><dd>' + esc(planFee()) + "</dd></div>" +
          '<div class="info-row"><dt>Period end</dt><dd>' + esc(periodEnd) + "</dd></div></dl></section>" +
        '<section class="card"><div class="card-head"><h3>Workspace quota</h3><span class="muted small">' + used + " of " + quota + " used</span></div>" +
          '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div><strong>' + Math.round(quota ? used / quota * 100 : 0) + "%</strong></div>" +
          '<p class="small muted" style="margin-top:20px">' + (used < quota ? "You can create another workspace." : "Contact support if your business needs a higher limit.") + "</p>" +
          (used < quota && acc && acc.subscription_status === "active"
            ? '<button class="button secondary small-btn" data-action="add-workspace">Add workspace</button>' : "") + "</section>" +
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

  function planName(acc) {
    var p = state.plans.find(function (x) { return x.active; }) || state.plans[0];
    return p ? p.name : "n8n Workspace";
  }
  function planFee() {
    var p = state.plans.find(function (x) { return x.active; }) || state.plans[0];
    return p ? fmtMinor(p.amount_minor, p.currency) + " / year" : "-";
  }

  function workspaceItemHtml(inst, badgeHtml, actionHtml) {
    if (!inst) {
      return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
        "<div><h3>No workspace yet</h3><p>Subscribe and provision to create your first workspace.</p></div>" +
        statusBadge("off", "Not created") + "</article>";
    }
    return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
      "<div><h3>" + esc(titleCase(inst.stack_name)) + " workspace</h3><p>" + esc(inst.domain || "") + "</p></div>" +
      (badgeHtml || "") + (actionHtml || "") + "</article>";
  }

  function customerWorkspaces() {
    var s = subscriptionState();
    var acc = state.session ? state.session.account : null;
    var insts = state.session ? (state.session.instances || []).filter(function (i) { return i.status !== "deleted"; }) : [];
    var used = insts.length;
    var quota = acc ? (acc.quota || 1) : 1;
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
        "<div><h3>" + esc(titleCase(i.stack_name)) + " workspace</h3><p>https://" + esc(i.domain) + "<br>Created " + esc(fmtDate(i.created_at)) + "</p></div>" +
        badge + action + "</article>";
    }).join("")
      : '<div class="empty-state" style="min-height:240px"><div><h3>No workspaces yet</h3><p class="muted">' + esc(emptyCopy) + "</p></div></div>";
    return appLayout("customer", "My workspaces", "workspace",
      '<main class="page">' + pageHead("My workspaces", "Open, monitor, and request additional private automation workspaces.", '<div class="actions">' + addBtn + "</div>") +
      '<section class="card flush">' + listHtml + "</section>" + detail +
      '<section class="card" style="margin-top:18px"><div class="card-head"><h3>Workspace allowance</h3><strong>' + used + " of " + quota + " used</strong></div>" +
        '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div>' +
        '<span>' + Math.max(0, quota - used) + " available</span></div>" +
        '<p class="small muted" style="margin:18px 0 0">Each workspace receives its own secure SteProTECH address. Contact support if you need more than your current allowance.</p></section>' +
      "</main>");
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
    var planCards = plans.map(function (p) {
      var activePlan = !!p.active;
      if (activePlan) {
        var cta;
        if (subscribed) {
          cta = '<span class="plan-already">' + icon("check") + " Already subscribed</span>";
        } else if (due) {
          cta = '<button class="button warning" data-action="pay-now">Renew subscription</button>';
        } else {
          cta = '<button class="button" data-action="pay-now">Subscribe</button>';
        }
        return '<article class="plan-card featured"><div class="eyebrow">Available now</div><h2>' + esc(p.name) + "</h2>" +
          '<div class="plan-price">' + esc(fmtMinor(p.amount_minor, p.currency)) + ' <span>per year</span></div>' +
          '<p class="muted">A private, managed n8n environment for your business automations.</p>' +
          '<ul class="feature-list">' + ["Your own private workspace", "Your own secure address", "Email support", "Automatic annual renewal"].map(function (f) { return "<li>" + icon("check") + "<span>" + esc(f) + "</span></li>"; }).join("") + "</ul>" +
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

    return appLayout("customer", "Plans & billing", "billing",
      '<main class="page">' + pageHead("Plans & billing", "Manage your annual subscription and understand what happens next.") +
      '<div class="plan-grid">' + planCards + "</div>" +
      '<div class="grid cols-2" style="margin-top:20px">' +
        '<section class="card"><div class="card-head"><h2>Subscription details</h2>' + statusRow + "</div>" +
          '<dl class="info-list"><div class="info-row"><dt>Plan</dt><dd>' + esc(planName(acc)) + "</dd></div>" +
          '<div class="info-row"><dt>Billing interval</dt><dd>Annually</dd></div>' +
          '<div class="info-row"><dt>Current period end</dt><dd>' + esc(periodEnd) + "</dd></div>" +
          '<div class="info-row"><dt>Payment method</dt><dd>Paystack</dd></div></dl></section>' +
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
    var awaiting = requests.filter(function (r) { return r.status === "requested"; }).length;
    var pastDue = accounts.filter(function (a) { return a.subscription_status === "past_due"; }).length;
    var locked = accounts.filter(function (a) { return a.subscription_status === "locked" || a.subscription_status === "canceled"; }).length;
    var total = accounts.length;

    var attentionRows = accounts.filter(function (a) {
      return ["past_due", "locked", "canceled"].indexOf(a.subscription_status) !== -1;
    }).slice(0, 6);
    var requestRows = requests.slice(0, 6);

    return appLayout("admin", "Overview", "overview",
      '<main class="page">' + pageHead("Today at a glance", "See what needs attention and jump directly to the right task.") +
      '<div class="grid cols-4">' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon">' + icon("requests") + "</span>" + statusBadge("awaiting", "Needs review") + "</div>" +
          '<div class="metric-value">' + awaiting + '</div><p class="muted">Access requests awaiting approval</p><a href="#/admin/requests">Review requests ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--amber-100);color:var(--amber-700)">' + icon("warning") + "</span></div>" +
          '<div class="metric-value">' + pastDue + '</div><p class="muted">Accounts past due</p><a href="#/admin/accounts">View accounts ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--green-100);color:var(--green-700)">' + icon("workspace") + "</span></div>" +
          '<div class="metric-value">' + locked + '</div><p class="muted">Locked / cancelled accounts</p><a href="#/admin/accounts">Review ' + icon("arrow") + '</a></article>' +
        '<article class="card metric"><div class="metric-top"><span class="metric-icon" style="background:var(--slate-100);color:var(--slate-700)">' + icon("account") + "</span></div>" +
          '<div class="metric-value">' + total + '</div><p class="muted">Total customer accounts</p><a href="#/admin/accounts">View accounts ' + icon("arrow") + '</a></article>' +
      "</div>" +
      envHealthCards() +
      '<div class="grid cols-3" style="margin-top:18px">' +
        '<section class="card flush span-2"><div class="card-head" style="padding:20px 20px 0"><h2>Access requests</h2><a class="button ghost small-btn" href="#/admin/requests">View all</a></div>' +
          '<div class="table-wrap"><table><thead><tr><th>Email</th><th>Status</th><th>Requested</th></tr></thead><tbody>' +
          (requestRows.length ? requestRows.map(function (r) {
            return "<tr><td><strong>" + esc(r.email) + "</strong></td><td>" + requestStatusLabel(r.status) + "</td><td>" + esc(fmtDateTime(r.created_at)) + "</td></tr>";
          }).join("") : '<tr><td colspan="3"><div class="empty-state" style="min-height:160px"><div><h3>No access requests</h3><p class="muted">New requests appear here.</p></div></div></td></tr>') +
          "</tbody></table></div></section>" +
        '<aside class="card"><h2>Quick actions</h2><div class="grid">' +
          '<a class="button" href="#/admin/requests">Review access requests</a>' +
          '<a class="button secondary" href="#/admin/accounts">Find a customer</a>' +
          '<a class="button secondary" href="#/admin/maintenance">Run expiry maintenance</a>' +
        "</div>" +
        (attentionRows.length ? '<div class="banner warning" style="margin-top:18px">' + icon("clock") +
          "<div><strong>" + attentionRows.length + " account(s) need attention</strong><br><span class=\"small\">" +
          esc(attentionRows.map(function (a) { return a.username; }).join(", ")) + "</span></div></div>" : "") +
        "</aside></div></main>");
  }

  function requestStatusLabel(st) {
    if (st === "requested") return statusBadge("awaiting", "Awaiting approval");
    if (st === "token_sent") return statusBadge("approved", "Code sent");
    return statusBadge("registered", "Already registered");
  }

  function adminRequests() {
    var requests = state.requests || [];
    var counts = {
      all: requests.length,
      awaiting: requests.filter(function (r) { return r.status === "requested"; }).length,
      approved: requests.filter(function (r) { return r.status === "token_sent"; }).length,
      registered: requests.filter(function (r) { return r.status === "registered"; }).length
    };
    var filter = state.accessFilter || "all";
    var rows = requests.filter(function (r) { return filter === "all" || (filter === "awaiting" ? r.status === "requested" : filter === "approved" ? r.status === "token_sent" : r.status === "registered"); });
    return appLayout("admin", "Access requests", "requests",
      '<main class="page">' + pageHead("Access requests", "Approve new customers and issue a one-time registration code.") +
      '<section class="card flush"><div class="tabs">' +
        [["all", "All"], ["awaiting", "Awaiting approval"], ["approved", "Code sent"], ["registered", "Already registered"]].map(function (t) {
          return '<button class="tab' + (filter === t[0] ? " active" : "") + '" data-filter="' + t[0] + '">' + t[1] + " (" + counts[t[0]] + ")</button>";
        }).join("") +
      "</div>" +
      '<div class="table-wrap"><table><thead><tr><th>Email</th><th>Requested</th><th>Status</th><th>Action</th></tr></thead><tbody>' +
        (rows.length ? rows.map(function (r) {
          var action = r.status === "requested"
            ? '<button class="button small-btn" data-approve="' + r.id + '">Approve</button>'
            : '<button class="button ghost small-btn" data-request-state="' + r.id + '">View</button>';
          return "<tr><td><strong>" + esc(r.email) + "</strong></td><td>" + esc(fmtDateTime(r.created_at)) + "</td><td>" + requestStatusLabel(r.status) + "</td><td>" + action + "</td></tr>";
        }).join("") : '<tr><td colspan="4"><div class="empty-state" style="min-height:220px"><div><h3>No matching requests</h3><p class="muted">New requests will appear here after someone enters their email on the portal.</p></div></div></td></tr>') +
      "</tbody></table></div></section></main>");
  }

  function subLabel(sub) {
    return titleCase(String(sub || "none").replace("_", " "));
  }

  function adminAccounts() {
    var accounts = state.adminAccounts || [];
    return appLayout("admin", "Accounts", "accounts",
      '<main class="page">' + pageHead("Customer accounts", "Review subscriptions, quotas, and workspace availability.", '<div class="actions"><button class="button" data-action="admin-add-user">' + icon("plus") + " Add user</button>" +
        '<a class="button secondary" href="#/admin/requests">' + icon("requests") + " New requests</a></div>") +
      '<section class="card flush"><div class="table-tools"><div class="search"><input id="account-search" type="search" placeholder="Search name, email, or username" aria-label="Search accounts"></div></div>' +
      '<div class="table-wrap"><table><thead><tr><th>Customer</th><th>Plan</th><th>Subscription</th><th>Period end</th><th>Quota</th><th></th></tr></thead><tbody>' +
        (accounts.length ? accounts.map(function (a) {
          var badge = a.subscription_status === "active" ? statusBadge("active", "Active")
            : a.subscription_status === "past_due" ? statusBadge("pastdue", "Past due")
            : a.subscription_status === "locked" ? statusBadge("locked", "Locked")
            : a.subscription_status === "canceled" ? statusBadge("cancelled", "Cancelled")
            : statusBadge("pending", "No subscription");
          return "<tr data-account-row=\"" + a.id + "\">" +
            '<td><div class="customer-cell"><span class="avatar">' + esc(initialsOf(a.display_name || a.username)) + '</span><span class="cell-text"><strong>' + esc(a.display_name || cap(a.username)) + "</strong><span>" + esc(a.email) + "</span></span></div></td>" +
            "<td>" + esc(planName(a)) + "</td>" +
            "<td>" + badge + "</td>" +
            "<td>" + esc(fmtDate(a.paid_until)) + "</td>" +
            "<td>" + (a.quota || 1) + "</td>" +
            '<td><button class="button secondary small-btn" data-route="/admin/account/' + a.id + '">View account</button></td></tr>';
        }).join("") : '<tr><td colspan="6"><div class="empty-state" style="min-height:240px"><div><h3>No customer accounts yet</h3><p class="muted">Accounts appear here after registration.</p></div></div></td></tr>') +
      "</tbody></table></div></section></main>");
  }

  function adminAccountPage(id) {
    var a = (state.adminAccounts || []).find(function (x) { return x.id === Number(id); });
    var detail = state.accountCache[id];
    if (!a) return appLayout("admin", "Account details", "accounts", "<main class=\"page\">" + pageHead("Account not found", "This account may have been removed.") + "</main>");
    var instances = (detail && detail.instances) || [];
    var sub = a.subscription_status || "none";
    var subBadge = sub === "active" ? statusBadge("active", "Active")
      : sub === "past_due" ? statusBadge("pastdue", "Past due")
      : sub === "locked" ? statusBadge("locked", "Locked")
      : sub === "canceled" ? statusBadge("cancelled", "Cancelled") : statusBadge("pending", "No subscription");
    var used = instances.filter(function (i) { return i.status !== "deleted"; }).length;
    var quota = a.quota || 1;

    return appLayout("admin", "Account details", "accounts",
      '<main class="page">' + pageHead(a.display_name || cap(a.username), esc(a.email) + " · " + esc(a.username), '<div class="actions">' +
        '<button class="button secondary" data-action="admin-mark-paid" data-id="' + a.id + '">' + icon("check") + " Mark paid</button>" +
        '<button class="button secondary" data-action="admin-attach" data-id="' + a.id + '">' + icon("workspace") + " Attach workspace</button>" +
        '<button class="button secondary" data-action="quota" data-id="' + a.id + '">Change quota</button>' +
        (sub === "locked" ? '<button class="button" data-action="admin-unlock" data-id="' + a.id + '">' + icon("check") + " Switch workspace on</button>"
                          : '<button class="button secondary" data-action="admin-lock" data-id="' + a.id + '">' + icon("warning") + " Switch workspace off</button>") +
        "</div>") +
      '<div class="grid cols-3">' +
        '<section class="card"><div class="card-head"><h2>Customer profile</h2>' + subBadge + "</div>" +
          '<dl class="info-list"><div class="info-row"><dt>Username</dt><dd>' + esc(a.username) + "</dd></div>" +
          '<div class="info-row"><dt>Email</dt><dd>' + esc(a.email) + "</dd></div>" +
          '<div class="info-row"><dt>Signed up</dt><dd>' + esc(fmtDate(a.created_at)) + "</dd></div></dl></section>" +
        '<section class="card"><h2>Subscription</h2><dl class="info-list">' +
          '<div class="info-row"><dt>Plan</dt><dd>' + esc(planName(a)) + "</dd></div>" +
          '<div class="info-row"><dt>Status</dt><dd>' + esc(subLabel(sub)) + "</dd></div>" +
          '<div class="info-row"><dt>Period start</dt><dd>' + esc(fmtDate(a.paid_from)) + "</dd></div>" +
          '<div class="info-row"><dt>Period end</dt><dd>' + esc(fmtDate(a.paid_until)) + "</dd></div>" +
          '<div class="info-row"><dt>Renewal</dt><dd>Automatic</dd></div></dl></section>' +
        '<section class="card"><div class="card-head"><h2>Quota</h2><strong>' + used + " / " + quota + "</strong></div>" +
          '<div class="quota"><div class="bar"><span style="width:' + Math.min(100, quota ? Math.round(used / quota * 100) : 0) + '%"></span></div></div>' +
          '<p class="small muted" style="margin-top:20px">' + (used < quota ? "The customer may provision another workspace." : "Quota fully used.") + "</p>" +
          '<button class="button secondary small-btn" data-action="quota" data-id="' + a.id + '">Change allowance</button></section>' +
      "</div>" +
      '<div class="grid cols-3" style="margin-top:18px"><section class="card span-2"><div class="card-head"><h2>Workspaces</h2>' +
        (used < quota ? '<button class="button secondary small-btn" data-action="admin-add-workspace" data-id="' + a.id + '">Add workspace</button>' : "") + "</div>" +
        '<div class="workspace-list">' +
        (instances.length ? instances.filter(function (i) { return i.status !== "deleted"; }).map(function (i) {
          var badge = i.locked ? statusBadge("off", "Switched off")
            : i.status === "healthy" ? statusBadge("running", "Running")
            : i.status === "provisioning" ? statusBadge("provisioning", "Being set up")
            : i.status === "failed" ? statusBadge("failed", "Setup failed") : statusBadge("neutral", cap(i.status));
          var attachedTag = i.managed === 0 ? ' <span class=\"status info\">Attached</span>' : "";
          return '<article class="workspace-item"><span class="workspace-logo">' + icon("workspace") + "</span>" +
            "<div><h3>" + esc(titleCase(i.stack_name)) + " workspace" + attachedTag + "</h3><p>" + esc(i.domain) + "<br>Env " + esc(i.environment_name || i.environment_id) + " · Port " + esc(i.port) + (i.managed === 0 ? " · password unchanged" : "") + "</p></div>" +
            badge + '<button class="button secondary small-btn" data-action="workspace-actions" data-id="' + i.id + '" data-name="' + esc(i.stack_name) + '">Actions</button></article>';
        }).join("") : '<div class="empty-state" style="min-height:200px"><div><h3>No workspaces yet</h3><p class="muted">The customer has not provisioned a workspace.</p></div></div>') +
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
    }).join("") || '<div class="empty-state" style="min-height:160px"><div><h3>No environments found</h3><p class="muted">The n8n servers could not be listed right now.</p></div></div>';
    return appLayout("admin", "Settings", "settings",
      '<main class="page">' + pageHead("Server environments", "Choose where new customer workspaces are created.", '<button class="button" data-action="save-servers">' + icon("check") + " Save selection</button>") +
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

  /* ================= router ================= */
  function render() {
    modalRoot.innerHTML = "";
    var route = (location.hash || "#/entry").slice(1);
    var parts = route.split("/").filter(Boolean);
    var html = null;

    /* ---- public / auth pages ---- */
    if (route === "/entry") html = entryPage();
    else if (route === "/request") html = requestPage();
    else if (route === "/code") html = codePage();
    else if (route === "/register") html = registerPage();
    else if (route === "/signin") html = signinPage();

    /* ---- customer app ---- */
    else if (route === "/customer/dashboard") html = requireCustomer(function () { return customerDashboard(); });
    else if (route === "/customer/workspaces") html = requireCustomer(function () { return customerWorkspaces(); });
    else if (route === "/customer/billing") html = requireCustomer(function () { return customerBilling(); });
    else if (route === "/customer/support") html = requireCustomer(function () { return supportPage(); });
    else if (route === "/customer/account") html = requireCustomer(function () { return accountPage(); });

    /* ---- admin app ---- */
    else if (route === "/admin/signin") html = state.adminAuthed ? appLayout("admin", "Overview", "overview", '<main class="page"><p class="muted">Signed in.</p></main>') : adminSignin();
    else if (route === "/admin/overview") html = requireAdmin(function () { return adminOverview(); });
    else if (route === "/admin/requests") html = requireAdmin(function () { return adminRequests(); });
    else if (route === "/admin/accounts") html = requireAdmin(function () { return adminAccounts(); });
    else if (parts[0] === "admin" && parts[1] === "account") html = requireAdmin(function () { return adminAccountPage(parts[2]); });
    else if (route === "/admin/maintenance") html = requireAdmin(function () { return maintenancePage(); });
    else if (route === "/admin/settings") html = requireAdmin(function () { return settingsPage(); });
    else html = entryPage();

    appEl.innerHTML = html;
    var h1 = $("h1");
    document.title = (h1 ? h1.textContent : "Portal") + " | SteProTECH";
    window.scrollTo(0, 0);
    afterRender(route);
  }

  function requireCustomer(fn) {
    if (!localStorage.getItem("portal_token")) { navigate("/entry"); return ""; }
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
    if (parts[0] === "customer") { refreshSession().then(after); }
    else if (parts[0] === "admin" && route !== "/admin/signin") { refreshAdminData().then(after); }
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
    var r = state.requests ? JSON.stringify(state.requests) : "";
    var e = state.envOrder.join(",");
    var p = state.plans ? JSON.stringify(state.plans) : "";
    var ev = state.adminEnvs ? JSON.stringify(state.adminEnvs) : "";
    var ul = state.unlinkedStacks ? JSON.stringify(state.unlinkedStacks) : "";
    var route = (location.hash || "").slice(1);
    var m = route.match(/^\/admin\/account\/(\d+)$/);
    var d = m && state.accountCache[m[1]] ? JSON.stringify(state.accountCache[m[1]]) : "";
    return c + "|" + a + "|" + r + "|" + e + "|" + p + "|" + d + "|" + ev + "|" + ul;
  }

  async function refreshSession() {
    var token = localStorage.getItem("portal_token");
    if (!token) { state.session = null; return null; }
    try {
      var data = await api("/me");
      state.session = { token: token, account: data.account, instances: data.instances || [] };
      if (!state.plans.length) await loadPlans();
      return state.session;
    } catch (e) {
      if (e.status === 401) {
        localStorage.removeItem("portal_token");
        state.session = null;
        showToast("Session ended", "Please sign in again.");
        navigate("/entry");
      }
      return null;
    }
  }

  async function refreshAdminData() {
    if (!state.adminAuthed) return;
    try {
      var accts = await api("/admin/accounts");
      state.adminAccounts = accts || [];
      var reqs = await api("/admin/access-requests");
      state.requests = reqs || [];
      // account details for the account page currently open (always fresh)
      var route = (location.hash || "").slice(1);
      var m = route.match(/^\/admin\/account\/(\d+)$/);
      if (m) {
        var id = m[1];
        try {
          var detail = await api("/accounts/" + id);
          state.accountCache[id] = detail;
        } catch (e) { /* public per-account read failed */ }
      }
      var settings = await api("/admin/settings");
      state.envOrder = String(settings.landing_environments || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      if (!state.environmentList.length) {
        try { state.environmentList = await api("/environments"); } catch (e) { state.environmentList = []; }
      }
      try { state.adminEnvs = await api("/admin/environments"); } catch (e) { state.adminEnvs = []; }
    } catch (e) {
      if (e.status === 401) { localStorage.removeItem("admin_token"); state.adminAuthed = false; showToast("Admin session ended", "Please sign in again."); navigate("/admin/signin"); }
    }
  }

  async function loadPlans() {
    try { state.plans = (await api("/plans")).plans || []; } catch (e) { state.plans = []; }
  }

  /* ================= modals / toasts ================= */

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
    var plan = state.plans.find(function (p) { return p.active; }) || {};
    showModal(modalHeader("Continue to secure payment") +
      '<p class="muted">You will leave the SteProTECH portal and complete payment securely on the payment provider.</p>' +
      '<div class="banner">' + icon("shield") + "<div><strong>" + esc(fmtMinor(plan.amount_minor, plan.currency)) + " per year</strong><br><span class=\"small\">Your workspace will start being prepared after payment is confirmed.</span></div></div>" +
      '<div class="actions end" style="margin-top:24px"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-action="payment-continue">Continue to payment ' + icon("external") + "</button></div>");
  }

  function approveModal(id) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(id); });
    if (!r) return;
    showModal(modalHeader("Approve access request?") +
      "<p>Approve <strong>" + esc(r.email) + "</strong> to generate a one-time registration code.</p>" +
      '<div class="consequence" style="background:var(--amber-100)"><strong>What happens next</strong><ul><li>The code is emailed automatically</li><li>It expires after 72 hours</li><li>The same code is shown here once for copying</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-approve="' + id + '">Approve and send code</button></div>');
  }

  function approvalSuccess(data) {
    var r = (state.requests || []).find(function (x) { return x.id === Number(data.request_id); });
    showModal('<div class="center-state"><div class="state-orb success">' + icon("check") + '</div><h2 id="modal-title">Access approved</h2>' +
      '<p class="muted">A one-time code was generated and emailed to <strong>' + esc(r ? r.email : "") + "</strong>.</p>" +
      '<div class="card" style="margin:22px 0"><div class="eyebrow">One-time code</div>' +
      '<div class="mono" style="font-size:1.7rem;letter-spacing:.18em;font-weight:800;margin:8px 0">' + esc(data.token || "") + "</div>" +
      '<button class="button secondary small-btn" data-action="copy-code" data-code="' + esc(data.token || "") + '">' + icon("copy") + " Copy code</button></div>" +
      '<div class="banner success" style="text-align:left">' + icon("check") + "<div><strong>Code emailed</strong><br><span class=\"small\">Expires in 72 hours. This code will not be shown again after closing.</span></div></div>" +
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
      showModal(modalHeader("Mark " + esc(name) + " as paid") +
        '<p class="muted">Record that this customer has paid, with the exact subscription dates. You can backdate the start when the customer already paid before being added here.</p>' +
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
    showModal(modalHeader("Change workspace quota") +
      '<p class="muted">Set how many workspaces this customer may own (1 to 50).</p>' +
      '<div class="field"><label>Workspaces allowed</label><input id="quota-input" type="number" min="1" max="50" value="' + ((a && a.quota) || 1) + '">' +
      '<span class="hint">Raising the quota lets the customer provision more workspaces. Reducing it does not delete existing workspaces.</span></div>' +
      '<div class="actions end" style="margin-top:22px"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-confirm-quota="' + accountId + '">Update quota</button></div>');
  }

  function maintenanceModal() {
    showModal(modalHeader("Run expiry maintenance?") +
      "<p>This will switch off every workspace whose subscription has ended.</p>" +
      '<div class="consequence"><strong>The sweep will:</strong><ul><li>Check every customer account</li><li>Switch off expired workspaces</li><li>Leave active accounts untouched</li><li>Safely confirm already-off workspaces</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button danger" data-action="maintenance-run">Run maintenance</button></div>');
  }

  function addWorkspaceModal() {
    var acc = state.session ? state.session.account : null;
    showModal(modalHeader("Provision another workspace") +
      '<p class="muted">Your account can currently own up to ' + ((acc && acc.quota) || 1) + " workspace(s).</p>" +
      '<div class="consequence safe"><strong>How this works</strong><ul><li>Provisioning uses the password you chose at signup</li><li>Your new workspace gets a fresh address and encryption key</li><li>You will be emailed the access details when it is ready</li></ul></div>' +
      '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
      '<button class="button" data-action="provision-now">Provision workspace</button></div>');
  }

  /* ================= actions / forms ================= */

  async function doPayment() {
    var acc = state.session ? state.session.account : null;
    if (!acc) { navigate("/signin"); return; }
    var id = acc.id;
    try {
      var data = await api("/accounts/" + id + "/checkout", { method: "POST" });
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

  function eventHandlers() {
    document.addEventListener("click", function (event) {
      var route = event.target.closest("[data-route]");
      if (route) { navigate(route.dataset.route); return; }

      var act = event.target.closest("[data-action]");
      var action = act && act.dataset.action;

      if (action === "menu") { state.menuOpen = !state.menuOpen; render(); }
      else if (action === "close-modal" &&
               (event.target === act || !act.classList.contains("modal-backdrop"))) closeModal();
      else if (action === "signout") { localStorage.removeItem("portal_token"); state.session = null; state.provisioningPw = null; stopPolling(); showToast("Signed out", "You have been signed out of the portal."); navigate("/entry"); }
      else if (action === "signout-route") {
        if (currentKind() === "admin") {
          localStorage.removeItem("admin_token"); state.adminAuthed = false;
          state.adminAccounts = []; state.requests = []; state.accountCache = {};
          showToast("Signed out", "Admin session ended."); navigate("/admin/signin");
        } else {
          if (window.confirm("Sign out of the portal?")) { localStorage.removeItem("portal_token"); state.session = null; state.provisioningPw = null; stopPolling(); navigate("/entry"); }
        }
      }
      else if (action === "toggle-password") {
        var inp = document.getElementById(act.dataset.target);
        if (inp) {
          inp.type = inp.type === "password" ? "text" : "password";
          act.innerHTML = icon(inp.type === "password" ? "eye" : "eyeoff");
        }
      }
      else if (action === "payment-continue") { doPayment(); }
      else if (action === "pay-now") paymentModal();
      else if (action === "provision-now") { autoProvision(); }
      else if (action === "provision-confirm") {
        var pv = String((document.getElementById("provision-pw") || {}).value || "");
        closeModal();
        if (!pv) { showToast("Password required", "Enter the password you chose at signup."); autoProvision(); return; }
        state.provisioningPw = pv;
        localStorage.setItem("portal_pw", pv);
        autoProvision();
      }
      else if (action === "add-workspace") addWorkspaceModal();

      /* admin */
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
      else if (action === "admin-add-user") { adminAddUserModal(); }
      else if (action === "admin-mark-paid") { markPaidModal(act.dataset.id); }
      else if (action === "admin-attach") {
        var attId = act.dataset.id;
        (async function () {
          try {
            state.unlinkedStacks = await api("/admin/stacks/unlinked");
          } catch (e) { state.unlinkedStacks = []; }
          attachModal(attId);
        })();
      }
      else if (action === "after-add-attach") {
        closeModal();
        var aaId = act.dataset.id;
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
      else if (action === "lock-workspace") confirmLock(act.dataset.id);
      else if (action === "unlock-workspace") confirmUnlock(act.dataset.id);
      else if (action === "reset-password") resetPasswordModal(act.dataset.id);
      else if (action === "admin-add-workspace") {
        showModal(modalHeader("Add workspace for customer") + "<p>Provision another workspace for this customer. Access details are emailed to them.</p>" +
          '<div class="actions end"><button class="button secondary" data-action="close-modal">Cancel</button>' +
          '<button class="button" data-confirm-admin-provision="' + act.dataset.id + '">Provision workspace</button></div>');
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
          showModal(modalHeader(isRegistered ? "Already registered" : "Code already sent") +
            '<div class="banner ' + (isRegistered ? "" : "success") + '">' + icon(isRegistered ? "account" : "check") +
            "<div><strong>No duplicate code was created</strong><br><span class=\"small\">" + esc(rr.email) + " " +
            (isRegistered ? "already has a customer portal account." : "was previously approved and emailed a code.") + "</span></div></div>" +
            '<button class="button" style="margin-top:20px" data-action="close-modal">Done</button>');
        }
      }

      var confirmApprove = event.target.closest("[data-confirm-approve]");
      if (confirmApprove) {
        var rid = confirmApprove.dataset.confirmApprove;
        (async function () {
          try {
            var data = await api("/admin/access-requests/" + rid + "/token", { method: "POST" });
            closeModal();
            approvalSuccess(data);
            refreshAdminData();
          } catch (err) { showToast("Could not approve", err.message); }
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
      var cReset = event.target.closest("[data-confirm-reset]");
      if (cReset) {
        closeModal();
        adminAction("/instances/" + cReset.dataset.confirmReset + "/reset-password", "Password reset started", "The new n8n password will be emailed to the customer.").then(refreshAdminData);
      }
      var cQuota = event.target.closest("[data-confirm-quota]");
      if (cQuota) {
        var quotaVal = parseInt(($("#quota-input") || {}).value || "1", 10);
        if (!quotaVal || quotaVal < 1 || quotaVal > 50) { showToast("Invalid quota", "Quota must be 1 to 50."); return; }
        closeModal();
        (async function () {
          try {
            await api("/admin/accounts/" + cQuota.dataset.confirmQuota + "/quota", { method: "PUT", body: { quota: quotaVal } });
            showToast("Quota updated", "The customer may now own " + quotaVal + " workspace(s).");
            refreshAdminData();
          } catch (err) { showToast("Quota update failed", err.message); }
        })();
      }
      var cMarkPaid = event.target.closest("[data-confirm-mark-paid]");
      if (cMarkPaid) {
        var mpId = cMarkPaid.dataset.confirmMarkPaid;
        var paidFrom = (document.getElementById("paid-from") || {}).value;
        var paidUntil = (document.getElementById("paid-until") || {}).value;
        function toEpoch(dateStr) { var d = new Date(dateStr + "T23:59:59"); return Math.floor(d.getTime() / 1000); }
        if (!paidFrom || !paidUntil) { showToast("Dates required", "Pick both the start and expiry dates."); return; }
        var until = toEpoch(paidUntil);
        var from = toEpoch(paidFrom);
        closeModal();
        (async function () {
          try {
            var data = await api("/admin/accounts/" + mpId + "/mark-paid", { method: "POST", body: { paid_until: until, paid_from: from } });
            showToast(data.subscription_status === "active" ? "Marked as paid" : "Recorded as expired",
              data.subscription_status === "active" ? "Subscription active until " + fmtDate(until) + "." : "Expiry was in the past; the workspace stays off.");
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
        closeModal();
        (async function () {
          try {
            await api("/accounts/" + cAdminProv.dataset.confirmAdminProvision + "/provision", { method: "POST", body: {} });
            showToast("Provisioning queued", "The customer will receive an email when the workspace is ready.");
            refreshAdminData();
          } catch (err) { showToast("Provisioning failed", err.message); }
        })();
      }

      /* filters */
      var filter = event.target.closest("[data-filter]");
      if (filter) { state.accessFilter = filter.dataset.filter; render(); }

      /* (server reorder removed - placement is now checkbox selection) */
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
          else if (r.action === "token") navigate("/code");
          else navigate("/request"); // requested | waiting -> same screen copy
        }).catch(function (err) { showToast("Could not continue", err.message); });
      }
      else if (type === "code") {
        var token = String(new FormData(form).get("token") || "").trim().toUpperCase();
        if (!token) { showToast("Enter the code", "Paste or type the code from your email."); return; }
        api("/auth/verify-token", { method: "POST", body: { email: state.gateEmail, token: token } }).then(function () {
          state.gateToken = token;
          navigate("/register");
        }).catch(function (err) { showToast("Code not accepted", err.message); });
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
        api("/auth/login", { method: "POST", body: { email: String(f2.get("email") || "").trim().toLowerCase(), password: String(f2.get("password") || "") } }).then(function (r) {
          localStorage.setItem("portal_token", r.token);
          state.session = { token: r.token, account: r.account, instances: [] };
          loadPlans();
          navigate("/customer/dashboard");
        }).catch(function (err) { showToast("Sign in failed", err.message); });
      }
      else if (type === "admin-signin") {
        var pwA = String(new FormData(form).get("password") || "");
        var btnA = form.querySelector("button[type=submit]");
        if (btnA) btnA.disabled = true;
        api("/admin/login", { method: "POST", body: { password: pwA } }).then(function (r) {
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
    });
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
  function boot() {
    loadPlans();
    state.adminAuthed = !!localStorage.getItem("admin_token");
    var portalToken = localStorage.getItem("portal_token");
    // handle payment return: ?status=success after Paystack redirect
    var params = new URLSearchParams(location.search);
    if (params.get("status") === "success" && portalToken) {
      history.replaceState(null, "", location.pathname + location.hash);
      if (params.get("checkout") !== "mock") {
        refreshSession().then(function () {
          showToast("Payment received", "Your subscription is active. Preparing your workspace...");
          autoProvision();
          navigate("/customer/dashboard");
        });
      }
    }
    eventHandlers();
    window.addEventListener("hashchange", render);
    render();
  }

  boot();
})();
