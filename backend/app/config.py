# n8n Self-Service Provisioning — Backend (FastAPI)
# Config loads from environment (stack.env when running in Portainer).

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # --- Portainer API ---
    portainer_url: str = os.getenv("PORTAINER_URL", "http://host.docker.internal:9000")
    portainer_token: str = os.getenv("PORTAINER_TOKEN", "")

    # --- NPM API ---
    npm_url: str = os.getenv("NPM_URL", "http://host.docker.internal:81")
    npm_email: str = os.getenv("NPM_ADMIN_EMAIL", "")
    npm_password: str = os.getenv("NPM_ADMIN_PASSWORD", "")

    # --- Domain / naming ---
    base_domain: str = os.getenv("BASE_DOMAIN", "steprotech.com")
    port_range_start: int = int(os.getenv("PORT_RANGE_START", "32768"))
    port_range_end: int = int(os.getenv("PORT_RANGE_END", "60999"))
    # WebAuthn relying-party id for passkeys (2026-09-03). Must be a registrable
    # domain (or subdomain) the portal is really served on, minus scheme.
    rp_id: str = os.getenv("RP_ID", "portal.steprotech.com")

    # --- Admin portal auth ---
    admin_password_hash: str = os.getenv("ADMIN_PASSWORD_HASH", "")  # bcrypt hash
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_expiry_hours: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    # Admin impersonation (login as customer) session lifetime in minutes.
    impersonation_ttl_minutes: int = int(os.getenv("IMPERSONATION_TTL_MINUTES", "60"))

    # --- SMTP (welcome / reset emails) ---
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.purelymail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "admin@steprotech.com")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    # Sender may arrive as 'Name <addr>' or '"Name <addr>"' (stray wrapping
    # quotes from compose plain scalars). Normalize so the From header is always
    # a valid RFC 5322 address (Gmail rejects quoted-only display names — 550
    # 5.7.1 "missing a valid address in From", hit live 2026-09-02).
    smtp_sender: str = os.getenv("SMTP_SENDER", "n8n Portal <no-reply@steprotech.com>").strip('"').strip()
    smtp_ssl: bool = os.getenv("SMTP_SSL", "false").lower() == "true"
    smtp_starttls: bool = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

    # --- Stack defaults (static values shared by all tenants) ---
    default_timezone: str = "Africa/Accra"
    n8n_email_mode: str = "smtp"
    n8n_smtp_host: str = "smtp.purelymail.com"
    n8n_smtp_port: int = 587
    n8n_smtp_user: str = "admin@steprotech.com"
    n8n_smtp_pass: str = os.getenv("N8N_SMTP_PASS", "")
    n8n_smtp_sender: str = '"n8n <no-reply@steprotech.com>"'
    n8n_smtp_ssl: str = "false"
    n8n_smtp_starttls: str = "true"

    # --- Files ---
    db_path: str = os.getenv("DB_PATH", "/data/portal.db")
    compose_template_path: str = os.getenv(
        "COMPOSE_TEMPLATE_PATH", "/app/templates/n8n-stack-compose.yml"
    )
    # --- Which n8n version NEW workspaces get (0.1.81) ---
    # A new workspace is pinned to a real release tag, never the floating
    # `latest` tag: `latest` is resolved per host at every container recreate,
    # so attaching a Python runner or any Portainer redeploy could silently
    # move a customer onto a newer n8n and run its one-way DB migrations with
    # nobody deciding it (Steward, 2026-10-06).
    # Default: pin the newest release published on Docker Hub at provision
    # time, so a new customer still starts current.
    # N8N_PROVISION_TAG (optional) holds new workspaces on a chosen version and
    # wins over the lookup; N8N_FALLBACK_TAG is used when the lookup cannot be
    # reached, so provisioning never falls back to a floating tag.
    n8n_provision_tag: str = os.getenv("N8N_PROVISION_TAG", "").strip()
    n8n_fallback_tag: str = os.getenv("N8N_FALLBACK_TAG", "2.43.1").strip()

    data_dir: str = "/data"
    backup_dir: str = os.getenv("BACKUP_DIR", "/data/backups")

    # --- Payments (facade: paystack | stripe | mock) ---
    # paystack is PRIMARY (GHS, native annual plans with auto-renewal). stripe is
    # available for later (USD). mock = no external service; simulates checkout +
    # webhooks so the full pay -> provision -> renew -> lock flow is E2E-testable.
    payment_gateway: str = os.getenv("PAYMENT_GATEWAY", "paystack").lower()
    plan_name: str = os.getenv("PLAN_NAME", "n8n Workspace")
    plan_currency: str = os.getenv("PLAN_CURRENCY", "GHS")
    plan_amount_minor: int = int(os.getenv("PLAN_AMOUNT_MINOR", "30000"))  # GHS 300 / year (minor units)
    # Plan B (GHS 500 / year) — Steward 2026-09-01: two prices, both annual; the
    # difference is how the compose is provisioned (300 = current template, 500 =
    # special compose later). PLAN_B_ACTIVE=false keeps it hidden/inactive for now.
    plan_b_name: str = os.getenv("PLAN_B_NAME", "n8n Workspace Plus")
    plan_b_amount_minor: int = int(os.getenv("PLAN_B_AMOUNT_MINOR", "50000"))  # GHS 500 / year
    plan_b_active: bool = os.getenv("PLAN_B_ACTIVE", "false").lower() == "true"
    # Renewal reminders (0.1.76): with the Paystack plan removed there is no
    # automatic renewal, so the portal emails the customer before and after the
    # term ends, each mail carrying a login-free renewal link. Sending stays OFF
    # until Steward approves the wording, so the copy can be previewed on live
    # data without a single customer being emailed.
    renewal_reminders_enabled: bool = \
        os.getenv("RENEWAL_REMINDERS_ENABLED", "false").lower() == "true"
    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "https://portal.steprotech.com")
    # Plan B stack = chibz/bigcroc owner tier (0.50 CPU / 1 GiB / heap 768)
    plan_b_compose_template_path: str = os.getenv(
        "PLAN_B_COMPOSE_TEMPLATE_PATH", "/app/templates/n8n-stack-compose-plus.yml"
    )

    # One account per user by default; admin can raise the instance quota
    default_quota: int = int(os.getenv("DEFAULT_QUOTA", "1"))

    # Paystack
    paystack_secret_key: str = os.getenv("PAYSTACK_SECRET_KEY", "")
    paystack_plan_code: str = os.getenv("PAYSTACK_PLAN_CODE", "")  # auto-created if empty
    # Webhook router shared secret (0.1.67): when set, /webhook/paystack ALSO
    # requires x-router-secret to match, so only OUR router can deliver events
    # (a stolen Paystack signature alone is not enough). Empty = feature off.
    paystack_router_secret: str = os.getenv("PAYSTACK_ROUTER_SECRET", "")
    # Amount enforcement (minor units): charge.success must match the plan
    # price. Empty amount = check disabled (not recommended).
    paystack_expected_amount_a: str = os.getenv("PAYSTACK_EXPECTED_AMOUNT_A", "")
    paystack_expected_amount_b: str = os.getenv("PAYSTACK_EXPECTED_AMOUNT_B", "")
    paystack_plan_code_b: str = os.getenv("PAYSTACK_PLAN_CODE_B", "")  # Plan B (500), auto-created if empty
    paystack_callback_url: str = os.getenv(
        "PAYSTACK_CALLBACK_URL", "https://portal.steprotech.com/?status=success"
    )

    # Stripe (secondary, for later)
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_price_id: str = os.getenv("STRIPE_PRICE_ID", "")
    stripe_success_url: str = os.getenv(
        "STRIPE_SUCCESS_URL", "https://portal.steprotech.com/?status=success"
    )
    stripe_cancel_url: str = os.getenv(
        "STRIPE_CANCEL_URL", "https://portal.steprotech.com/?status=cancelled"
    )

    # Grace period (days) after a failed renewal before the instance is locked
    lock_grace_days: int = int(os.getenv("LOCK_GRACE_DAYS", "7"))


settings = Settings()
