# SteProTECH n8n Self-Service Portal — Handover & Developer Guide

**Document version:** 1.0 — 2026-09-01
**Repo:** `github.com/stewardobeng/n8n-portal` (private) — branch `main`
**Owner / product decision-maker:** Steward Obeng (SteProTECH). Product requirements
below are HIS decisions and must not be silently changed.

> Read this whole document before touching the code or the infrastructure. It
> captures the architecture, every API, the non-obvious operational quirks
> (learned the hard way against live systems), and Steward's explicit product
> rules. The codebase is small (~5,000 LOC) but sits on real multi-tenant
> infrastructure where a mistake can take down paying customers.

---

## 1. What this project is

A **self-service client portal** where a paying customer gets their own dedicated
**n8n automation workspace** on SteProTECH's infrastructure:

1. A visitor enters only an **email** on the portal's first page.
2. An **admin** approves the request and issues a one-time **access token**
   (admin-gated onboarding — no open signup).
3. The person registers (email + username + first/last name + chosen password)
   and **pays first** (annual subscription; Paystack in Ghana cedis is the live
   primary gateway; Stripe is implemented but not yet enabled; a mock gateway
   makes the whole flow E2E-testable).
4. Only after payment is verified does the backend **auto-provision** a dedicated
   n8n instance: a unique subdomain (`<username>.steprotech.com`), a unique host
   port, a Portainer stack from one canonical compose template, an
   Nginx Proxy Manager (NPM) proxy host, a fresh Let's Encrypt certificate, and
   an **n8n owner account auto-created with the customer's chosen password** so
   login works on day one.
5. **Lifecycle is fully automated**: when the annual subscription ends, the
   instance container is **STOPPED** (nothing reachable — not even
   forgot-password); a successful renewal **starts it again**, and the customer
   logs back in with the same password they always had.

The portal UI deliberately **mimics CloudPanel** (no sidebar; 75px white header
with horizontal top-nav; `#f9fafb` app background; `#f3f3f3` login background;
`#267ddd` accent; label-outline status badges) — Steward's explicit design
requirement.

---

## 2. System architecture

```
                    ┌─────────────────────────────────────────────┐
                    │  Public internet                            │
                    └───────┬─────────────────────┬───────────────┘
                            │                     │
              portal.steprotech.com       <username>.steprotech.com  (wildcard DNS → control host)
                            │                     │
        ┌───────────────────▼─────────────────────▼──────────────────┐
        │  CONTROL HOST  — 137.131.55.194 (env 3 in Portainer)       │
        │                                                            │
        │  Nginx Proxy Manager (:80/:443 UI :81)                     │
        │    ├── proxy host "portal"        → 127.0.0.1:8788         │
        │    └── proxy host per tenant      → <env-ip>:<tenant-port> │
        │                                                            │
        │  n8n-portal-backend (Docker stack id 72, :8788)            │
        │    FastAPI + SQLite (volume portal_data → /data/portal.db) │
        │    + static SPA UI served at /                             │
        │                                                            │
        │  Portainer CE (:9000, API token auth, X-API-Key header)    │
        └───────┬────────────────────────────────────────────────────┘
                │ Portainer API: create/stop/start/delete stacks,
                │   per-environment queries
                ▼
   ┌─────────────────────────┐   ┌──────────────────────────┐
   │ ENV 8 — n8n-cloud 2     │   │ ENV 4 / ENV 9 (fallback) │
   │ 129.146.2.18            │   │ other n8n hosts          │
   │ beta, gamma, delta, …   │   └──────────────────────────┘
   │ each = one n8n stack    │
   └─────────────────────────┘

   Payment side:
   Paystack (LIVE primary, GHS) ─ webhooks ─▶ /api/v1/webhook/paystack
   Stripe   (implemented, dormant) ────────▶ /api/v1/webhook/stripe
   Mock     (E2E)                          ▶ /api/v1/webhook/mock
   SMTP     purelymail (admin@steprotech.com) — welcome / reset / token emails
```

**Three moving parts, all driven by the backend over local APIs:**

| Part | Role | How the backend talks to it |
|---|---|---|
| **Portainer CE 2.39.6** | Creates/stops/starts/deletes per-tenant compose stacks on the chosen environment host | REST API `:9000`, access token sent as **`X-API-Key`** header (Bearer is rejected on this version — hard-won fact) |
| **Nginx Proxy Manager 2.15.1** | Per-tenant proxy host (`<username>.steprotech.com` → env-ip:port) + Let's Encrypt certs | REST API `:81` (login `POST /api/tokens` → JWT, Bearer auth is correct here) |
| **n8n instance** (per tenant) | The actual product each customer uses | Direct HTTP to the container origin (`http://<env-ip>:<port>`) for owner setup + login verification; public origin for password change |

