# SQLite persistence for the provisioning portal.
# Tables:
#   accounts   — client accounts (email, username, status)
#   instances  — one row per provisioned n8n instance (stack name, env, port, domain)
#   settings   — key/value admin settings (e.g. landing_environment)
#   auth_tokens — issued portal JWTs (optional; keep simple)

import os
import sqlite3
import time
import uuid

from .config import settings
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    password_hash TEXT DEFAULT '',   -- PBKDF2 hash (never plaintext)
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | provisioned | failed | disabled
    quota INTEGER NOT NULL DEFAULT 1,         -- max n8n instances for this account (admin-raisable)
    -- Stripe billing
    stripe_customer_id TEXT DEFAULT '',
    subscription_id TEXT DEFAULT '',
    subscription_status TEXT DEFAULT 'none', -- none | active | past_due | unpaid | canceled | locked
    paid_until INTEGER,
    paid_from INTEGER,               -- subscription start (admin mark-paid backdating)
    account_state TEXT NOT NULL DEFAULT 'active', -- active | suspended | archived (admin lifecycle)
    totp_secret TEXT DEFAULT '',     -- authenticator-app secret (2FA)
    totp_enabled INTEGER DEFAULT 0,  -- authenticator 2FA active
    email_2fa INTEGER DEFAULT 0,     -- email one-time-code 2FA active
    created_at INTEGER NOT NULL,
    provisioned_at INTEGER
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL,        -- PBKDF2 hash of the reset token (never plaintext)
    used INTEGER NOT NULL DEFAULT 0, -- single-use
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS email_otps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL UNIQUE,
    otp_hash TEXT NOT NULL,          -- PBKDF2 hash of the 6-digit code
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    stack_name TEXT NOT NULL UNIQUE,
    stack_id INTEGER,
    environment_id INTEGER NOT NULL,
    environment_name TEXT,
    port INTEGER NOT NULL,
    domain TEXT NOT NULL,
    basic_auth_user TEXT,
    basic_auth_password TEXT DEFAULT '',
    n8n_encryption_key TEXT,
    npm_host_id INTEGER,
    certificate_id INTEGER,
    status TEXT NOT NULL DEFAULT 'provisioning', -- provisioning | healthy | failed | deleted
    locked INTEGER NOT NULL DEFAULT 0,          -- 1 = access locked (unpaid)
    lock_secret TEXT DEFAULT '',                -- random owner pw while locked (for unlock)
    container_id TEXT DEFAULT '',               -- fallback for admin-attached stacks w/o stack record
    managed INTEGER NOT NULL DEFAULT 1,         -- 1 = portal-provisioned; 0 = admin-attached pre-existing
    error TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'requested', -- requested | token_sent | registered | canceled
    token_hash TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    token_sent_at INTEGER,
    token_expires_at INTEGER,
    registered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_instances_account ON instances(account_id);
