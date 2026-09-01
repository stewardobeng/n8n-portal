# SteProTECH n8n Self-Service Portal

Self-service n8n provisioning platform: clients sign up, subscribe (Paystack,
GHS), and receive a fully provisioned, dedicated n8n workspace on their own
subdomain — with owner auto-creation, per-tenant SSL, and automatic expiry
shutdown.

## Architecture

- **FastAPI backend** (`backend/app`) — provisioning, billing facade, admin
  portal, static UI.
- **Portainer API** — creates per-tenant compose stacks on the chosen
  environment (admin-configurable landing environments).
- **Nginx Proxy Manager API** — per-tenant proxy host + Let's Encrypt cert.
- **Paystack** (primary, GHS annual plans, native auto-renewal), Stripe
  (secondary), mock gateway for E2E.
- **SQLite** (`portal_data` volume) — accounts, instances, access requests,
  settings.

## Flow

1. **Gate**: the portal's first page asks only for an email. Unknown emails
   create an access request; the admin issues a one-time token (72h) that is
   emailed to the person. Returning users log in with email + password.
2. **Pay**: signup → checkout (GHS 300/year active plan; GHS 500/year plan
   inactive until its special compose is built) → `charge.success` webhook
   activates the subscription.
3. **Provision**: a background task creates the Portainer stack from the
   canonical compose template with per-account env vars (unique port, domain,
   encryption key, basic-auth = email + chosen password), then the NPM proxy
   host and a fresh Let's Encrypt cert, then auto-creates the n8n owner
   account with the user's chosen password.
4. **Lifecycle**: one instance per account by default (admin can raise the
   quota). When the subscription expires, a background sweep STOPS the
   instance container; a successful renewal starts it again.

## Configuration (env / stack.env)

| Var | Purpose | Default |
|-----|---------|---------|
| `PORTAINER_URL` / `PORTAINER_TOKEN` | Portainer API (token as `X-API-Key`) | http://host.docker.internal:9000 |
| `NPM_URL` / `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` | NPM API (JWT bearer) | http://host.docker.internal:81 |
| `BASE_DOMAIN` | tenant subdomain suffix | steprotech.com |
| `ADMIN_PASSWORD_HASH` / `JWT_SECRET` | admin portal auth (PBKDF2) | |
| `PAYMENT_GATEWAY` | paystack \| stripe \| mock | paystack |
| `PLAN_NAME` / `PLAN_CURRENCY` / `PLAN_AMOUNT_MINOR` | active plan (GHS 300) | n8n Workspace / GHS / 30000 |
| `PLAN_B_NAME` / `PLAN_B_AMOUNT_MINOR` / `PLAN_B_ACTIVE` | second plan (GHS 500, inactive) | n8n Workspace Plus / 50000 / false |
| `PAYSTACK_SECRET_KEY` / `PAYSTACK_PUBLIC_KEY` | Paystack keys | |
| `SMTP_*` | welcome / reset / access-token email | purelymail |
| `DEFAULT_QUOTA` | instances per account | 1 |

## Tests

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
DB_PATH=/tmp/portal-test.db python -m pytest tests/ -q
```

## Deploy

Build the image, tag, and update the Portainer stack (image tag + env list in
`backend/docker-compose.yml`). The UI is served from the backend static mount;
`portal.steprotech.com` fronts it through NPM with a Let's Encrypt cert and
caching disabled.