Backend code lives in `backend/`; the canonical tenant compose template in
`templates/`; tests in `backend/tests/`.

---

## 3. Infrastructure map (as of 2026-09-01)

| Name | Host / IP | Role |
|---|---|---|
| Control host (Portainer env 3) | 137.131.55.194 | Portainer server + NPM + portal backend + many other services |
| Env 8 "n8n-cloud 2" | 129.146.2.18 | **Default landing environment** for new tenants (beta, gamma, delta live) |
| Env 4 "n8n-cloud" | 141.148.139.50 | Fallback landing environment |
| Env 9 "n8n Premium 1" | 132.226.124.178 | Fallback landing environment |
| Portal URL | https://portal.steprotech.com | Public UI (NPM proxy host, caching DISABLED — see §10) |
| Tenant domains | `<username>.steprotech.com` | Wildcard DNS → control host; NPM proxies to env host |

Admin-configurable landing environments: stored in the DB `settings`
table key `landing_environments` (comma-separated Portainer endpoint IDs tried in
order, e.g. `"8,4,9"`; default `"8"`). Default is seeded in `main.lifespan`.

Live tenants at handover: `beta` (port 32786), `gamma` (port 32787), `delta`
(port 32789) on env 8. Port range for tenants: **32768–60999**
(`PORT_RANGE_START`/`PORT_RANGE_END`).

---

## 4. Credentials & secrets — read this first

**Never commit, print, or paste real credentials.** The repo is private but will
be worked on by people who should not need production secrets.

| Secret | Where it lives (production) |
|---|---|
| `PORTAINER_TOKEN` | Backend stack env (Portainer UI → stack → edit env) AND control-host ops `.env` |
| `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` | Backend stack env + control-host ops `.env` |
| `ADMIN_PASSWORD_HASH`, `JWT_SECRET` | Backend stack env (PBKDF2 hash / JWT signing secret) |
| `SMTP_PASS` (purelymail `admin@steprotech.com`) | Backend stack env (also used as `N8N_SMTP_PASS` for tenants) |
| `PAYSTACK_SECRET_KEY` | Backend stack env (currently a placeholder — **mock gateway is live**) |
| Per-tenant `N8N_BASIC_AUTH_PASSWORD` + `N8N_ENCRYPTION_KEY` | Portainer stack env of each tenant stack (DB also stores current basic-auth pw + encryption key per instance row) |
| Account passwords | Only PBKDF2 hashes in the portal DB (salt derived from `JWT_SECRET`), never plaintext |

Operational scripts with hardcoded secrets live in `scripts/` on the control
host at `/home/ubuntu/projects/n8n-provisioning/scripts/` and are **gitignored** —
they are NOT part of the repo (see `.gitignore`).

**Control-host access** (if you are also the infra operator): SSH key
`/home/ubuntu/.ssh/hermes-server-access-20260812` → `ubuntu@137.131.55.194`
(control host), `ubuntu@129.146.2.18` (env 8). The Portainer/NPM API tokens
cross only SSH stdin, never argv or the public internet (see the skill
`portainer-stack-operations` for the exact transport pattern).

---

## 5. Business/product rules (Steward's decisions — do not regress)

1. **Pay before provision.** No instance is created until the annual
   subscription is `active` (set by the `charge.success` webhook). With live
   gateways, `POST /accounts/{id}/provision` returns 402 otherwise.
2. **Lock = STOP the container.** When an account is locked (admin action,
   cancellation, or expiry), the tenant's Portainer stack is STOPPED — not the
   n8n password rotated, not the door password rotated. Password rotation was
   tried and **failed as a lock** because n8n's forgot-password flow still
   worked (reset email via SMTP), letting a locked user back in. A stopped
   container is unreachable: no login, no API, no forgot-password. Unlock =
   START the stack; the owner password is untouched so the user logs in with
   their original credentials.
3. **Auto-expiry.** A background task (every 15 min) stops any instance whose
   `paid_until` has passed (status active/past_due/unpaid → locked). The annual
   period is prepaid — when it ends, access ends. Renewal (`charge.success`)
   starts it again automatically.
4. **Quota.** One instance per account by default (`accounts.quota = 1`). Only
   an admin can raise it (admin UI / `PUT /api/v1/admin/accounts/{id}/quota`,
   1–50). Multi-instance accounts get `username-2`, `username-3`, ... suffixes.
