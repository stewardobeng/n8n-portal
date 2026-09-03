# Tests for the 0.1.50 n8n update work (2026-09-03):
#  * admin image updates now PULL from the registry first (Portainer
#    "pullImage" flag) so uncached tags deploy, with a long HTTP timeout
#  * friendly 422 when the requested tag genuinely does not exist on Docker Hub
#  * GET /admin/n8n-releases serves the latest official n8n release (admin-only,
#    cached, Docker Hub tags filtered to clean X.Y.Z versions)
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def account(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "rel.db"))
    db.init_db()
    return db.create_account("rel@steprotech.com", "reluser", "Rel User",
                             first_name="Rel", last_name="User")


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "relapi.db"))
    db.init_db()
    return TestClient(app)


def _seed(tmp_path, monkeypatch):
    from app.security import create_access_token, create_client_token
    client = _client(tmp_path, monkeypatch)
    aid = db.create_account("relapi@steprotech.com", "relapi", "Rel API",
                            first_name="Rel", last_name="API")
    iid = db.create_instance(account_id=aid, stack_name="relapiws", environment_id=8,
                             environment_name="test", port=33021,
                             domain="relapiws.steprotech.com",
                             basic_auth_user="relapi@steprotech.com",
                             basic_auth_password="RelApiPass123",
                             n8n_encryption_key="e" * 32)
    db.update_instance(iid, status="healthy", stack_id=3)
    atoken = create_access_token("admin")
    ctoken = create_client_token(aid)
    return client, aid, iid, atoken, ctoken


def _h(token):
    return {"Authorization": "Bearer " + token}


# ---- release service ----

def test_release_service_picks_newest_semver(monkeypatch):
    from app.services import n8n_releases as nr
    tags = [
        {"name": "latest", "last_updated": "2026-09-03T00:00:00Z"},
        {"name": "nightly", "last_updated": "2026-09-03T00:00:00Z"},
        {"name": "beta", "last_updated": "2026-09-03T00:00:00Z"},
        {"name": "1.2.3", "last_updated": "2026-01-01T00:00:00Z"},
        {"name": "2.9.9", "last_updated": "2026-02-01T00:00:00Z"},
        {"name": "2.31.6", "last_updated": "2026-03-01T00:00:00Z"},
        {"name": "2.31.7", "last_updated": "2026-04-01T00:00:00Z"},
    ]

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": tags}

    calls = {"n": 0}

    def fake_get(*a, **kw):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(nr.httpx, "get", fake_get)
    data = nr.latest_release(force=True)
    assert data["latest"]["tag"] == "2.31.7", data
    recent = [t["tag"] for t in data["recent"]]
    assert recent[0] == "2.31.7" and "latest" not in recent
    # second call within TTL is served from cache (no second HTTP hit)
    nr.latest_release()
    assert calls["n"] == 1


def test_release_service_survives_offline_with_stale_cache(monkeypatch):
    from app.services import n8n_releases as nr

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"name": "2.30.0", "last_updated": "x"}]}

    monkeypatch.setattr(nr.httpx, "get", lambda *a, **kw: FakeResp())
    first = nr.latest_release(force=True)
    assert first["latest"]["tag"] == "2.30.0"

    def boom(*a, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(nr.httpx, "get", boom)
    # unreachable but stale cache exists -> serve cache, do not raise
    again = nr.latest_release(force=True)
    assert again["latest"]["tag"] == "2.30.0"
    # no cache at all -> propagate the failure
    monkeypatch.setattr(nr, "_CACHE", {"at": 0.0, "data": None})
    with pytest.raises(RuntimeError):
        nr.latest_release(force=True)


# ---- releases endpoint ----

def test_releases_endpoint_admin_only(tmp_path, monkeypatch):
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    assert client.get("/api/v1/admin/n8n-releases").status_code == 401
    assert client.get("/api/v1/admin/n8n-releases",
                      headers=_h(ctoken)).status_code == 401


def test_releases_endpoint_returns_latest_for_admin(tmp_path, monkeypatch):
    from app.services import n8n_releases as nr
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(nr, "latest_release",
                        lambda force=False: {"latest": {"tag": "2.38.3"},
                                             "recent": [], "checked_at": 1})
    r = client.get("/api/v1/admin/n8n-releases", headers=_h(atoken))
    assert r.status_code == 200
    assert r.json()["latest"]["tag"] == "2.38.3"


def test_releases_endpoint_502_on_failure(tmp_path, monkeypatch):
    from app.services import n8n_releases as nr
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)

    def boom(force=False):
        raise RuntimeError("hub down")

    monkeypatch.setattr(nr, "latest_release", boom)
    assert client.get("/api/v1/admin/n8n-releases",
                      headers=_h(atoken)).status_code == 502


