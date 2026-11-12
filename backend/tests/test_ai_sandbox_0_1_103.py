"""0.1.103: attach/detach the shared n8n AI sandbox for one workspace.

The feature is Steward's decision of 2026-11-12: a SELLABLE capability the ADMIN grants per workspace
(like the Python runner), backed by ONE shared n8n sandbox service for the whole fleet, with a tenant key
per workspace. The user supplies their own model in their instance; the portal only wires the sandbox.

These tests use a fake Portainer and a fake sandbox service, so nothing is deployed and no real key is
minted. They pin the behaviour that matters:

  * the four sandbox env vars plus the SearXNG URL land INSIDE the n8n service environment
  * only this workspace's tenant key is written, and it never comes back in an API response
  * detach removes exactly the inserted block and leaves the rest of the file byte-identical
  * a failing sandbox service aborts the attach with the stack file untouched
"""
import os

import pytest

from app.config import settings
from app.services import ai_sandbox


STACK = '''services:
  n8n:
    image: n8nio/n8n:3.2.0
    environment:
      - N8N_RUNNERS_ENABLED=true
      - N8N_RUNNERS_MODE=external
      - NODE_OPTIONS=--max-old-space-size=768
    volumes:
      - n8n_data:/home/node/.n8n
  runners:
    image: n8nio/runners:3.2.0
    mem_limit: "384m"
volumes:
  n8n_data:
'''


class FakeInstance(dict):
    """Mimics sqlite3.Row's `.keys()` check used by db._row_get."""

    def keys(self):
        return list(super().keys())


class FakePortainer:
    def __init__(self):
        self.content = STACK
        self.pushes = 0

    def get_stack(self, stack_id):
        return {"Env": [{"name": "N8N_ENCRYPTION_KEY", "value": "x"}]}

    def get_stack_file(self, stack_id):
        return self.content

    def update_stack_file(self, stack_id, env_id, content, env_vars):
        self.content = content
        self.pushes += 1


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(settings, "ai_sandbox_url", "https://sandbox.example.com")
    monkeypatch.setattr(settings, "ai_sandbox_admin_key", "admin-test-key")
    monkeypatch.setattr(settings, "ai_sandbox_search_url", "https://sandbox-search.example.com")
    monkeypatch.setattr(settings, "ai_sandbox_model_api_key", "")
    monkeypatch.setattr(settings, "ai_sandbox_model", "")
    monkeypatch.setattr(settings, "ai_sandbox_model_url", "")
    monkeypatch.setattr(ai_sandbox, "PortainerClient", FakePortainer)
    monkeypatch.setattr(ai_sandbox, "service_health",
                        lambda: {"configured": True, "reachable": True})
    monkeypatch.setattr(ai_sandbox, "_mint_tenant", lambda inst: ("tenant-abc", "secret-key-123"))
    monkeypatch.setattr(ai_sandbox, "_drop_tenant", lambda tid: None)


def _inst(attached=0, managed=1):
    return FakeInstance({"id": 24, "stack_name": "steward", "environment_id": 8,
                         "stack_id": 94, "ai_sandbox": attached, "ai_sandbox_tenant_id": "",
                         "managed": managed})


def _rows(monkeypatch):
    """Capture db.update_instance calls instead of touching a database."""
    calls = []
    monkeypatch.setattr(ai_sandbox.db, "update_instance", lambda iid, **kw: calls.append((iid, kw)))
    monkeypatch.setattr(ai_sandbox.db, "_row_get",
                        lambda row, key, default=None: row.get(key, default))
    return calls


def test_attach_writes_the_sandbox_env_inside_the_n8n_service(monkeypatch):
    calls = _rows(monkeypatch)
    inst = _inst()
    result = ai_sandbox.attach(inst)
    assert result["attached"] is True and result["tenant_id"] == "tenant-abc"

    # verify the transform the module applies
    new = ai_sandbox._inject(STACK, "secret-key-123")
    for needle in ("N8N_INSTANCE_AI_SANDBOX_ENABLED=true",
                   "N8N_INSTANCE_AI_SANDBOX_PROVIDER=n8n-sandbox",
                   "N8N_SANDBOX_SERVICE_URL=https://sandbox.example.com",
                   "N8N_SANDBOX_SERVICE_API_KEY=secret-key-123",
                   "N8N_INSTANCE_AI_SEARXNG_URL=https://sandbox-search.example.com"):
        assert needle in new
    # the block belongs to the n8n service, not to the runners sidecar
    n8n_part, runners_part = new.split("  runners:", 1)
    assert "N8N_SANDBOX_SERVICE_API_KEY" in n8n_part
    assert "N8N_SANDBOX_SERVICE_API_KEY" not in runners_part
    assert calls and calls[0][1]["ai_sandbox"] == 1
    assert calls[0][1]["ai_sandbox_tenant_id"] == "tenant-abc"


