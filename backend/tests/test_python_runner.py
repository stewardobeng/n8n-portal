# Tests: the admin Python-runner sidecar feature (0.1.63).
# Covers: stack-file injection shape (services mapping position, marker blocks,
# configs section), idempotence/refusals, env-token handling, detach cleanup,
# runners-image lockstep on image updates, and the API routes' auth + errors.
import json
import re
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def runner_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "runner.db"))
    db.init_db()
    return tmp_path


@pytest.fixture(autouse=True)
def _skip_runner_wait(monkeypatch):
    # attach_runner's best-effort "did the container come up" wait sleeps for
    # up to 60s per call; tests fake Portainer and don't need it.
    from app.services import python_runner as pr
    monkeypatch.setattr(pr, "_wait_runner_container",
                        lambda *a, **k: True)


CANONICAL = open(__file__).read() and (
    __import__("pathlib").Path(__file__).resolve().parents[2]
    / "templates" / "n8n-stack-compose.yml"
).read_text()


def _instance(account_id, stack_name="steward", stack_id=77):
    iid = db.create_instance(
        account_id=account_id,
        stack_name=stack_name,
        environment_id=4,
        environment_name="n8n Server 1",
        port=32768,
        domain=f"{stack_name}.steprotech.com",
        basic_auth_user="", basic_auth_password="", n8n_encryption_key="",
        stack_id=stack_id, container_id="abc123", managed=1,
        status="healthy",
    )
    return db.get_instance(iid)


def _account(email):
    return db.create_account(email, email.split("@")[0], "Name",
                             first_name="F", last_name="L")


class FakePC:
    """Captures update_stack_file calls; serves a mutable stack file."""

    def __init__(self, content=None):
        self.stack_file = content if content is not None else CANONICAL
        self.stack_env = [{"name": "N8N_HOST", "value": "x"}]
        self.puts = []

    def get_stack_file(self, stack_id):
        return self.stack_file

    def get_stack(self, stack_id):
        return {"Env": [dict(e) for e in self.stack_env]}

    def update_stack_file(self, stack_id, endpoint_id, content, env, **kw):
        self.puts.append({"content": content, "env": env})
        self.stack_file = content
        self.stack_env = [dict(e) for e in env]

    def list_containers(self, endpoint_id, all=True):
        return []


@pytest.fixture
def fake_pc(monkeypatch):
    from app.services.portainer_client import PortainerClient
    pc = FakePC()
    monkeypatch.setattr(PortainerClient, "__new__", lambda cls, *a, **k: pc)
    # attach_runner builds PortainerClient() then calls methods on it; the
    # __new__ hijack makes every instance the same FakePC.
    return pc


# ---------- injection shape ----------