# ---- pull flag on the Portainer stack update ----

def test_update_stack_image_sends_pull_image(tmp_path, monkeypatch):
    from app.services.portainer_client import PortainerClient
    pc = PortainerClient(base_url="http://portainer.test", token="t")
    monkeypatch.setattr(pc, "get_stack_file",
                        lambda sid: "services:\n  n8n:\n    image: n8nio/n8n:2.31.6\n")
    monkeypatch.setattr(pc, "get_stack", lambda sid: {"Env": []})
    captured = {}

    def fake_req(method, path, **kw):
        captured.update(method=method, path=path, kw=kw)
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(pc, "_req", fake_req)
    full = pc.update_stack_image(3, 8, "2.38.3")
    assert full == "n8nio/n8n:2.38.3"
    assert "pullImage=true" in captured["path"], captured
    assert captured["kw"]["timeout"] == 600.0
    assert "n8nio/n8n:2.38.3" in captured["kw"]["json"]["stackFileContent"]
    assert captured["kw"]["json"]["pullImage"] is True


def test_update_stack_image_can_skip_pull(tmp_path, monkeypatch):
    from app.services.portainer_client import PortainerClient
    pc = PortainerClient(base_url="http://portainer.test", token="t")
    monkeypatch.setattr(pc, "get_stack_file", lambda sid: "image: n8nio/n8n:2.31.6\n")
    monkeypatch.setattr(pc, "get_stack", lambda sid: {"Env": []})
    captured = {}

    def fake_req(method, path, **kw):
        captured.update(path=path)
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(pc, "_req", fake_req)
    pc.update_stack_image(3, 8, "2.31.6", pull=False)
    assert "pullImage" not in captured["path"]


# ---- update-image route: registry miss maps to a friendly 422 ----

def test_update_image_route_maps_unknown_tag(tmp_path, monkeypatch):
    from app.services import backup_ops
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)

    def raiser(instance, image):
        raise RuntimeError("docker API error: manifest unknown for n8nio/n8n:9.9.9")

    monkeypatch.setattr(backup_ops, "update_instance_image", raiser)
    r = client.post(f"/api/v1/admin/instances/{iid}/update-image",
                    headers=_h(atoken), json={"image": "9.9.9"})
    assert r.status_code == 422
    assert "does not exist on Docker Hub" in r.json()["detail"]


def test_update_image_route_other_errors_502(tmp_path, monkeypatch):
    from app.services import backup_ops
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)

    def raiser(instance, image):
        raise RuntimeError("timeout talking to Portainer")

    monkeypatch.setattr(backup_ops, "update_instance_image", raiser)
    r = client.post(f"/api/v1/admin/instances/{iid}/update-image",
                    headers=_h(atoken), json={"image": "2.38.3"})
    assert r.status_code == 502


def test_update_image_route_missing_instance_404(tmp_path, monkeypatch):
    client, aid, iid, atoken, ctoken = _seed(tmp_path, monkeypatch)
    assert client.post("/api/v1/admin/instances/99999/update-image",
                       headers=_h(atoken), json={"image": "2.38.3"}).status_code == 404