def test_attach_never_returns_the_key(monkeypatch):
    _rows(monkeypatch)
    result = ai_sandbox.attach(_inst())
    blob = str(result)
    assert "secret-key-123" not in blob
    assert "api_key" not in blob


def test_attach_refuses_a_second_time(monkeypatch):
    _rows(monkeypatch)
    with pytest.raises(ValueError) as exc:
        ai_sandbox.attach(_inst(attached=1))
    assert "already attached" in str(exc.value)


def test_attach_refuses_an_unmanaged_workspace(monkeypatch):
    _rows(monkeypatch)
    with pytest.raises(ValueError) as exc:
        ai_sandbox.attach(_inst(managed=0))
    assert "externally managed" in str(exc.value)


def test_attach_aborts_when_the_service_is_down(monkeypatch):
    _rows(monkeypatch)
    monkeypatch.setattr(ai_sandbox, "service_health",
                        lambda: {"configured": True, "reachable": False, "reason": "timeout"})
    with pytest.raises(ai_sandbox.SandboxError) as exc:
        ai_sandbox.attach(_inst())
    assert "not reachable" in str(exc.value)


def test_attach_aborts_without_a_configured_service(monkeypatch):
    _rows(monkeypatch)
    monkeypatch.setattr(settings, "ai_sandbox_admin_key", "")
    with pytest.raises(ValueError) as exc:
        ai_sandbox.attach(_inst())
    assert "no sandbox service configured" in str(exc.value)


def test_detach_restores_the_file_exactly(monkeypatch):
    _rows(monkeypatch)
    with_block = ai_sandbox._inject(STACK, "secret-key-123")
    assert with_block != STACK
    restored = ai_sandbox._strip(with_block)
    assert restored == STACK, "detach must leave the stack file byte-identical to the original"


def test_strip_refuses_a_malformed_block():
    broken = STACK.replace("      - N8N_RUNNERS_MODE=external",
                           "      " + ai_sandbox.MARK_START + "\n      - X=1")
    with pytest.raises(ValueError) as exc:
        ai_sandbox._strip(broken)
    assert "malformed" in str(exc.value)


def test_inject_is_idempotent_hostile(monkeypatch):
    """A file that already carries the block must not gain a second one."""
    _rows(monkeypatch)
    once = ai_sandbox._inject(STACK, "k1")
    with pytest.raises(ValueError) as exc:
        ai_sandbox._inject(once, "k2")
    assert "already carries" in str(exc.value)


def test_attach_works_when_the_stack_has_no_runners_flag(monkeypatch):
    """Legacy stacks without N8N_RUNNERS_ENABLED fall back to the environment: anchor."""
    _rows(monkeypatch)
    legacy = STACK.replace("      - N8N_RUNNERS_ENABLED=true\n", "")
    new = ai_sandbox._inject(legacy, "secret-key-123")
    assert "N8N_SANDBOX_SERVICE_API_KEY=secret-key-123" in new
    n8n_part = new.split("  runners:", 1)[0]
    assert "N8N_SANDBOX_SERVICE_URL" in n8n_part


def test_fleet_model_is_injected_only_when_configured(monkeypatch):
    _rows(monkeypatch)
    assert "N8N_INSTANCE_AI_MODEL" not in ai_sandbox._inject(STACK, "k")
    monkeypatch.setattr(settings, "ai_sandbox_model_api_key", "fleet-model-key")
    monkeypatch.setattr(settings, "ai_sandbox_model", "openai/gpt-5.5")
    monkeypatch.setattr(settings, "ai_sandbox_model_url", "https://riverapi.example.com/v1")
    block = ai_sandbox._inject(STACK, "k")
    assert "N8N_INSTANCE_AI_MODEL=openai/gpt-5.5" in block
    assert "N8N_INSTANCE_AI_MODEL_API_KEY=fleet-model-key" in block
    assert "N8N_INSTANCE_AI_MODEL_URL=https://riverapi.example.com/v1" in block