CREATE INDEX IF NOT EXISTS idx_instances_env_port ON instances(environment_id, port);
CREATE INDEX IF NOT EXISTS idx_access_requests_email ON access_requests(email);
"""


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        # Migration: older DBs lack the account name/password columns
        cur = conn.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(accounts)")]
        for c in ("first_name", "last_name", "password_hash"):
            if c not in cols:
                cur.execute(f'ALTER TABLE accounts ADD COLUMN {c} TEXT DEFAULT ""')
        # Migration: instances gained basic_auth_password (reset needs the current value)
        icols = [r[1] for r in cur.execute("PRAGMA table_info(instances)")]
        if "basic_auth_password" not in icols:
            cur.execute('ALTER TABLE instances ADD COLUMN basic_auth_password TEXT DEFAULT ""')
        # Migration: instances gained locked flag (billing lock)
        if "locked" not in icols:
            cur.execute("ALTER TABLE instances ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        if "lock_secret" not in icols:
            cur.execute('ALTER TABLE instances ADD COLUMN lock_secret TEXT DEFAULT ""')
        # Migration: accounts gained Stripe billing columns
        acols = [r[1] for r in cur.execute("PRAGMA table_info(accounts)")]
        for c, ddl in (
            ("stripe_customer_id", 'TEXT DEFAULT ""'),
            ("subscription_id", 'TEXT DEFAULT ""'),
            ("subscription_status", 'TEXT DEFAULT "none"'),
            ("paid_until", "INTEGER"),
            ("quota", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if c not in acols:
                cur.execute(f"ALTER TABLE accounts ADD COLUMN {c} {ddl}")
        # Migration: instances gained container_id + managed flag (admin-attached
        # pre-existing stacks have no valid Portainer stack record / no managed
        # basic-auth; lock/unlock falls back to container-level stop/start).
        icols = [r[1] for r in cur.execute("PRAGMA table_info(instances)")]
        if "container_id" not in icols:
            cur.execute('ALTER TABLE instances ADD COLUMN container_id TEXT DEFAULT ""')
        if "managed" not in icols:
            cur.execute("ALTER TABLE instances ADD COLUMN managed INTEGER NOT NULL DEFAULT 1")
        # Migration: accounts record the paid-from (subscription start) date for
        # admin backdating (mark-paid with custom dates, 2026-09-02).
        acols2 = [r[1] for r in cur.execute("PRAGMA table_info(accounts)")]
        if "paid_from" not in acols2:
            cur.execute("ALTER TABLE accounts ADD COLUMN paid_from INTEGER")
        # Migration: account lifecycle state for admin suspend/archive (2026-09-02).
        # Separate from provisioning `status` (pending/provisioned/failed):
        #   active     -> normal operation
        #   suspended  -> admin hold; workspace stopped immediately, login blocked
        #   archived   -> soft-delete; hidden from the portal, workspace stays off
        acols3 = [r[1] for r in cur.execute("PRAGMA table_info(accounts)")]
        if "account_state" not in acols3:
            cur.execute('ALTER TABLE accounts ADD COLUMN account_state TEXT NOT NULL DEFAULT "active"')
        # Migration: instances record the running n8n image (update feature tracks
        # the current image tag so the admin can change it on one instance).
        ic2 = [r[1] for r in cur.execute("PRAGMA table_info(instances)")]
        if "image" not in ic2:
            cur.execute('ALTER TABLE instances ADD COLUMN image TEXT DEFAULT "n8nio/n8n:latest"')
        # Migration: 2FA columns (authenticator TOTP + email OTP) + reset/otp tables
        acols4 = [r[1] for r in cur.execute("PRAGMA table_info(accounts)")]
        for c, ddl in (
            ("totp_secret", 'TEXT DEFAULT ""'),
            ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("email_2fa", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if c not in acols4:
                cur.execute(f"ALTER TABLE accounts ADD COLUMN {c} {ddl}")
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL UNIQUE,
            otp_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            instance_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'full',   -- full | workflows | credentials
            filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'creating', -- creating | ready | failed
            error TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            UNIQUE(account_id, instance_id, kind, created_at)
        );
        CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            event TEXT NOT NULL,          -- login_fail | login_ok | register | mfa_fail | reset | ban_env ...
            account_id INTEGER,
            detail TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ip_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL UNIQUE,
            reason TEXT DEFAULT '',
            expires_at INTEGER,            -- NULL = permanent
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS passkeys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,           -- 'account' | 'admin'
            account_id INTEGER,            -- NULL for admin
            credential_id TEXT NOT NULL UNIQUE,
            public_key BLOB NOT NULL,
            sign_count INTEGER DEFAULT 0,
            transports TEXT DEFAULT '',
            name TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS passkey_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            account_id INTEGER,
            challenge BLOB NOT NULL,
            kind TEXT DEFAULT 'register',  -- 'register' | 'login'
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        """)
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


def new_key(prefix: str = "", nbytes: int = 24) -> str:
    """Return a URL-safe random string (used for encryption keys / generated passwords)."""
    raw = uuid.uuid4().hex + uuid.uuid4().hex
    return f"{prefix}{raw[: nbytes * 2]}"  # hex → nbytes bytes worth of entropy


def new_password(length: int = 16) -> str:
    """Policy-compliant generated password: n8n requires >=1 uppercase, >=1 lowercase,
    >=1 digit (verified live 2026-09-01: pure-hex password was rejected with
    'Password must contain at least 1 uppercase letter'). Guarantee each class."""
    import random
    import string as _string

    lower = _string.ascii_lowercase
    upper = _string.ascii_uppercase
    digits = _string.digits
    # guarantee one of each, fill the rest, shuffle
    rest = [random.SystemRandom().choice(lower + upper + digits) for _ in range(length - 3)]
    pool = [random.SystemRandom().choice(upper),
            random.SystemRandom().choice(lower),
            random.SystemRandom().choice(digits)] + rest
    random.SystemRandom().shuffle(pool)
    return "".join(pool)


