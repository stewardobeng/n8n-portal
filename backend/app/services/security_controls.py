# Security controls: rate limiting, IP banning, and client-IP extraction.
# Steward 2026-09-03: "assess the entire project and harden security and safety
# measures like rate limiting, ip banning etc to avoid bad actors."
#
# Design notes:
#   * The app sits behind NPM (nginx-proxy-manager) which sets X-Real-IP and
#     X-Forwarded-For. However port 8788 is ALSO published directly on the host,
#     so the real client IP must be read from the trusted proxy header when
#     present, falling back to the peer address.
#   * Rate limiting is an in-memory sliding window keyed by client IP (and a
#     route/scope suffix). It is intentionally per-process — the backend runs a
#     single uvicorn worker (start command is `uvicorn app.main:app`, no --workers),
#     so a process-local limiter is consistent. If the app is ever scaled to
#     multiple workers, swap in a shared store (Redis).
#   * Failed-login and MFA-failure events are recorded to SQLite (auth_events) so
#     both the rate limiter and the persistent IP-ban list can auto-ban abusive IPs
#     that survive a restart (the in-memory limiter alone resets on restart).
#
# Endpoints that should apply per-IP limiting are wired by adding a dependency
# or calling the helper directly (see main.py). Simple, dependency-free, stdlib.

import time
import threading
import re

import httpx

from .. import db
from ..config import settings

# ---- in-memory sliding-window rate limiter ----

_RATE_LOCK = threading.Lock()
# key -> list[int] of event timestamps (monotonic)
_RATE = {}
# Master switch. Tests toggle this OFF (conftest autouse fixture) so the
# same-client-IP TestClient requests don't exhaust a bucket across a suite.
RATE_LIMIT_ENABLED = True
# default buckets: (limit, window_seconds)
DEFAULT_LIMITS = {
    "login": (10, 60),          # 10 login failures per minute per IP
    "register": (5, 3600),      # 5 registrations per hour per IP
    "forgot": (5, 3600),        # 5 password-reset requests per hour per IP
    "mfa": (10, 60),            # 10 MFA attempts per minute per IP
    "verify": (8, 60),          # 8 access-token (XXXX-XXXX) guesses per minute per IP
    "check": (15, 60),          # 15 /auth/check per minute per IP (enumeration sweep cap)
    "global": (120, 60),        # 120 requests per minute per IP (crude abuse cap)
}
# Hard ban thresholds for non-login repeated abuse
BAN_THRESHOLDS = {
    "login_fail": (20, 15 * 60),   # 20 failed logins in 15 min -> 1h ban
    "mfa_fail": (15, 15 * 60),     # 15 failed MFA in 15 min -> 1h ban
}
LOGIN_FAIL_BAN_SECONDS = 3600


def _now() -> float:
    return time.time()


def client_ip(request) -> str:
    """Best-effort client IP behind NPM.

    NPM (openresty) sets X-Real-IP and X-Forwarded-For. Trust X-Real-IP first
    (it is set by the proxy from the real peer), then the first X-Forwarded-For
    hop, then the direct peer. NOTE: because port 8788 is also published directly,
    a direct caller can spoof X-Forwarded-For/-Real-IP; that is why the primary
    hardening is to restrict 8788 to the proxy (see handoff) AND we ban/limit on
    the extracted IP as defense-in-depth."""
    try:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    except Exception:
        pass
    try:
        return request.client.host if request.client else "0.0.0.0"
    except Exception:
        return "0.0.0.0"


def check_rate(key: str, scope: str, limit: int | None = None,
               window: int | None = None) -> bool:
    """Return True if the key is over its limit (rate-limited). Records a hit
    whether or not it is over. key is the client IP, scope is the bucket name."""
    if not RATE_LIMIT_ENABLED:
        return False
    if limit is None or window is None:
        limit, window = DEFAULT_LIMITS.get(scope, DEFAULT_LIMITS["global"])
    k = f"{scope}:{key}"
    now = _now()
    with _RATE_LOCK:
        hits = _RATE.get(k, [])
        # prune old
        cutoff = now - window
        hits = [t for t in hits if t > cutoff]
        over = len(hits) >= limit
        hits.append(now)
        _RATE[k] = hits
    return over


def record_failed(ip: str, event: str, account_id: int | None = None,
                  detail: str = "") -> None:
    """Record a failed auth + auto-ban the IP if it crosses the threshold."""
    db.record_auth_event(ip, event, account_id, detail)
    threshold, window = BAN_THRESHOLDS.get(event, (None, None))
    if threshold and threshold > 0:
        since = int(time.time()) - window
        count = db.count_auth_events(ip, event, since)
        if count >= threshold:
            expires = int(time.time()) + LOGIN_FAIL_BAN_SECONDS
            db.ban_ip(ip, f"auto: {count} {event} in {window}s", expires)
            _rate_clear(ip)


def record_ok(ip: str, event: str, account_id: int | None = None) -> None:
    db.record_auth_event(ip, event, account_id)


def _rate_clear(ip: str) -> None:
    with _RATE_LOCK:
        for k in list(_RATE):
            if k.endswith(":" + ip):
                _RATE.pop(k, None)


def is_banned(ip: str) -> bool:
    return db.is_ip_banned(ip, int(time.time()))


def ban(ip: str, reason: str, hours: float) -> None:
    db.ban_ip(ip, reason, int(time.time()) + int(hours * 3600))
    _rate_clear(ip)


def unban(ip: str) -> bool:
    _rate_clear(ip)
    return db.unban_ip(ip)


def check_ip_ban(request) -> None:
    """Raise an HTTPException (503/429-ish) if the client IP is banned."""
    from fastapi import HTTPException
    ip = client_ip(request)
    if is_banned(ip):
        raise HTTPException(403, "Access temporarily blocked. Contact support.")


def extract_ip(request) -> str:
    """Expose the IP so route handlers can gate / record auth events without
    re-deriving it (they already receive Request)."""
    return client_ip(request)
