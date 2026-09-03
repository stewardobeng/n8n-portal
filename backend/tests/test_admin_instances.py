# Admin instances list endpoint (Steward 2026-09-03) — the admin Backups page now
# triggers per-workspace backups from a single /admin/instances list, so it must
# be admin-gated and return instances with an account label.

import os

from app import db
from app.config import settings


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai.db"))
    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT INTO accounts (email, username, password_hash, account_state, subscription_status, quota, created_at) VALUES (?,?,?,?,?,?,?)",
                 ("ai@steprotech.com", "aiuser", "x", "active", "active", 1, int(__import__("time").time())))
    conn.commit()
    aid = conn.execute("SELECT id FROM accounts WHERE email='ai@steprotech.com'").fetchone()["id"]
    conn.close()
    iid = db.create_instance(account_id=aid, stack_name="aiws", environment_id=8,
                             environment_name="test", port=33000, domain="aiws.steprotech.com",
                             basic_auth_user="ai@steprotech.com", basic_auth_password="AiPass123",
                             n8n_encryption_key="e" * 32)
    db.update_instance(iid, status="healthy", stack_id=2)
    return aid, iid


def test_admin_instances_is_admin_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    aid, iid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    # no token -> 401/403
    r = client.get("/api/v1/admin/instances")
    assert r.status_code in (401, 403), r.status_code


def test_admin_instances_returns_all_with_label(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_access_token
    aid, iid = _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + create_access_token("admin")}
    r = client.get("/api/v1/admin/instances", headers=h)
    assert r.status_code == 200, r.text
    insts = r.json()["instances"]
    assert len(insts) >= 1
    inst = insts[0]
    # must carry the fields the Backups page needs to render a trigger
    for f in ["id", "stack_name", "domain", "status", "locked", "account_id", "account_display", "managed"]:
        assert f in inst, f"missing field {f} in {list(inst.keys())}"
    assert inst["account_display"] == "aiuser"
