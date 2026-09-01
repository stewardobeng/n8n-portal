/* n8n Portal UI — CloudPanel-style. */
(function () {
  "use strict";

  var API = "/api/v1";
  var state = { account: null, instances: [], plan: null };
  var pollTimer = null;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function msg(el, text, cls) { el.textContent = text || ""; el.className = "msg" + (cls ? " " + cls : ""); }
  function fmtAmount(minor, cur) { return (minor / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + cur; }
  function fmtDate(ts) { if (!ts) return "—"; return new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
  function fmtDateTime(ts) { if (!ts) return "—"; return new Date(ts * 1000).toLocaleString(); }

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
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      if (typeof detail === "object") detail = JSON.stringify(detail);
      var err = new Error(detail || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ---------- gate (email -> login / token / register) ---------- */
  function showGateStep(name) {
    ["email", "login", "token", "waiting", "register"].forEach(function (s) {
      $("gate-step-" + s).classList.add("hidden");
    });
    $("gate-step-" + name).classList.remove("hidden");
  }
  function resetGate() {
    state.gateEmail = null;
    state.gateToken = null;
    $("gate-email").value = "";
    showGateStep("email");
  }
  function enterApp(account, token) {
    state.account = account;
    if (token) localStorage.setItem("portal_token", token);
    localStorage.setItem("account_id", String(account.id));
    $("gate-email").value = "";
    document.querySelector(".header").classList.remove("hidden");
    document.body.classList.remove("gate-mode");
    $("view-gate").classList.add("hidden");
    $("app-shell").classList.remove("hidden");
    showView("dashboard");
  }
  function signOut() {
    localStorage.removeItem("portal_token");
    localStorage.removeItem("account_id");
    state.account = null; state.instances = [];
    document.querySelector(".header").classList.add("hidden");
    document.body.classList.add("gate-mode");
    $("app-shell").classList.add("hidden");
    $("view-gate").classList.remove("hidden");
    resetGate();
  }

  async function gateSubmitEmail() {
    var email = $("gate-email").value.trim();
    msg($("gate-msg"), "");
    if (!email) { msg($("gate-msg"), "Enter your email address.", "err"); return; }
    try {
      var r = await api("/auth/check", { method: "POST", body: { email: email } });
      state.gateEmail = r.email;
      if (r.action === "login") showGateStep("login");
      else if (r.action === "token") { $("gate-token").value = ""; msg($("token-msg"), ""); showGateStep("token"); }
      else if (r.action === "waiting") {
        $("waiting-email").textContent = r.email;
        showGateStep("waiting");
      } else { // requested
        $("waiting-email").textContent = r.email;
        showGateStep("waiting");
      }
    } catch (err) { msg($("gate-msg"), err.message, "err"); }
  }

  async function gateSubmitLogin() {
    var pw = $("login-password").value;
    msg($("login-msg"), "");
    if (!pw) { msg($("login-msg"), "Enter your password.", "err"); return; }
    try {
      var r = await api("/auth/login", { method: "POST", body: { email: state.gateEmail, password: pw } });
      enterApp(r.account, r.token);
    } catch (err) { msg($("login-msg"), err.message, "err"); }
  }

  async function gateSubmitToken() {
    var tok = $("gate-token").value.trim();
    msg($("token-msg"), "");
    if (!tok) { msg($("token-msg"), "Enter the token from your email.", "err"); return; }
    try {
      var r = await api("/auth/verify-token", { method: "POST", body: { email: state.gateEmail, token: tok } });
      state.gateToken = tok;
      $("reg-email").value = state.gateEmail;
      $("reg-first").value = ""; $("reg-last").value = ""; $("reg-username").value = ""; $("reg-password").value = "";
      msg($("register-msg"), "");
      showGateStep("register");
    } catch (err) { msg($("token-msg"), err.message, "err"); }
  }

  async function gateSubmitRegister() {
    var first = $("reg-first").value.trim();
    var last = $("reg-last").value.trim();
    var username = $("reg-username").value.trim() || null;
    var pw = $("reg-password").value;
    msg($("register-msg"), "");
    if (!first || !last || !pw) { msg($("register-msg"), "Fill in all required fields.", "err"); return; }
    try {
      var r = await api("/accounts", { method: "POST", body: {
        email: state.gateEmail, username: username,
        first_name: first, last_name: last,
        password: pw, access_token: state.gateToken,
      }});
      enterApp(r.account, r.token);
    } catch (err) { msg($("register-msg"), err.message, "err"); }
  }

  $("gate-next").addEventListener("click", gateSubmitEmail);
  $("gate-email").addEventListener("keydown", function (e) { if (e.key === "Enter") gateSubmitEmail(); });
  $("login-submit").addEventListener("click", gateSubmitLogin);
  $("login-password").addEventListener("keydown", function (e) { if (e.key === "Enter") gateSubmitLogin(); });
  $("token-submit").addEventListener("click", gateSubmitToken);
  $("gate-token").addEventListener("keydown", function (e) { if (e.key === "Enter") gateSubmitToken(); });
  $("register-submit").addEventListener("click", gateSubmitRegister);
  $("login-back").addEventListener("click", function (e) { e.preventDefault(); resetGate(); });
  $("token-back").addEventListener("click", function (e) { e.preventDefault(); resetGate(); });
  $("waiting-back").addEventListener("click", function (e) { e.preventDefault(); resetGate(); });
  $("register-back").addEventListener("click", function (e) { e.preventDefault(); resetGate(); });

  /* ---------- navigation ---------- */
  function showView(name) {
    document.querySelectorAll(".view").forEach(function (v) { v.classList.add("hidden"); });
    var view = $("view-" + name);
    if (view) view.classList.remove("hidden");
    document.querySelectorAll("[data-view]").forEach(function (n) {
      n.classList.toggle("active", n.dataset.view === name);
    });
    if (name === "dashboard") loadDashboard();
    if (name === "instances") loadInstances();
    if (name === "plans") loadPlans();
    if (name === "admin") loadAdmin();
  }

  /* ---------- theme (html.dark, like the real CloudPanel) ---------- */
  $("theme-toggle").addEventListener("click", function (e) {
    e.preventDefault();
    document.documentElement.classList.toggle("dark");
    localStorage.setItem("cp_theme", document.documentElement.classList.contains("dark") ? "dark" : "light");
  });
  if (localStorage.getItem("cp_theme") === "dark") document.documentElement.classList.add("dark");

  /* ================= ACCOUNT / SIGNUP ================= */

  function ensureAccountId() {
    return state.account ? state.account.id : (localStorage.getItem("account_id") || null);
  }

  async function loadAccount() {
    if (!localStorage.getItem("portal_token")) return null;
    try {
      var data = await api("/me");
      state.account = data.account;
      state.instances = data.instances || [];
      return data;
    } catch (e) {
      if (e.status === 401) { signOut(); }
      return null;
    }
  }

  /* ================= DASHBOARD ================= */

  async function loadDashboard() {
    await loadAccount();
    renderStats();
    var body = $("dash-body");
    var provBtn = $("dash-provision");
    if (provBtn) provBtn.style.display = "none";

    if (!state.account) {
      body.innerHTML = '<div class="status-line"><span class="pill pending">Not signed in</span></div>' +
        '<p class="form-text">Sign in to see your workspace.</p>';
      return;
    }
    var acc = state.account;
    var inst = state.instances[0];

    if (acc.subscription_status === "active" && inst && inst.status === "healthy") {
      var locked = inst.locked;
      body.innerHTML =
        '<div class="status-line"><span class="pill ' + (locked ? "locked" : "healthy") + '">' +
        (locked ? "Locked — your subscription has expired. Renew to restart your workspace" : "Active") + "</span></div>" +
        '<div class="meta-grid">' +
        '<div><span class="k">Workspace</span><span class="v"><a href="https://' + esc(inst.domain) + '/" target="_blank" rel="noopener">' + esc(inst.domain) + "</a></span></div>" +
        '<div><span class="k">Port</span><span class="v">' + inst.port + "</span></div>" +
        '<div><span class="k">Subscription</span><span class="v">' + statusLabel(acc.subscription_status) + " · paid until " + fmtDate(acc.paid_until) + "</span></div>" +
        "</div>" +
        '<p class="form-text">Sign in with your email and the password you chose. ' +
        "Use Forgot password on the sign-in page if you ever need to reset it.</p>";
    } else if (acc.subscription_status === "active") {
      provBtn.style.display = "inline-block";
      body.innerHTML =
        '<div class="status-line"><span class="pill provisioning">Provisioning…</span></div>' +
        '<p class="form-text">Your subscription is active. Your instance is being created — this takes a couple of minutes. ' +
        "Refresh to check progress, or click Provision now if it has not started.</p>";
    } else {
      // subscription required first
      body.innerHTML =
        '<div class="status-line"><span class="pill pending">Subscription required</span></div>' +
        '<p>Your account is ready. Subscribe to the annual plan to get your n8n workspace.</p>' +
        '<button class="btn btn-blue" id="dash-checkout">Pay now — ' + (state.plan ? fmtAmount(state.plan.amount_minor, state.plan.currency) : "") + " / year</button>" +
        '<p class="msg" id="dash-checkout-msg"></p>';
      var co = $("dash-checkout");
      if (co) co.addEventListener("click", startCheckout);
    }
    if (provBtn) provBtn.addEventListener("click", function () { provisionNow(); });
  }

  function renderStats() {
    var acc = state.account;
    var inst = state.instances && state.instances[0];
    var s = "";
    s += statHtml("Account", acc ? acc.username : "—", acc ? acc.email : "Sign up to begin");
    s += statHtml("Subscription", acc ? statusLabel(acc.subscription_status) : "—", acc && acc.paid_until ? "paid until " + fmtDate(acc.paid_until) : "annual plan");
    s += statHtml("Instance", inst ? (inst.locked ? "Locked" : inst.status) : "—", inst ? "port " + inst.port : "not yet provisioned");
    s += statHtml("Plan", state.plan ? fmtAmount(state.plan.amount_minor, state.plan.currency) : "—", state.plan ? state.plan.interval : "per year");
    $("dash-stats").innerHTML = s;
  }

  function statHtml(k, v, sub) {
    return '<div class="stat-box"><div class="stat-title">' + esc(k) + '</div>' +
           '<div class="stat-value">' + esc(v) + "</div>" +
           (sub ? '<div class="stat-sub">' + esc(sub) + "</div>" : "") + "</div>";
  }

  /* ---------- payment ---------- */

  async function startCheckout() {
    var id = ensureAccountId();
    if (!id) return;
    var coBtn = $("dash-checkout");
    if (coBtn) { coBtn.disabled = true; }
    msg($("dash-checkout-msg") || $("sub-msg"), "", "");
    try {
      var data = await api("/accounts/" + id + "/checkout", { method: "POST" });
      if (data.gateway === "mock") {
        // E2E mock mode: simulate the payment immediately (charge.success)
        var webhook = await api("/webhook/mock", {
          method: "POST",
          body: { mock: true, type: "charge.success", data: { metadata: { account_id: String(id) } } },
        });
        msg($("dash-checkout-msg") || $("sub-msg"),
          "Mock payment succeeded. Subscription active.", "ok");
        loadAccount().then(function () { loadDashboard(); loadInstances(); loadPlans(); });
      } else {
        window.location.href = data.url; // Paystack / Stripe hosted checkout
      }
    } catch (err) {
      msg($("dash-checkout-msg") || $("sub-msg"), err.message, "err");
      if (coBtn) coBtn.disabled = false;
    }
  }

  async function provisionNow() {
    var id = ensureAccountId();
    if (!id) return;
    var btn = $("dash-provision");
    btn.disabled = true;
    var pw = storedPassword();
    if (!pw) {
      pw = window.prompt("Enter the password you chose at signup (used to create your n8n owner account):");
      if (!pw) { btn.disabled = false; return; }
      localStorage.setItem("portal_pw", pw);
    }
    try {
      await api("/accounts/" + id + "/provision", { method: "POST", body: { password: pw } });
      startPolling();
      loadDashboard();
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  function storedPassword() {
    return localStorage.getItem("portal_pw") || "";
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      loadAccount().then(function () { loadDashboard(); loadInstances(); });
    }, 12000);
  }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  /* ================= INSTANCES ================= */

  async function loadInstances() {
    await loadAccount();
    var tbody = $("instances-tbody");
    if (!state.account) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">Sign up to create your first instance.</td></tr>';
      return;
    }
    if (!state.instances.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">No instances yet. Subscribe and provision to get started.</td></tr>';
      return;
    }
    tbody.innerHTML = state.instances.map(function (i) {
      var st = i.locked ? "locked" : i.status;
      return "<tr>" +
        "<td><b>" + esc(i.stack_name) + "</b></td>" +
        '<td><span class="pill ' + pillClass(st) + '">' + esc(st) + "</span></td>" +
        '<td><span class="pill ' + pillClass(state.account.subscription_status) + '">' + statusLabel(state.account.subscription_status) + "</span></td>" +
        '<td><a href="https://' + esc(i.domain) + '/" target="_blank" rel="noopener">' + esc(i.domain) + "</a></td>" +
        '<td class="ta-r"><button class="btn btn-sm" data-domain="' + esc(i.domain) + '">Open</button></td>' +
        "</tr>";
    }).join("");
    tbody.querySelectorAll("button[data-domain]").forEach(function (b) {
      b.addEventListener("click", function () { window.open("https://" + b.dataset.domain + "/", "_blank"); });
    });
  }

  $("inst-new").addEventListener("click", function () {
    showView("plans");
  });

  /* ================= PLANS ================= */

  async function loadPlans() {
    try { state.plan = await api("/plan"); } catch (e) {}
    renderPlanGrid();
    var subBody = $("sub-body");
    var acc = state.account;
    if (!acc) {
      subBody.innerHTML = '<p class="color-grey">Create an account to subscribe.</p>';
      return;
    }
    subBody.innerHTML =
      '<div class="meta-grid">' +
      '<div><span class="k">Status</span><span class="v">' + statusLabel(acc.subscription_status) + "</span></div>" +
      '<div><span class="k">Paid until</span><span class="v">' + fmtDate(acc.paid_until) + "</span></div>" +
      '<div><span class="k">Gateway</span><span class="v">' + esc(state.plan ? state.plan.gateway : "—") + "</span></div>" +
      "</div>" +
      '<p class="form-text">Annual plan, auto-renews yearly. When your subscription expires, ' +
      "your instance is stopped automatically until the renewal payment succeeds.</p>" +
      (acc.subscription_status === "active"
        ? '<p class="msg ok">Subscription active.</p>'
        : '<button class="btn btn-blue" id="sub-checkout">Pay now — ' +
          (state.plan ? fmtAmount(state.plan.amount_minor, state.plan.currency) : "") + " / year</button>" +
          '<p class="msg" id="sub-msg"></p>');
    var co = $("sub-checkout");
    if (co) co.addEventListener("click", startCheckout);
  }

  function renderPlanGrid() {
    var grid = $("plan-grid");
    try {
      api("/plans").then(function (data) {
        var plans = data.plans || [];
        var html = plans.map(function (p, i) {
          var featured = p.active ? " featured" : "";
          var badge = p.active
            ? '<span class="plan-badge">Most popular</span>'
            : '<span class="plan-badge plan-badge-off">Coming soon</span>';
          var btn = p.active
            ? '<button class="btn btn-blue" id="plan-subscribe">Subscribe</button>'
            : '<button class="btn btn-gray" disabled>Not available yet</button>';
          var feats = p.active
            ? ["Dedicated n8n workspace", "Your own subdomain with SSL",
               "Owner account + email credentials", "Auto-renewal, instance stops on expiry"]
            : ["Everything in the GHS 300 plan", "Higher capacity compose (coming later)",
               "Priority support"];
          return '<div class="plan-card' + featured + '">' +
            badge +
            '<div class="p-name">' + esc(p.name) + "</div>" +
            '<div class="p-price">' + fmtAmount(p.amount_minor, p.currency) + '<small> / year</small></div>' +
            '<div class="p-per">Billed annually · auto-renews</div>' +
            "<ul>" + feats.map(function (f) { return "<li>" + f + "</li>"; }).join("") + "</ul>" +
            btn +
            "</div>";
        }).join("");
        grid.innerHTML = html || '<p class="color-grey">Plan information unavailable.</p>';
        var b = $("plan-subscribe");
        if (b) b.addEventListener("click", function () {
          if (!ensureAccountId()) { showView("dashboard"); return; }
          startCheckout();
        });
      }).catch(function () {
        grid.innerHTML = '<p class="color-grey">Plan information unavailable.</p>';
      });
    } catch (e) {
      grid.innerHTML = '<p class="color-grey">Plan information unavailable.</p>';
    }
  }

  /* ================= ADMIN ================= */

  function adminAuthed() { return !!localStorage.getItem("admin_token"); }

  async function loadAdmin() {
    if (!adminAuthed()) {
      $("admin-login-wrap").classList.remove("hidden");
      $("admin-panel").classList.add("hidden");
      return;
    }
    $("admin-login-wrap").classList.add("hidden");
    $("admin-panel").classList.remove("hidden");
    try {
      var envs = await api("/admin/settings");
      $("landing-envs").value = envs.landing_environments;
      var accts = await api("/admin/accounts");
      renderAdminAccounts(accts);
      renderAccessRequests();
    } catch (err) {
      msg($("accounts-msg"), err.message, "err");
      if (String(err.message).toLowerCase().indexOf("token") !== -1 || err.status === 401) {
        localStorage.removeItem("admin_token");
        loadAdmin();
      }
    }
  }

  $("admin-login-btn").addEventListener("click", async function () {
    var btn = $("admin-login-btn");
    btn.disabled = true;
    msg($("admin-login-msg"), "");
    try {
      var data = await api("/admin/login", { method: "POST", body: { password: $("admin-password").value } });
      localStorage.setItem("admin_token", data.token);
      loadAdmin();
    } catch (err) {
      msg($("admin-login-msg"), err.message, "err");
    } finally { btn.disabled = false; }
  });

  $("save-envs-btn").addEventListener("click", async function () {
    msg($("envs-msg"), "");
    try {
      var data = await api("/admin/settings", { method: "PUT", body: { landing_environments: $("landing-envs").value.trim() } });
      msg($("envs-msg"), "Saved: environments " + data.landing_environments + ".", "ok");
    } catch (err) { msg($("envs-msg"), err.message, "err"); }
  });

  $("sweep-btn").addEventListener("click", async function () {
    msg($("accounts-msg"), "");
    try {
      var data = await api("/admin/billing/sweep", { method: "POST" });
      msg($("accounts-msg"), "Sweep done. Locked: " + (data.locked || []).length + " account(s).", "ok");
      loadAdmin();
    } catch (err) { msg($("accounts-msg"), err.message, "err"); }
  });

  $("sweep-expired-btn").addEventListener("click", async function () {
    if (!window.confirm("Stop instances whose subscription has expired? Their containers will shut down now.")) return;
    msg($("accounts-msg"), "");
    try {
      var data = await api("/admin/billing/sweep-expired", { method: "POST" });
      msg($("accounts-msg"), "Stopped " + (data.locked || []).length + " expired instance(s).", "ok");
      loadAdmin();
    } catch (err) { msg($("accounts-msg"), err.message, "err"); }
  });

  function renderAdminAccounts(accts) {
    var tbody = $("admin-tbody");
    if (!accts || !accts.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No accounts yet.</td></tr>';
      return;
    }
    tbody.innerHTML = accts.map(function (a) {
      var lockBtn = a.subscription_status === "locked"
        ? '<button class="btn btn-sm btn-blue" data-unlock="' + a.id + '">Unlock</button>'
        : '<button class="btn btn-sm btn-red" data-lock="' + a.id + '">Lock</button>';
      var quota = (a.quota || 1);
      return "<tr>" +
        "<td><b>" + esc(a.username) + "</b><br><span class=\"color-grey\">" + esc(a.email) + "</span></td>" +
        '<td><span class="pill ' + pillClass(a.status) + '">' + esc(a.status) + "</span></td>" +
        '<td><span class="pill ' + pillClass(a.subscription_status) + '">' + statusLabel(a.subscription_status) + "</span></td>" +
        "<td>" + fmtDate(a.paid_until) + "</td>" +
        '<td><span class="quota-n" data-quota-for="' + a.id + '">' + quota + "</span>" +
        ' <button class="btn btn-sm" data-quota-edit="' + a.id + '" title="Change instance quota">Edit</button></td>' +
        '<td class="ta-r">' + lockBtn + "</td>" +
        "</tr>";
    }).join("");
    tbody.querySelectorAll("[data-lock]").forEach(function (b) {
      b.addEventListener("click", function () { adminSetLock(b.dataset.lock, true); });
    });
    tbody.querySelectorAll("[data-unlock]").forEach(function (b) {
      b.addEventListener("click", function () { adminSetLock(b.dataset.unlock, false); });
    });
    tbody.querySelectorAll("[data-quota-edit]").forEach(function (b) {
      b.addEventListener("click", function () { adminEditQuota(b.dataset.quotaEdit); });
    });
  }

  async function adminEditQuota(accountId) {
    var el = document.querySelector('[data-quota-for="' + accountId + '"]');
    var val = window.prompt("Instance quota for account " + accountId + " (1-50):",
                            el ? el.textContent : "1");
    if (!val) return;
    val = parseInt(val, 10);
    if (!val || val < 1 || val > 50) { msg($("accounts-msg"), "Quota must be a number from 1 to 50.", "err"); return; }
    msg($("accounts-msg"), "");
    try {
      await api("/admin/accounts/" + accountId + "/quota", { method: "PUT", body: { quota: val } });
      msg($("accounts-msg"), "Quota for account " + accountId + " set to " + val + ".", "ok");
      loadAdmin();
    } catch (err) { msg($("accounts-msg"), err.message, "err"); }
  }

  async function adminSetLock(accountId, lock) {
    if (!window.confirm((lock ? "Lock" : "Unlock") + " this account's instance?")) return;
    msg($("accounts-msg"), "");
    try {
      await api("/admin/accounts/" + accountId + (lock ? "/lock" : "/unlock"), { method: "POST" });
      msg($("accounts-msg"), (lock ? "Locked" : "Unlocked") + " account " + accountId + ".", "ok");
      loadAdmin();
    } catch (err) { msg($("accounts-msg"), err.message, "err"); }
  }

  /* ================= ADMIN: access requests ================= */

  async function renderAccessRequests() {
    var tbody = $("requests-tbody");
    msg($("requests-msg"), "");
    try {
      var reqs = await api("/admin/access-requests");
      if (!reqs || !reqs.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No access requests yet.</td></tr>';
        return;
      }
      tbody.innerHTML = reqs.map(function (r) {
        var btn = "";
        if (r.status === "requested") {
          btn = '<button class="btn btn-sm btn-blue" data-issue="' + r.id + '" data-email="' + esc(r.email) + '">Generate token &amp; send</button>';
        } else if (r.status === "token_sent") {
          btn = '<button class="btn btn-sm btn-blue" data-issue="' + r.id + '" data-email="' + esc(r.email) + '">Resend token</button>';
        } else {
          btn = '<span class="color-green">Registered</span>';
        }
        var st = r.status === "registered" ? "registered"
          : r.status === "token_sent" ? "token_sent" : "requested";
        return "<tr>" +
          "<td><b>" + esc(r.email) + "</b></td>" +
          '<td><span class="pill ' + pillClass(st) + '">' + statusLabel(r.status) + "</span></td>" +
          "<td>" + fmtDateTime(r.created_at) + "</td>" +
          '<td class="ta-r">' + btn + "</td>" +
          "</tr>";
      }).join("");
      tbody.querySelectorAll("[data-issue]").forEach(function (b) {
        b.addEventListener("click", function () { issueAccessToken(b.dataset.issue, b.dataset.email); });
      });
    } catch (err) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">' + esc(err.message) + "</td></tr>";
    }
  }

  async function issueAccessToken(requestId, email) {
    if (!window.confirm("Generate an access token for " + email + " and email it to them?")) return;
    msg($("requests-msg"), "");
    try {
      var data = await api("/admin/access-requests/" + requestId + "/token", { method: "POST" });
      msg($("requests-msg"), "Token sent to " + data.email + ". Token: " + data.token + " (valid " + data.expires_hours + "h).", "ok");
      renderAccessRequests();
    } catch (err) { msg($("requests-msg"), err.message, "err"); }
  }

  /* ================= helpers ================= */

  function statusLabel(s) {
    return String(s || "none").replace(/_/g, " ");
  }
  function pillClass(s) {
    s = String(s || "none");
    if (["healthy", "provisioned", "active", "token_sent"].indexOf(s) !== -1) return "active";
    if (["pending", "none", "past_due", "provisioning", "requested", "waiting"].indexOf(s) !== -1) return "past_due";
    if (["locked", "canceled", "failed", "unpaid", "registered"].indexOf(s) !== -1) return "locked";
    return "pending";
  }

  /* ================= boot ================= */
  document.querySelectorAll("[data-view]").forEach(function (n) {
    n.addEventListener("click", function (e) {
      e.preventDefault();
      location.hash = n.dataset.view;
      showView(n.dataset.view);
    });
  });

  $("user-chip").addEventListener("click", function (e) {
    e.preventDefault();
    if (window.confirm("Sign out of the portal?")) signOut();
  });

  (async function boot() {
    try { state.plan = await api("/plan"); } catch (e) {}
    var authed = false;
    if (localStorage.getItem("portal_token")) {
      authed = await loadAccount();
    }
    if (authed) {
      document.body.classList.remove("gate-mode");
      document.querySelector(".header").classList.remove("hidden");
      $("view-gate").classList.add("hidden");
      $("app-shell").classList.remove("hidden");
      var hash = (location.hash || "#dashboard").slice(1);
      showView(["dashboard", "instances", "plans", "admin"].indexOf(hash) !== -1 ? hash : "dashboard");
      if (location.search.indexOf("status=success") !== -1) { showView("dashboard"); startPolling(); }
    } else {
      // not signed in -> the email gate is the first page (no header, like CloudPanel login)
      document.querySelector(".header").classList.add("hidden");
      document.body.classList.add("gate-mode");
      $("view-gate").classList.remove("hidden");
      $("app-shell").classList.add("hidden");
      resetGate();
    }
  })();
})();
