# Tests: image updates on admin-ATTACHED workspaces (managed=0, 2026-09-03).
# Attached stacks may predate the portal and carry no stack_id; the update path
# now resolves the Portainer stack by name (and persists it), and attach itself
# records the stack id up front, so attached workspaces get the same one-click
# pull-and-update as portal-provisioned ones.
import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def attach_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "att.db"))
    db.init_db()
    return tmp_path


def _instance(account_id, stack_name="chibz", stack_id=None, container_id="abc123"):
    return db.create_instance(
        account_id=account_id,
        stack_name=stack_name,
        environment_id=4,
        environment_name="n8n Server 1",
        port=32768,
        domain=f"{stack_name}.steprotech.com",
        basic_auth_user="", basic_auth_password="", n8n_encryption_key="",
        stack_id=stack_id, container_id=container_id, managed=0,
        status="healthy",
    )


# ---- update path: resolve a missing stack_id by name ----

def test_update_attached_instance_resolves_stack(attach_db, monkeypatch):
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient
    aid = db.create_account("chibz@steprotech.com", "chibz", "Chibz",
                            first_name="C", last_name="Z")
    iid = _instance(aid)
    assert db.get_instance(iid)["stack_id"] is None

    calls = {}
    monkeypatch.setattr(PortainerClient, "list_stacks",
                        lambda self: [{"Id": 19, "Name": "chibz", "EndpointId": 4}])
    def fake_update(self, stack_id, endpoint_id, image, pull=True):
        calls.update(stack_id=stack_id, endpoint_id=endpoint_id,
                     image=image, pull=pull)
    monkeypatch.setattr(PortainerClient, "update_stack_image", fake_update)

    inst = db.get_instance(iid)
    full = backup_ops.update_instance_image(inst, "2.38.3")
    assert full == "n8nio/n8n:2.38.3"
    assert calls == {"stack_id": 19, "endpoint_id": 4,
                     "image": "n8nio/n8n:2.38.3", "pull": True}
    row = db.get_instance(iid)
    assert row["stack_id"] == 19, "resolved stack id must be persisted"
    assert row["image"] == "n8nio/n8n:2.38.3"


def test_update_attached_instance_no_stack_record_raises(attach_db, monkeypatch):
    from app.services import backup_ops
    from app.services.portainer_client import PortainerClient
    aid = db.create_account("stt@steprotech.com", "stt", "Stt",
                            first_name="S", last_name="T")
    iid = _instance(aid, stack_name="stt")
    monkeypatch.setattr(PortainerClient, "list_stacks", lambda self: [])
    with pytest.raises(ValueError, match="no Portainer stack record"):
        backup_ops.update_instance_image(db.get_instance(iid), "2.38.3")


# ---- attach: record the stack id up front ----

class _StubPC:
    def __init__(self, stacks, containers):
        self._stacks = stacks
        self._containers = containers

    def list_containers(self, endpoint_id, all=True):
        return self._containers

    def list_stacks(self):
        return self._stacks

    def list_endpoints(self):
        return []


def _n8n_container(stack_name="chibz", state="running"):
    return {
        "Names": [f"/{stack_name}-n8n-1"],
        "Image": "n8nio/n8n:2.31.6",
        "Id": "abc123",
        "State": state,
        "Ports": [{"PublicPort": 32768}],
        "Labels": {"com.docker.compose.project": stack_name},
    }


def test_attach_records_stack_id(attach_db):
    from app.services.admin_ops import attach_instance
    aid = db.create_account("chibz@steprotech.com", "chibz", "Chibz",
                            first_name="C", last_name="Z")
    pc = _StubPC(stacks=[{"Id": 19, "Name": "chibz", "EndpointId": 4}],
                 containers=[_n8n_container("chibz")])
    res = attach_instance(aid, 4, "chibz", 32768, "chibz.steprotech.com", pc=pc)
    inst = db.get_instance(res["instance_id"])
    assert inst["managed"] == 0
    assert inst["stack_id"] == 19
    assert inst["container_id"] == "abc123"


def test_attach_without_stack_record_keeps_null(attach_db):
    from app.services.admin_ops import attach_instance
    aid = db.create_account("stt@steprotech.com", "stt", "Stt",
                            first_name="S", last_name="T")
    pc = _StubPC(stacks=[], containers=[_n8n_container("stt")])
    res = attach_instance(aid, 4, "stt", 0, "stt.steprotech.com", pc=pc)
    inst = db.get_instance(res["instance_id"])
    assert inst["stack_id"] is None
    assert inst["managed"] == 0
