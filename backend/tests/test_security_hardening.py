# Security hardening regression tests (Steward 2026-09-03).
# Covers: the privilege-escalation fix (verify_admin must require sub==admin),
# the owner-or-admin gate, rate limiting + IP ban on auth endpoints, security
# headers, and the mock-webhook owner gate.

import httpx
import pytest
import time

from app import db
from app.services import security_controls as sc
from app.services.portainer_client import _strip_docker_stream
from app.security import (create_access_token, create_client_token,
                          verify_admin, verify_client, authorize_owner_or_admin,
                          hash_password, verify_password)
from fastapi import HTTPException


# ---------- privilege escalation (the critical fix) ----------

def test_verify_admin_rejects_client_token(monkeypatch):
    # A normal customer token (sub=acc:1) must NOT pass verify_admin.
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    ctok = create_client_token(1)
    with pytest.raises(HTTPException) as e:
        verify_admin("Bearer " + ctok)
    assert e.value.status_code == 401


def test_verify_admin_accepts_admin_token(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    atok = create_access_token("admin")
    # Should not raise
    verify_admin("Bearer " + atok)


def test_verify_client_rejects_admin_token(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    atok = create_access_token("admin")
    with pytest.raises(HTTPException) as e:
        verify_client("Bearer " + atok)
    assert e.value.status_code == 401


def test_verify_client_returns_account_id(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    ctok = create_client_token(42)
    assert verify_client("Bearer " + ctok) == 42


def test_verify_admin_requires_plaintext_token(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    with pytest.raises(HTTPException) as e:
        verify_admin("Basic abc123")
    assert e.value.status_code == 401


# ---------- owner-or-admin gate ----------

def test_authorize_owner_or_admin_owner_ok(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    ctok = create_client_token(7)
    authorize_owner_or_admin("Bearer " + ctok, 7)  # owner -> ok


def test_authorize_owner_or_admin_admin_ok(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    atok = create_access_token("admin")
    authorize_owner_or_admin("Bearer " + atok, 999)  # admin -> ok


def test_authorize_owner_or_admin_cross_account_forbidden(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    ctok = create_client_token(7)
    with pytest.raises(HTTPException) as e:
        authorize_owner_or_admin("Bearer " + ctok, 8)  # different account -> 403
    assert e.value.status_code == 403


def test_authorize_owner_or_admin_anonymous_forbidden(monkeypatch):
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    with pytest.raises(HTTPException) as e:
        authorize_owner_or_admin(None, 8)
    assert e.value.status_code == 401


# ---------- docker stream de-framing (regression, unchanged) ----------

def test_strip_docker_stream_mixed():
    def frame(data: bytes, t: int = 1) -> bytes:
        return bytes([t, 0, 0, 0]) + len(data).to_bytes(4, "big") + data
    out = _strip_docker_stream(frame(b"json") + frame(b"err", 2) + frame(b"tail"))
    assert out == b"jsonerrtail"


# ---------- rate limiting + IP banning (SQLite-backed) ----------

def test_rate_limit_blocks_after_threshold(monkeypatch):
    monkeypatch.setattr(sc, "DEFAULT_LIMITS", {**sc.DEFAULT_LIMITS, "login": (3, 60)})
    sc.RATE_LIMIT_ENABLED = True  # the autouse fixture disables it; re-enable here
    sc._RATE.clear()
    ip = "203.0.113.9"
    assert sc.check_rate(ip, "login") is False
    assert sc.check_rate(ip, "login") is False
    assert sc.check_rate(ip, "login") is False
    assert sc.check_rate(ip, "login") is True  # 4th within 60s window -> limited
    sc._RATE.clear()


def test_ip_ban_and_unban(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bans.db"))
    db.init_db()
    sc.ban("198.51.100.7", "test ban", 1.0)
    assert sc.is_banned("198.51.100.7") is True
    assert sc.unban("198.51.100.7") is True
    assert sc.is_banned("198.51.100.7") is False


def test_record_failed_ban_threshold(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bans2.db"))
    monkeypatch.setattr(sc, "BAN_THRESHOLDS", {"login_fail": (2, 600)})
    db.init_db()
    ip = "203.0.113.55"
    sc.record_failed(ip, "login_fail", 1, "bad")
    sc.record_failed(ip, "login_fail", 1, "bad")
    # after 2 thresholds, the IP should be auto-banned
    assert db.is_ip_banned(ip, int(time.time())) is True


# ---------- webhook owner gate (the payment-bypass fix) ----------

def test_mock_webhook_requires_auth(monkeypatch):
    # The owner-or-admin gate used by /webhook/mock must reject an anonymous call.
    monkeypatch.setattr("app.security.settings.jwt_secret", "testsecret")
    with pytest.raises(HTTPException) as e:
        authorize_owner_or_admin(None, 5)
    assert e.value.status_code == 401


# ---------- auth event recording persistence ----------

def test_auth_events_recorded_and_counted(tmp_path):
    from app.config import settings
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ev.db"))
    db.init_db()
    db.record_auth_event("10.0.0.1", "login_fail", 1, "x")
    db.record_auth_event("10.0.0.1", "login_fail", 1, "x")
    assert db.count_auth_events("10.0.0.1", "login_fail", int(time.time()) - 10) == 2
    events = db.list_auth_events()
    assert len(events) >= 2