def test_attach_injects_service_inside_services_mapping(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("steward@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)
    content = fake_pc.puts[0]["content"]

    # service must sit BEFORE the top-level volumes: line (not nested under it)
    assert content.find("runners:") < content.find("\nvolumes:"), \
        "runners service must live inside the services: mapping"
    # markers present exactly once
    for marker in ("portal-runner:env-start", "portal-runner:service-start",
                   "portal-runner:configs-start"):
        assert content.count(marker) == 1
    # configs section exists as a top-level key
    assert "\nconfigs:\n" in content
    # runners image version-locked to the n8n line
    import re
    n8n_tag = re.search(r"image:\s*n8nio/n8n:([^\s]+)", content).group(1)
    runners_tag = re.search(r"image:\s*n8nio/runners:([^\s]+)", content).group(1)
    assert n8n_tag == runners_tag
    # no ports published on the runners service
    svc = content[content.find("runners:"):content.find("portal-runner:service-end")]
    assert "ports:" not in svc


def test_attach_adds_auth_token_env_and_n8n_flags(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("second@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)
    env = {e["name"]: e["value"] for e in fake_pc.puts[0]["env"]}
    # a fresh random token, not a placeholder
    tok = env.get("N8N_RUNNERS_AUTH_TOKEN", "")
    assert len(tok) >= 32, "runner auth token must be a real random secret"
    # the rest of the stack env is preserved
    assert env.get("N8N_HOST") == "x"
    content = fake_pc.puts[0]["content"]
    assert "N8N_RUNNERS_MODE=external" in content
    assert "N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0" in content


def test_attach_config_json_opens_stdlib_only(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("third@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)
    content = fake_pc.puts[0]["content"]
    m = re.search(r"content: '(\{.*\})'", content)
    assert m, "inline config content must be present"
    cfg = json.loads(m.group(1))
    py = [r for r in cfg["task-runners"] if r["runner-type"] == "python"]
    assert py and py[0]["env-overrides"]["N8N_RUNNERS_STDLIB_ALLOW"] == "*"
    assert py[0]["env-overrides"]["N8N_RUNNERS_EXTERNAL_ALLOW"] == ""


def test_attach_is_refused_twice(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("fourth@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)
    with pytest.raises(ValueError, match="already attached"):
        pr.attach_runner(db.get_instance(inst["id"]))
    assert db.get_instance(inst["id"])["python_runner"] == 1


def test_attach_refused_on_attached_workspace(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("fifth@steprotech.com")
    iid = db.create_instance(
        account_id=aid, stack_name="ext", environment_id=4,
        environment_name="n8n Server 1", port=32769,
        domain="ext.steprotech.com", basic_auth_user="", basic_auth_password="",
        n8n_encryption_key="", stack_id=88, container_id="c2", managed=0,
        status="healthy")
    with pytest.raises(ValueError, match="attached \\(externally managed\\)"):
        pr.attach_runner(db.get_instance(iid))


def test_attach_refused_when_configs_section_exists(runner_db, monkeypatch):
    from app.services import python_runner as pr
    from app.services.portainer_client import PortainerClient
    pc = FakePC(CANONICAL + "\nconfigs:\n  other:\n    file: ./x.json\n")
    monkeypatch.setattr(PortainerClient, "__new__", lambda cls, *a, **k: pc)
    aid = _account("sixth@steprotech.com")
    inst = _instance(aid)
    with pytest.raises(ValueError, match="configs section"):
        pr.attach_runner(inst)
    assert db.get_instance(inst["id"])["python_runner"] == 0, \
        "failed attach must not flip the flag"


def test_attach_refused_without_stack_id(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("seventh@steprotech.com")
    inst = _instance(aid, stack_id=None)
    with pytest.raises(ValueError, match="no Portainer stack record"):
        pr.attach_runner(inst)


# ---------- detach ----------

def test_detach_restores_the_original_file(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("eighth@steprotech.com")
    inst = _instance(aid)
    before = fake_pc.stack_file
    pr.attach_runner(inst)
    pr.detach_runner(db.get_instance(inst["id"]))
    after = fake_pc.stack_file
    assert "portal-runner:service-start" not in after
    assert "portal-runner:env-start" not in after
    assert "portal-runner:configs-start" not in after
    assert "runners:" not in after, "runners service must be fully removed"
    assert "N8N_RUNNERS_MODE=external" not in after
    env = {e["name"]: e["value"] for e in fake_pc.stack_env}
    assert "N8N_RUNNERS_AUTH_TOKEN" not in env, "token env must be removed"
    # the file is byte-identical to the pre-attach state apart from whitespace
    import re as _re
    norm = lambda s: _re.sub(r"[ \t]+\n", "\n", _re.sub(r"\n{3,}", "\n\n", s)).strip()
    assert norm(after) == norm(before)
    assert db.get_instance(inst["id"])["python_runner"] == 0


def test_detach_refused_without_attach(runner_db, fake_pc):
    from app.services import python_runner as pr
    aid = _account("ninth@steprotech.com")
    inst = _instance(aid)
    with pytest.raises(ValueError, match="No Python runner is attached"):
        pr.detach_runner(inst)


# ---------- image-update lockstep ----------

def test_update_stack_image_retags_runners(runner_db, fake_pc, monkeypatch):
    from app.services import python_runner as pr
    from app.services.portainer_client import PortainerClient
    aid = _account("tenth@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)  # stack file now carries n8n:latest + runners:latest

    def fake_get_file(stack_id):          # instance attr: NOT bound, no self
        return fake_pc.stack_file
    def fake_get_stack(stack_id):
        return {"Env": [dict(e) for e in fake_pc.stack_env]}
    def fake_req(method, path, **kw):
        assert method == "PUT" and path.startswith("/stacks/77")
        fake_pc.stack_file = kw["json"]["stackFileContent"]
        fake_pc.stack_env = kw["json"]["env"]
        class R: status_code = 204
        return R()
    # patch ON THE INSTANCE: update_stack_image is invoked with fake_pc as self
    monkeypatch.setattr(fake_pc, "get_stack_file", fake_get_file, raising=False)
    monkeypatch.setattr(fake_pc, "get_stack", fake_get_stack, raising=False)
    monkeypatch.setattr(fake_pc, "_req", fake_req, raising=False)

    # __new__ is hijacked to FakePC, so invoke the real method explicitly
    PortainerClient.update_stack_image(fake_pc, 77, 4, "2.51.0")
    assert "image: n8nio/n8n:2.51.0" in fake_pc.stack_file
    assert "image: n8nio/runners:2.51.0" in fake_pc.stack_file, \
        "runners tag must follow the n8n tag on image updates"


# ---------- API routes ----------

def test_routes_require_admin(runner_db):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.post("/api/v1/admin/instances/1/python-runner/attach")
    assert r.status_code in (401, 403)


def test_attach_route_404_and_validation(runner_db, fake_pc, monkeypatch):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    c = TestClient(app)
    hdr = {"Authorization": "Bearer " + security.create_access_token("admin")}
    r = c.post("/api/v1/admin/instances/99999/python-runner/attach", headers=hdr)
    assert r.status_code == 404
    aid = _account("eleventh@steprotech.com")
    iid = db.create_instance(
        account_id=aid, stack_name="nostack", environment_id=4,
        environment_name="n8n Server 1", port=32770,
        domain="nostack.steprotech.com", basic_auth_user="",
        basic_auth_password="", n8n_encryption_key="", stack_id=None,
        container_id="c3", managed=1, status="healthy")
    r = c.post(f"/api/v1/admin/instances/{iid}/python-runner/attach", headers=hdr)
    assert r.status_code == 422


def test_full_cycle_through_routes(runner_db, fake_pc, monkeypatch):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    from app.services.portainer_client import PortainerClient
    # the attach path builds its own PortainerClient inside the app process;
    # hijack __new__ the same way the unit tests do
    monkeypatch.setattr(PortainerClient, "__new__", lambda cls, *a, **k: fake_pc)
    c = TestClient(app)
    hdr = {"Authorization": "Bearer " + security.create_access_token("admin")}
    aid = _account("twelfth@steprotech.com")
    inst = _instance(aid)
    r = c.post(f"/api/v1/admin/instances/{inst['id']}/python-runner/attach",
               headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert db.get_instance(inst["id"])["python_runner"] == 1
    r = c.post(f"/api/v1/admin/instances/{inst['id']}/python-runner/detach",
               headers=hdr)
    assert r.status_code == 200, r.text
    assert db.get_instance(inst["id"])["python_runner"] == 0


# ---------- legacy stack files (no N8N_RUNNERS_ENABLED line) ----------

LEGACY = """services:
  n8n:
    image: n8nio/n8n:2.40.3
    restart: unless-stopped
    cpus: "0.50"
    mem_limit: "1g"
    mem_reservation: "512m"

    ports:
      - "${N8N_PORT}:5678"

    environment:
      # Database (SQLite stored in volume)
      - DB_TYPE=sqlite
      - DB_SQLITE_VACUUM_ON_STARTUP=true

      # Encryption (unique per tenant)
      - N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}

      # Timezone
      - GENERIC_TIMEZONE=${GENERIC_TIMEZONE}

    volumes:
      - n8n_data:/home/node/.n8n

volumes:
  n8n_data:
"""


def test_attach_on_legacy_stack_without_runners_line(runner_db, monkeypatch):
    from app.services import python_runner as pr
    from app.services.portainer_client import PortainerClient
    pc = FakePC(LEGACY)
    monkeypatch.setattr(PortainerClient, "__new__", lambda cls, *a, **k: pc)
    aid = _account("legacy@steprotech.com")
    inst = _instance(aid)
    pr.attach_runner(inst)
    content = pc.puts[0]["content"]
    # runners flag + block injected, exactly once
    assert content.count("- N8N_RUNNERS_ENABLED=true") == 1
    assert "portal-runner:env-start" in content
    # runners service sits inside services: (before top-level volumes:)
    assert content.find("runners:") < content.find("\nvolumes:")
    assert db.get_instance(inst["id"])["python_runner"] == 1
    # and detach restores byte-equivalence
    pr.detach_runner(db.get_instance(inst["id"]))
    after = pc.stack_file
    norm = lambda s: re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", s)).strip()
    assert norm(after) == norm(LEGACY)