# ---- accounts ----

def create_account(email: str, username: str, display_name: str = "",
                   first_name: str = "", last_name: str = "",
                   password_hash: str = "") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (email, username, display_name, first_name, last_name, "
            "password_hash, status, quota, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (email.lower(), username, display_name, first_name, last_name,
             password_hash, settings.default_quota, now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_account_by_email(email: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email.lower(),)
        ).fetchone()
    finally:
        conn.close()


def get_account_by_username(username: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()


def get_account(account_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    finally:
        conn.close()


def get_account_by_subscription(subscription_id: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM accounts WHERE subscription_id = ?", (subscription_id,)
        ).fetchone()
    finally:
        conn.close()


def list_accounts() -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM accounts ORDER BY id DESC").fetchall()
    finally:
        conn.close()


def set_account_status(account_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET status = ?, provisioned_at = COALESCE(provisioned_at, ?) "
            "WHERE id = ?",
            (status, now() if status == "provisioned" else None, account_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_account_quota(account_id: int, quota: int) -> None:
    """Admin raises/lowers the max-instance quota for an account."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET quota = ? WHERE id = ?",
            (max(1, quota), account_id),
        )
        conn.commit()
    finally:
        conn.close()


def _row_get(row, key, default=None):
    """sqlite3.Row helper: default when the column is missing (older DBs)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def set_account_state(account_id: int, state: str) -> None:
    """Admin lifecycle state: active | suspended | archived (2026-09-02).
    Suspension/archival stops the workspace and blocks portal access;
    restore returns the state to active (workspace stays stopped until
    explicitly unlocked or renewed)."""
    if state not in ("active", "suspended", "archived"):
        raise ValueError(f"Invalid account state: {state}")
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET account_state = ? WHERE id = ?",
            (state, account_id),
        )
        conn.commit()
    finally:
        conn.close()


def count_instances(account_id: int) -> int:
    """Number of live instances for the quota check. Excludes 'deleted' rows AND
    'failed' rows — a failed/reprovisioned instance has its stack + volume rolled
    back and must not consume quota, otherwise a first-attempt failure permanently
    blocks re-provisioning (verified 2026-09-03 with space@steprotech.com)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM instances "
            "WHERE account_id = ? AND status NOT IN ('deleted', 'failed')",
            (account_id,),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


# ---- billing (stripe) ----

def set_subscription(account_id: int, stripe_customer_id: str = "",
                     subscription_id: str = "", status: str = "none",
                     paid_until: int | None = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE accounts SET stripe_customer_id=?, subscription_id=?, "
            "subscription_status=?, paid_until=? WHERE id=?",
            (stripe_customer_id, subscription_id, status, paid_until, account_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_subscription_status(account_id: int, status: str,
                               paid_until: int | None = None,
                               paid_from: int | None = None) -> None:
    conn = get_conn()
    try:
        if paid_until is not None:
            conn.execute(
                "UPDATE accounts SET subscription_status=?, paid_until=?, paid_from=COALESCE(?, paid_from) WHERE id=?",
                (status, paid_until, paid_from, account_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET subscription_status=? WHERE id=?",
                (status, account_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---- instances ----

def create_instance(
    account_id: int,
    stack_name: str,
    environment_id: int,
    environment_name: str,
    port: int,
    domain: str,
    basic_auth_user: str,
    basic_auth_password: str,
    n8n_encryption_key: str,
    stack_id: int | None = None,
    container_id: str = "",
    managed: int = 1,
    status: str = "provisioning",
) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO instances (account_id, stack_name, environment_id, environment_name, "
            "port, domain, basic_auth_user, basic_auth_password, n8n_encryption_key, "
            "stack_id, container_id, managed, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, stack_name, environment_id, environment_name, port, domain,
             basic_auth_user, basic_auth_password, n8n_encryption_key,
             stack_id, container_id, managed, status, now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_instance_by_stack_name(stack_name: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM instances WHERE stack_name = ?", (stack_name,)
        ).fetchone()
    finally:
        conn.close()


def get_instance(instance_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    finally:
        conn.close()


def list_instances(account_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if account_id is not None:
            return conn.execute(
                "SELECT * FROM instances WHERE account_id = ? ORDER BY id DESC", (account_id,)
            ).fetchall()
        return conn.execute("SELECT * FROM instances ORDER BY id DESC").fetchall()
    finally:
        conn.close()


def get_active_instance(account_id: int) -> sqlite3.Row | None:
    """The healthy (provisioned) instance for an account, if any."""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM instances WHERE account_id = ? AND status = 'healthy' "
            "ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()


def update_instance(instance_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE instances SET {cols} WHERE id = ?",
            (*fields.values(), instance_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_instance_failed(instance_id: int, error: str) -> None:
    update_instance(instance_id, status="failed", error=error[:500])


def delete_instance(instance_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
        conn.commit()
    finally:
        conn.close()


# ---- settings ----

def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---- access requests (admin-gated onboarding) ----

def create_access_request(email: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO access_requests (email, status, created_at) VALUES (?, 'requested', ?)",
            (email.lower(), now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_access_request(email: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM access_requests WHERE email = ?", (email.lower(),)
        ).fetchone()
    finally:
        conn.close()


def get_access_request_by_id(request_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM access_requests WHERE id = ?", (request_id,)
        ).fetchone()
    finally:
        conn.close()


def list_access_requests() -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM access_requests ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()


def update_access_request(request_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE access_requests SET {cols} WHERE id = ?",
            (*fields.values(), request_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_access_request(request_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM access_requests WHERE id = ?", (request_id,))
        conn.commit()
    finally:
        conn.close()


# ---- account security: password reset + 2FA (2026-09-02) ----

def set_account_password(account_id: int, password_hash: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
                     (password_hash, account_id))
        conn.commit()
    finally:
        conn.close()


def set_password_reset(account_id: int, token: str, ttl_seconds: int) -> int:
    """Store a reset token (hashed) for an account. Prior tokens are invalidated
    (single active reset at a time). Returns the row id."""
    token_hash = hash_password(token)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM password_resets WHERE account_id = ?", (account_id,))
        cur = conn.execute(
            "INSERT INTO password_resets (account_id, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (account_id, token_hash, now(), int(now()) + ttl_seconds),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_password_reset_by_token(token: str) -> sqlite3.Row | None:
    token_hash = hash_password(token)
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM password_resets WHERE token_hash = ? ORDER BY id DESC LIMIT 1",
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()


def consume_password_reset(reset_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (reset_id,))
        conn.commit()
    finally:
        conn.close()


def _get_totp_secret(account_id: int) -> str:
    conn = get_conn()
    try:
        row = conn.execute("SELECT totp_secret FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return row["totp_secret"] if row else ""
    finally:
        conn.close()


def set_account_totp_secret(account_id: int, secret: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE accounts SET totp_secret = ? WHERE id = ?", (secret, account_id))
        conn.commit()
    finally:
        conn.close()


def set_account_totp_enabled(account_id: int, enabled: bool) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE accounts SET totp_enabled = ? WHERE id = ?",
                     (1 if enabled else 0, account_id))
        conn.commit()
    finally:
        conn.close()


def set_account_email_2fa(account_id: int, enabled: bool) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE accounts SET email_2fa = ? WHERE id = ?",
                     (1 if enabled else 0, account_id))
        conn.commit()
    finally:
        conn.close()


def set_account_email_otp(account_id: int, otp_hash: str, ttl_seconds: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM email_otps WHERE account_id = ?", (account_id,))
        conn.execute(
            "INSERT INTO email_otps (account_id, otp_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (account_id, otp_hash, now(), int(now()) + ttl_seconds),
        )
        conn.commit()
    finally:
        conn.close()


def get_email_otp(account_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM email_otps WHERE account_id = ?", (account_id,)
        ).fetchone()
    finally:
        conn.close()


def clear_email_otp(account_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM email_otps WHERE account_id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


# ---- backups (2026-09-03) ----

def create_backup(account_id: int, instance_id: int, kind: str, filename: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO backups (account_id, instance_id, kind, filename, status, created_at) "
            "VALUES (?, ?, ?, ?, 'creating', ?)",
            (account_id, instance_id, kind, filename, now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_backup(backup_id: int, size_bytes: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE backups SET status = 'ready', size_bytes = ? WHERE id = ?",
            (size_bytes, backup_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_backup(backup_id: int, error: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE backups SET status = 'failed', error = ? WHERE id = ?",
            (error[:500], backup_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_backup(backup_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
    finally:
        conn.close()


def list_backups(account_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if account_id is not None:
            return conn.execute(
                "SELECT * FROM backups WHERE account_id = ? ORDER BY created_at DESC",
                (account_id,),
            ).fetchall()
        return conn.execute("SELECT * FROM backups ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()


# ---- auth events + IP bans (2026-09-03, Steward hardening) ----

def record_auth_event(ip: str, event: str, account_id: int | None = None,
                      detail: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO auth_events (ip, event, account_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ip, event, account_id, detail[:500] if detail else "", now()),
        )
        conn.commit()
    finally:
        conn.close()


def count_auth_events(ip: str, event: str, since_ts: int) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM auth_events WHERE ip = ? AND event = ? AND created_at > ?",
            (ip, event, since_ts),
        ).fetchone()
        return row["n"] if row else 0
    finally:
        conn.close()


def list_auth_events(limit: int = 100) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM auth_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


def ban_ip(ip: str, reason: str = "", expires_at: int | None = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO ip_bans (ip, reason, expires_at, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET reason = excluded.reason, "
            "expires_at = excluded.expires_at, created_at = excluded.created_at",
            (ip, reason[:200] if reason else "", expires_at, now()),
        )
        conn.commit()
    finally:
        conn.close()


def unban_ip(ip: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM ip_bans WHERE ip = ?", (ip,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_ip_banned(ip: str, now_ts: int) -> bool:
    """True if the IP is on the ban list and not expired."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT expires_at FROM ip_bans WHERE ip = ?", (ip,)
        ).fetchone()
        if not row:
            return False
        exp = row["expires_at"]
        if exp is not None and exp < now_ts:
            # expired: auto-clear
            conn.execute("DELETE FROM ip_bans WHERE ip = ?", (ip,))
            conn.commit()
            return False
        return True
    finally:
        conn.close()


def list_ip_bans() -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM ip_bans ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()


# ---------- passkeys (WebAuthn) ----------

def add_passkey(scope: str, account_id: int | None, credential_id: str,
                public_key: bytes, sign_count: int, transports: str = "",
                name: str = "") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO passkeys (scope, account_id, credential_id, public_key,"
            " sign_count, transports, name, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (scope, account_id, credential_id, public_key, sign_count,
             transports or "", name or "", now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_passkeys(scope: str, account_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if scope == "admin":
            return conn.execute(
                "SELECT * FROM passkeys WHERE scope='admin' ORDER BY id DESC").fetchall()
        return conn.execute(
            "SELECT * FROM passkeys WHERE scope='account' AND account_id=? ORDER BY id DESC",
            (account_id,)).fetchall()
    finally:
        conn.close()


def get_passkey_by_credential_id(credential_id: str) -> sqlite3.Row | None:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)).fetchone()
    finally:
        conn.close()


def delete_passkey(scope: str, credential_id: str, account_id: int | None = None) -> bool:
    conn = get_conn()
    try:
        if scope == "admin":
            cur = conn.execute(
                "DELETE FROM passkeys WHERE scope='admin' AND credential_id=?",
                (credential_id,))
        else:
            cur = conn.execute(
                "DELETE FROM passkeys WHERE scope='account' AND account_id=? AND credential_id=?",
                (account_id, credential_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_passkey_challenge(scope: str, account_id: int | None, challenge: bytes,
                           kind: str = "register", ttl: int = 600) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO passkey_challenges (scope, account_id, challenge, kind,"
            " created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (scope, account_id, challenge, kind, now(), now() + ttl))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_latest_passkey_challenge(scope: str, account_id: int | None,
                                 kind: str = "register") -> sqlite3.Row | None:
    conn = get_conn()
    try:
        if scope == "admin":
            return conn.execute(
                "SELECT * FROM passkey_challenges WHERE scope='admin' AND kind=?"
                " ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return conn.execute(
            "SELECT * FROM passkey_challenges WHERE scope='account' AND account_id=?"
            " AND kind=? ORDER BY id DESC LIMIT 1", (account_id, kind)).fetchone()
    finally:
        conn.close()


def consume_passkey_challenge(challenge_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM passkey_challenges WHERE id=?", (challenge_id,))
        conn.commit()
    finally:
        conn.close()