5. **Two plans, annual, GHS.** Plan A "n8n Workspace" = **GHS 300/year**
   (active — current canonical compose). Plan B "n8n Workspace Plus" =
   **GHS 500/year**, **inactive** (`PLAN_B_ACTIVE=false`) until its special
   compose is built; the difference between the plans is how the compose is
   provisioned. Currently both serve the same template.
6. **Username rule:** no dots in usernames/domains (`john-doe`, never
   `john.doe`). Derived from the email local part with dots/non-alnum → hyphens
   when the user doesn't choose one. Email addresses may contain dots — that's
   fine; the derived username must not.
7. **Admin-gated onboarding.** No open registration. First page asks for an
   email only; unknown emails create an access request that only the admin can
   approve with a token (see §8). Returning users always log in with
   email + password — never a token again.
8. **UI = CloudPanel style, no sidebar.** Steward's explicit design constraint.
   If you redesign the UI, study https://cloudpanel.steprotech.com (design
   tokens in §11).
9. **Chosen password is used everywhere.** The password the customer picks at
   signup is used for BOTH the basic-auth door (`N8N_BASIC_AUTH_PASSWORD`) and
   the n8n owner account, so one credential set works and n8n's own
   forgot-password works from day one. Policy: ≥8 chars, ≥1 uppercase,
   ≥1 lowercase, ≥1 digit (validated at signup and again before owner setup —
   n8n rejects passwords that don't meet its policy).
10. **No em dashes in user-facing text.** (SteProTECH copy rule.)

---

## 6. End-to-end flows

### 6.1 Onboarding (gate → register → pay → provision)

```
Visitor → https://portal.steprotech.com/
  │ POST /api/v1/auth/check {email}
  ├─ account exists?        → action=login        (password form)
  ├─ no account, no request → creates request     → action=requested ("we'll email you")
  └─ request exists:
       ├─ status requested  → action=waiting      (admin hasn't issued yet)
       └─ status token_sent → action=token        (enter the 8-char token)

Admin (logged into portal as admin) sees "Access requests" →
  POST /api/v1/admin/access-requests/{id}/token
    → generates XXXX-XXXX token (no 0/O/1/I), PBKDF2-hashes it in the DB,
       emails it to the visitor, and returns it once to the admin.

Visitor re-enters email → action=token → enters token →
  POST /api/v1/auth/verify-token {email, token} → action=verified
  → registration form (email locked) → POST /api/v1/accounts
      {email, username?, first_name, last_name, password, access_token}
    → re-verifies token server-side, validates policy, hashes password,
      creates account (quota=1), consumes the token, returns a portal session
      JWT (auto-login). 403 without a valid token.

Customer clicks Pay →
  POST /api/v1/accounts/{id}/checkout → Paystack hosted page (annual plan)
  → Paystack webhook charge.success →
      subscription_status=active, paid_until = now + 365d, unlock (no-op)
  → (UI then) POST /api/v1/accounts/{id}/provision
      → background task provision_account():
          pick env (landing_environments) → resolve forward IP
          → next free port (3-source merge, §9) → build per-account env
          → create Portainer stack from templates/n8n-stack-compose.yml
          → wait container running → NPM proxy host → LE cert → attach + ssl_forced
          → n8n owner auto-create + login verify → instance healthy
          → welcome email with URL + login + password
      → account status provisioned
```

### 6.2 Renewal

Paystack re-charges the saved card annually (native plan auto-renewal):

- `charge.success` → `paid_until` extended +365d, status active,
  `unlock_instance()` → stack started → user logs back in.
- `invoice.payment_failed` → status `past_due`, `paid_until` = now + grace
  (`LOCK_GRACE_DAYS`, default 7) → instance still running during grace.
- `subscription.disable` / `subscription.not_renew` → status canceled →
  `lock_instance()` immediately.
- If a past_due account is never renewed: `sweep_past_due()` (admin button)
  locks it once `paid_until` (the grace deadline) passes.
- If `paid_until` simply elapses while active: `sweep_expired()` (background,
  15 min) locks it. **No grace in this path** — prepaid year ended.

### 6.3 Expiry / lock / unlock (the absolute-lock engine)

- **Lock** = `PortainerClient.stop_stack(stack_id, env_id)` → all containers
  stopped → NPM serves 502 → nothing reachable.
- **Unlock** = `PortainerClient.start_stack(stack_id, env_id)` → containers
  start → n8n boots (~30–60s) → login works with the ORIGINAL password.
- Portainer returns 409 for stop/start on an already-stopped/running stack —
  the client treats 409 as success (idempotent).

### 6.4 Admin password reset (per instance)

`POST /api/v1/instances/{id}/reset-password` (admin JWT):

1. Read the current password from the Portainer stack env (source of truth).
2. Change the n8n owner password via `PATCH /rest/me/password` — this MUST go
   through the **public origin** (`https://<domain>`); calling the direct IP
   returns 401 because n8n enforces `N8N_HOST` host-header match.
3. Update the Portainer stack env (`N8N_BASIC_AUTH_PASSWORD`) + redeploy
   (brief 502/404 window while the container restarts).
4. Persist + email the new password.

---

## 7. Database (SQLite, volume `portal_data`, file `/data/portal.db`)

Tables (all DDL in `backend/app/db.py`):

- **accounts** — `id, email (unique), username (unique), display_name,
  first_name, last_name, password_hash (PBKDF2), status
  (pending|provisioned|failed|disabled), quota (default 1),
  stripe_customer_id, subscription_id, subscription_status
  (none|active|past_due|unpaid|canceled|locked), paid_until (unix ts),
  created_at, provisioned_at`.
- **instances** — `id, account_id, stack_name (unique), stack_id,
  environment_id, environment_name, port, domain, basic_auth_user,
  basic_auth_password (current door pw), n8n_encryption_key, npm_host_id,
  certificate_id, status (provisioning|healthy|failed|deleted),
  locked (0/1), lock_secret (vestigial — unused by lock v3), error,
  created_at`. `lock_secret` is a leftover from the superseded
  owner-password-rotation lock; it is unused and safe to ignore/remove later.
- **settings** — key/value (`landing_environments` = env fallback order).
- **access_requests** — `id, email (unique), status
  (requested|token_sent|registered|canceled), token_hash (PBKDF2),
  created_at, token_sent_at, token_expires_at, registered_at`.

Notes:
- Migrations are additive `ALTER TABLE ... ADD COLUMN` checks inside
  `init_db()` (runs at startup). `CREATE TABLE IF NOT EXISTS` does not add
  columns to existing tables — when you add a column, add an ALTER migration
  the same way or existing DBs break.
- WAL journal mode is enabled per connection.
- Stored passwords are PBKDF2-SHA256, 100k iterations, salt = first 16 hex of
  sha256(JWT_SECRET). There is intentionally no per-record random salt (v1
  simplicity) — do not weaken this further; upgrading to per-record salts
  would be a good future change.

---

## 8. API reference

Base: `https://portal.steprotech.com/api/v1` (or `http://127.0.0.1:8788` on the
control host). All JSON. Client session tokens: JWT `acc:<account_id>`.
Admin tokens: JWT `admin`, `Authorization: Bearer <token>`.

### Public / client

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /auth/check` | — | Email-only gate branch → `login|requested|waiting|token` |
| `POST /auth/login` | — | Returning user: email+password → `{token, account}` |
| `POST /auth/verify-token` | — | Visitor proves token → `verified` |
| `POST /accounts` | access token | Register (gated) → `{account, token}` |
| `GET /me` | client JWT | Account + instances for the dashboard |
| `POST /accounts/{id}/provision` | — | Start provisioning (background); 402 if unpaid live, 409 over quota |
| `GET /accounts/{id}` | — | Account + instances status |
| `GET /plans` | — | Both plans with `active` flags |
| `GET /plan` | — | Legacy single-plan shape |
| `POST /accounts/{id}/checkout` | — | Create hosted checkout → `{gateway, url, reference}` |
| `POST /webhook/paystack` | HMAC-SHA512 | Paystack events (charge.success / invoice.payment_failed / subscription.*) |
| `POST /webhook/stripe` | Stripe sig | Stripe events (dormant) |
| `POST /webhook/mock` | — | Mock E2E events; 400 unless `PAYMENT_GATEWAY=mock` |
| `GET /environments` | — | Portainer endpoints list (for UI) |
| `GET /health` | — | `{"ok": true}` |

### Admin (all require `Authorization: Bearer <admin JWT>`)

| Method & path | Purpose |
|---|---|
| `POST /admin/login` | password → admin JWT (PBKDF2 vs `ADMIN_PASSWORD_HASH`) |
| `GET/PUT /admin/settings` | read/set `landing_environments` |
| `GET /admin/accounts` | all accounts |
| `GET /admin/access-requests` | pending onboarding requests |
| `POST /admin/access-requests/{id}/token` | issue token (emails it, returns once) |
| `POST /admin/accounts/{id}/lock` | STOP instance stack + status locked |
| `POST /admin/accounts/{id}/unlock` | START instance stack + status active |
| `PUT /admin/accounts/{id}/quota` | `{quota: 1–50}` |
| `POST /admin/billing/sweep` | lock past-due past grace |
| `POST /admin/billing/sweep-expired` | manual auto-expiry sweep (UI button) |
| `POST /instances/{id}/reset-password` | full real owner+door password reset |

Auth modules: `backend/app/security.py` — `create_client_token`,
`verify_client`, `create_access_token`, `verify_admin`, PBKDF2 helpers.

---

## 9. Key implementation details (the hard-won facts)

These are runtime behaviors verified against the live systems — code is already
written around them; knowing them prevents "fixing" something that isn't broken
or reintroducing a bug.

### Portainer (CE 2.39.6)
- **Auth:** access token ONLY via `X-API-Key: <token>`. `Authorization: Bearer`
  returns 401 "Invalid JWT token" on this version. (NPM is the opposite —
  Bearer is correct there.)
- Creating a string stack: `POST /stacks/create/standalone/string?endpointId=N`
  with `{name, stackFileContent, env}`.
- `GET /stacks/{id}` does NOT include the compose content; use
  `GET /stacks/{id}/file` (returns `StackFileContent`). Needed before any
  env update via `PUT /stacks/{id}?endpointId=N` with `{env, prune,
  stackFileContent}`.
- Stop/start: `POST /stacks/{id}/stop?endpointId=N` / `.../start...`. 409 on
  wrong state is the guard — treat as success.
- **Stack delete keeps named volumes.** After deleting a failed stack, also
  delete `<stack>_n8n_data` (`DELETE /endpoints/{id}/docker/volumes/{name}`)
  or a re-provision mounts the stale n8n DB (owner already set) and fails
  owner setup.
- Docker published-port data reports `ports=[]` for EXITED containers — never
  trust Docker alone for "is this port free".

### Port allocation (3-source merge)
`provisioner.used_ports_all_sources()` = NPM proxy hosts whose forward_host is
this env's IP **∪** Portainer stack env vars `N8N_PORT` on this endpoint **∪**
Docker published ports. Next free port = highest used in range + 1
(`next_free_port`). "The fact that the container is off doesn't mean the port
is free" — stopped tenants keep their port because NPM proxy hosts and stack
envs still reference it.

### NPM (2.15.1)
- Login `POST /api/tokens {identity, secret}` → JWT; Bearer auth.
- Cert request: `POST /nginx/certificates {domain_names, meta: {}, provider:
  "letsencrypt"}`. Do NOT send letsencrypt_email/agree/dns_challenge — NPM
  fills them from its global settings; extra properties → 400.
- Proxy host PUT must be a CLEAN payload (whitelisted fields only —
  see `NPMClient.update_proxy_host`); copying fields back from GET → 400.
- `ssl_forced: true` is ignored at create time when no cert is attached; set
  it explicitly on the update that attaches the certificate.
- **NPM caching gotcha:** proxy hosts are created with
  `caching_enabled=True`; after static redeploys NPM serves OLD css/js until
  caching is disabled on the host or purged. The portal host has caching
  DISABLED — keep it that way (dynamic app with auth).

### n8n instance API
- Owner setup: `POST /rest/owner/setup {email, firstName, lastName, password}`
  via the container origin (`http://<env-ip>:<port>`). Requirements:
  non-empty lastName (else 400 "Last name is required"), password meeting
  n8n policy (≥8, upper, lower, digit).
- **Boot-time SPA shell answers 200 to everything** — wait for `GET /healthz`
  → `{"status":"ok"}` and only accept setup/login responses whose body is real
  JSON (contains `"id"`/`"data"`).
- Login: `POST /rest/login` with `{emailOrLdapLoginId, password}` (note the
  field name; some n8n versions want `email`, this build wants
  `emailOrLdapLoginId`). Basic auth header is optional for the API.
- Password change: `PATCH /rest/me/password` requires the N8N_HOST host-header
  match — must go through the PUBLIC origin (`https://<domain>`), not the raw
  IP. Login tolerates the raw IP; password change does not.
- The basic-auth door (`N8N_BASIC_AUTH_ACTIVE=true`) does NOT gate
  `/rest/login` (both old and new door passwords authenticate) — this is why
  door rotation can't lock anyone. Container stop is the only real lock.

### Concurrency / retry safety
- Failed provisions delete their instance rows before retry (else
  `UNIQUE constraint failed: instances.stack_name`).
- Quota is checked both in the API (fast 409) and inside the provisioner
  (authoritative, multi-instance suffixing).

---

## 10. Configuration reference (env vars)

Backend compose: `backend/docker-compose.yml`; defaults in
`backend/app/config.py`. Secrets come from the Portainer stack env
(`stack.env`), never committed. Tenant template env: `build_stack_env()` in
`provisioner.py` (per-account) against `templates/n8n-stack-compose.yml`.

| Var | Meaning | Live value (prod intent) |
|---|---|---|
| `PORTAINER_URL` / `PORTAINER_TOKEN` | Portainer API | `http://host.docker.internal:9000` / secret |
| `NPM_URL` / `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` | NPM API | `http://host.docker.internal:81` / secret |
| `BASE_DOMAIN` | tenant suffix | `steprotech.com` |
| `PORT_RANGE_START/END` | tenant port range | `32768` / `60999` |
| `ADMIN_PASSWORD_HASH` | admin PBKDF2 | set in stack env |
| `JWT_SECRET` | JWT + PBKDF2 salt | set in stack env |
| `PAYMENT_GATEWAY` | `paystack` \| `stripe` \| `mock` | **`mock` live now**; flip to `paystack` when real keys are set |
| `PLAN_NAME` / `PLAN_CURRENCY` / `PLAN_AMOUNT_MINOR` | Plan A | `n8n Workspace` / `GHS` / `30000` (GHS 300/yr) |
| `PLAN_B_NAME` / `PLAN_B_AMOUNT_MINOR` / `PLAN_B_ACTIVE` | Plan B | `n8n Workspace Plus` / `50000` / `false` |
| `PAYSTACK_SECRET_KEY` / `PAYSTACK_PLAN_CODE` / `PAYSTACK_CALLBACK_URL` | Paystack | secret / auto-created if empty / `https://portal.steprotech.com/?status=success` |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_ID` | Stripe (dormant) | unset |
| `SMTP_HOST/PORT/USER/PASS/SENDER/SSL/STARTTLS` | portal emails | purelymail, `admin@steprotech.com` |
| `N8N_SMTP_PASS` | tenant n8n SMTP (same purelymail account) | same secret as `SMTP_PASS` |
| `DB_PATH` | sqlite file | `/data/portal.db` |
| `COMPOSE_TEMPLATE_PATH` | canonical template | `/app/templates/n8n-stack-compose.yml` |
| `LOCK_GRACE_DAYS` | renewal grace | `7` |
| `DEFAULT_QUOTA` | new-account instance quota | `1` |
| `STATIC_DIR` | UI dir override | default from Dockerfile layout |

**Paystack plan auto-creation:** if `PAYSTACK_PLAN_CODE` is empty,
`ensure_plan()` looks up an existing annual plan matching name/interval/currency
or creates one — no manual dashboard setup needed. Webhooks must point to
`https://portal.steprotech.com/api/v1/webhook/paystack`; signature header
`x-paystack-signature` = HMAC-SHA512 hex of the raw body keyed by the secret.

---

## 11. UI (static SPA served by the backend at `/`)

Files: `backend/static/{index.html, app.js, styles.css, logo.svg, favicon.svg}`.
No framework, no build step — plain HTML/CSS/JS. Views: gate (email-only),
login, register (after token verify), dashboard, instances, plans & billing,
admin (env settings, access requests, accounts table with lock/unlock/quota,
renewal sweep). Auth tokens stored client-side; `app.js` handles the API calls.

**CloudPanel design tokens (verified against live cloudpanel.steprotech.com):**

| Token | Value |
|---|---|
| App background | `#f9fafb` |
| Login/gate background | `#f3f3f3` |
| Header | 75px white, bottom border `#dfdfdf`, logo left with border-right |
| Accent (primary buttons `.btn-blue`) | `#267ddd`, hover `#2e87eb` |
| Danger `.btn-red` | `#e15650` |
| Card | white, radius 4, `box-shadow 0 2px 4px rgb(157 161 164 / 19%)` |
| Borders | `#dfdfdf` |
| Status badges | `.label-outline-*` — transparent bg, 1px border + colored text (`#73bf4c` green / `#ff7200` orange / `#d84531` red) |
| Nav | horizontal top-nav inside the header, gray `#aaaaaa`, active/hover `#0078d4` |
| Tables | thead `#fbfcfc`, uppercase 14px/700 `#9bacb6`, row borders `#eaeaea` |
| Dark theme | `html.dark` class |

**No sidebar. Ever.** Steward rejected a sidebar explicitly ("CloudPanel
doesn't have one"). If you touch layout, keep: 75px header + horizontal nav +
full-width content; gate/login pages have NO header and the `#f3f3f3`
background.

Known CSS traps (already solved in the code — don't reintroduce):
- Margin collapse pushes the gate down if a wrapper lacks `overflow:hidden`.
- After ANY static change, verify the SERVED file via curl (grep a new marker)
  — NPM proxy cache can serve stale CSS while the container holds the new file.
- Boot-time `$("...")` calls in app.js on removed DOM ids kill ALL rendering —
  when editing HTML ids, check app.js references.

---

## 12. Tests & verification

- **27 pytest tests** in `backend/tests/test_provisioner.py` covering: username
  derivation/validation, port allocation, the full provision flow (mocked
  Portainer/NPM), owner auto-creation, lock/unlock = stop/start, payment
  lifecycle (mock gateway: charge.success → active+unlock, payment_failed →
  past_due, disable → canceled+lock), auto-expiry sweep + renewal restart,
  quota gate, two-plan listing, access-gate full flow (requested → waiting →
  token → verify → register → auto-login), registration-without-token 403.
- Run: `cd backend && DB_PATH=/tmp/portal-test.db python -m pytest tests/ -q`
  (venv: `backend/.venv`, deps in `backend/requirements.txt`).
- `verify.yaml` at repo root is a Hermes verify recipe
  (compile → pytest → boot uvicorn on :8877 → poll /api/v1/health) — useful as
  a CI-equivalent if you adopt it.

---

## 13. Deployment (backend changes)

Production backend: Docker stack `n8n-portal-backend` (id 72) on the control
host env 3, image tag in `backend/docker-compose.yml` (`n8n-portal-backend:
0.1.24` at handover).

Standard deploy from the control host (ops scripts in `scripts/`, gitignored):

```bash
cd /home/ubuntu/projects/n8n-provisioning
# 1. bump the image tag in backend/docker-compose.yml
# 2. tar the changed tree, scp to control host, docker build there:
tar czf /tmp/portal.tgz backend/app backend/static backend/templates
scp -i ~/.ssh/hermes-server-access-20260812 /tmp/portal.tgz ubuntu@137.131.55.194:/tmp/
ssh -i ~/.ssh/hermes-server-access-20260812 ubuntu@137.131.55.194 '
  cd /tmp && rm -rf portal-src && mkdir portal-src && tar xzf portal.tgz -C portal-src
  cd portal-src/backend && docker build -t n8n-portal-backend:0.1.25 . '
# 3. update the stack image tag in the Portainer UI (or via update-backend-stack.sh),
#    redeploy, then verify: docker ps / health endpoint / public portal
```

After deploying, verify: container healthy (`docker ps`),
`curl http://127.0.0.1:8788/api/v1/health`, public
`https://portal.steprotech.com/` serves, and the SERVED css/js contain the
latest marker (NPM cache gotcha).

**Never** let a static redeploy go out without confirming the served asset
changed; and keep NPM caching OFF on the portal proxy host.

---

## 14. Operations runbook

| Task | How |
|---|---|
| See all accounts | Admin UI → Accounts (or `GET /admin/accounts`) |
| Approve a new onboarding request | Admin UI → Access requests → Issue token (emails visitor + shows token once) |
| Lock / unlock an account | Admin UI → Accounts → Lock/Unlock button (stops/starts the tenant stack) |
| Raise an account's quota | Admin UI → Quota → Edit (1–50) |
| Force an expiry sweep now | Admin UI → "Stop expired instances" (`POST /admin/billing/sweep-expired`) |
| Reset a tenant's n8n password | `POST /instances/{id}/reset-password` (admin JWT) — changes owner + door, emails it |
| Diagnose "wrong username or password" | 1) read the REAL current password from the Portainer stack env (that's what the welcome email carried); 2) test `POST /rest/login` against the public URL without basic auth; 3) read the error text — 401 "caps lock" = wrong password, 400 "Invalid email address" = username typed in the email field; 4) stale browser basic-auth cache is the classic client-side culprit (double gate after form login) — retry in incognito |
| Manual port inventory | NPM proxy hosts + Portainer stack envs + Docker ports for the env (§9) |
| Read the portal DB | `docker exec` a sqlite read inside the container, or copy `/data/portal.db` from the `portal_data` volume |

---

## 15. Known limitations & next steps (open items)

1. **Paystack is not live yet.** `PAYMENT_GATEWAY=mock`; `PAYSTACK_SECRET_KEY`
   is a placeholder. To go live: set the real Paystack secret key in the
   backend stack env, point Paystack webhooks at
   `https://portal.steprotech.com/api/v1/webhook/paystack` (HMAC-SHA512,
   events: charge.success, invoice.payment_failed, subscription.disable,
   subscription.not_renew, subscription.create), flip `PAYMENT_GATEWAY=paystack`
   and redeploy. Mock mode accepts `{"mock": true, ...}` events at
   `/api/v1/webhook/mock` for E2E — the payload shapes are documented in
   `billing.mock_handle_event()`.
2. **Stripe is dormant.** Implementation exists (`stripe_billing.py` +
   `requirements.txt` has `stripe`), needs keys + price id + webhook secret +
   webhook URL config to activate. Steward said Stripe is secondary/for later.
3. **Plan B (GHS 500) is inactive** until its special compose variant is built.
   `PLAN_B_ACTIVE=false`. When enabled, decide how plan choice flows into
   provisioning (today: one template serves both; `GET /api/v1/plans` returns
   both with `active` flags, UI shows plan B as "Coming soon").
4. **Single admin account** with a PBKDF2 hash from env; no admin
   multi-user/RBAC yet.
5. **`lock_secret` column** on instances is vestigial (unused by the
   stop/start lock). Safe to drop with a migration, or leave.
6. **Password hashing salt** derives from `JWT_SECRET` (no per-record salt) —
   fine for now, worth upgrading later.
7. **UI language/currency** is hardcoded to GHS per Steward's instruction
   ("for now let's keep it in Ghana cedis"); revisit quoting in dollars later
   if he asks.
8. **Owner dashboard for tenants** doesn't exist yet — customers interact with
   their n8n directly; the portal is for account/billing/admin.
9. Optional future: automated backups of the portal DB (`portal_data` volume)
   and of tenant n8n DBs.
10. `templates/n8n-stack.env.example` documents the tenant env shape; the real
    per-account values are produced by `build_stack_env()` — keep the two in
    sync when you change either.

---

## 16. Repository layout

```
backend/
  Dockerfile                 # python:3.12-slim + uvicorn
  docker-compose.yml         # prod backend stack (env + volume + healthcheck)
  requirements.txt           # fastapi/uvicorn/httpx/pydantic/PyJWT/stripe...
  app/
    main.py                  # FastAPI app: all routes + lifespan expiry loop
    config.py                # Settings dataclass (env-driven)
    db.py                    # SQLite schema, migrations, all data helpers
    security.py              # PBKDF2 + JWT (admin & client)
    services/
      provisioner.py         # THE provisioning pipeline (+ rollback)
      billing.py             # gateway facade + lock/unlock/sweep engine
      paystack.py            # Paystack client (primary)
      stripe_billing.py      # Stripe client (dormant)
      portainer_client.py    # Portainer REST client
      npm_client.py          # NPM REST client
      emailer.py             # SMTP (welcome/reset/token)
      access_gate.py         # onboarding token gate
  static/                    # SPA UI (index.html/app.js/styles.css/logo/favicon)
  tests/test_provisioner.py  # 27 tests
  scripts/                   # (local ops only — gitignored; dryrun_provision.py,
                             #  live_port_check.py ARE tracked dev utilities)
templates/
  n8n-stack-compose.yml      # canonical tenant compose (the "300" template)
  n8n-stack.env.example      # tenant env documentation
verify.yaml                  # hermetic verify recipe (compile+test+boot+health)
README.md                    # quick start (this doc supersedes it for depth)
.gitignore                   # excludes scripts/, .env*, *.pem, *.key, *.db*, .hermes/
```

---

## 17. Getting started (for the new developer)

```bash
git clone git@github.com:stewardobeng/n8n-portal.git
cd n8n-portal
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
# run the suite with a scratch DB:
DB_PATH=/tmp/portal-test.db python -m pytest tests/ -q          # expect 27 passed
# boot the API locally (no real infra needed for the suite):
DB_PATH=/tmp/portal-dev.db ADMIN_PASSWORD_HASH=x JWT_SECRET=dev \
  uvicorn app.main:app --reload --port 8788
# open http://127.0.0.1:8788/ — the gate page renders; PAYMENT_GATEWAY default
# 'paystack' has no keys, so for local payment E2E set PAYMENT_GATEWAY=mock.
```

Local runs hit no real Portainer/NPM unless you export the tokens (see §4).
The test suite mocks both clients, so CI/dev is safe by default.

---

## 18. Contact / support within SteProTECH

Product owner: **Steward Obeng** (decisions on pricing, lock semantics, UI
style, quotas). Infra access & production operations are handled by the
SteProTECH in-house operator (Inhim). Ask before:
- changing the lock mechanism (stop/start is a product decision),
- enabling Plan B or Stripe,
- touching live tenant instances,
- pushing to GitHub `main` with anything other than reviewed code.

*End of handover document.*
