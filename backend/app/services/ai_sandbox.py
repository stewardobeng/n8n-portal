"""Attach or detach the shared n8n AI sandbox (n8n-sandbox provider) for ONE workspace.

Mirrors `python_runner`: an admin turns the feature on for a workspace, this module writes a marked
block of environment variables into that workspace's live stack file and Portainer redeploys it against
the same volume, so workflows, executions and credentials survive.

What n8n needs for a self-hosted (non-Daytona) sandbox:
    N8N_INSTANCE_AI_SANDBOX_ENABLED=true
    N8N_INSTANCE_AI_SANDBOX_PROVIDER=n8n-sandbox
    N8N_SANDBOX_SERVICE_URL=<sandbox-api base url>
    N8N_SANDBOX_SERVICE_API_KEY=<a tenant key from that service>
plus, for the bundled web search:
    N8N_INSTANCE_AI_SEARXNG_URL=<searxng base url>

One shared sandbox service serves the whole fleet; each workspace gets its OWN tenant key, so revoking
one workspace cannot affect another. Nothing here touches the model key: Steward's decision (2026-11-12)
is that each user supplies their own model in their instance. `settings.ai_sandbox_model_api_key` exists
so a fleet-wide model can be injected later without changing the shape of this module.

The marker comment makes detachment exact: it cuts the block it inserted and nothing else.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .. import db
from ..config import settings
from .portainer_client import PortainerClient

MARK_START = "# >>> ai-sandbox:env-start"
MARK_END = "# >>> ai-sandbox:env-end"
INDENT = "      "  # matches the template's sequence items inside the n8n service environment


class SandboxError(RuntimeError):
    """The shared sandbox service refused or was unreachable."""


def _api(method: str, path: str, body=None, timeout: float = 60.0):
    url = settings.ai_sandbox_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Key", settings.ai_sandbox_admin_key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode()) if raw else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise SandboxError(f"sandbox service returned {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise SandboxError(f"sandbox service unreachable: {type(exc).__name__}") from exc


def service_health() -> dict:
    """Read-only health of the shared sandbox service (used by the admin UI before an attach)."""
    if not settings.ai_sandbox_url or not settings.ai_sandbox_admin_key:
        return {"configured": False, "reachable": False, "reason": "no sandbox service configured"}
    try:
        status, body = _api("GET", "/healthz", timeout=15.0)
        return {"configured": True, "reachable": status == 200,
                "url": settings.ai_sandbox_url, "response": body}
    except SandboxError as exc:
        return {"configured": True, "reachable": False, "url": settings.ai_sandbox_url, "reason": str(exc)}


def _mint_tenant(instance) -> tuple[str, str]:
    """Create a tenant for this workspace and return (tenant_id, api_key).

    The key is a secret: it goes into the stack file and is never returned to the caller or logged.
    """
    name = str(instance["stack_name"])[:60]
    external_ref = "instance-%d" % int(instance["id"])
    status, body = _api("POST", "/admin/tenants",
                        {"name": name, "external_ref": external_ref})
    if status not in (200, 201):
        raise SandboxError(f"could not create a sandbox tenant (HTTP {status})")
    tenant = (body or {}).get("tenant") or {}
    key = (body or {}).get("key") or {}
    if not tenant.get("id") or not key.get("api_key"):
        raise SandboxError("sandbox service did not return a tenant key")
    return str(tenant["id"]), str(key["api_key"])


def _drop_tenant(tenant_id: str) -> None:
    """Best effort: a tenant with sandboxes still attached refuses deletion, which is fine."""
    if not tenant_id:
        return
    try:
        _api("DELETE", "/admin/tenants/%s" % tenant_id, timeout=30.0)
    except SandboxError:
        pass


def _env_lines(tenant_key: str) -> str:
    lines = [
        MARK_START,
        f"{INDENT}- N8N_INSTANCE_AI_SANDBOX_ENABLED=true",
        f"{INDENT}- N8N_INSTANCE_AI_SANDBOX_PROVIDER=n8n-sandbox",
        f"{INDENT}- N8N_SANDBOX_SERVICE_URL={settings.ai_sandbox_url.rstrip('/')}",
        f"{INDENT}- N8N_SANDBOX_SERVICE_API_KEY={tenant_key}",
    ]
    if settings.ai_sandbox_search_url:
        lines.append(f"{INDENT}- N8N_INSTANCE_AI_SEARXNG_URL={settings.ai_sandbox_search_url.rstrip('/')}")
    # Room for a fleet-wide model later: only injected when the portal is actually configured with one.
    if settings.ai_sandbox_model_api_key:
        if settings.ai_sandbox_model:
            lines.append(f"{INDENT}- N8N_INSTANCE_AI_MODEL={settings.ai_sandbox_model}")
        if settings.ai_sandbox_model_url:
            lines.append(f"{INDENT}- N8N_INSTANCE_AI_MODEL_URL={settings.ai_sandbox_model_url.rstrip('/')}")
        lines.append(f"{INDENT}- N8N_INSTANCE_AI_MODEL_API_KEY={settings.ai_sandbox_model_api_key}")
    lines.append(MARK_END)
    return "\n".join(lines)


def _inject(content: str, tenant_key: str) -> str:
    """Put the marked block inside the n8n service's environment list."""
    if MARK_START in content:
        raise ValueError("this workspace already carries an ai-sandbox block")
    block = _env_lines(tenant_key)

    anchor = f"{INDENT}- N8N_RUNNERS_ENABLED=true"
    if content.count(anchor) == 1:
        return content.replace(anchor, anchor + "\n" + block, 1)

    env_head = re.search(r"^    environment:\s*$", content, flags=re.M)
    if env_head:
        at = env_head.end()
        return content[:at] + "\n" + block + content[at:]

    raise ValueError(
        "could not find the n8n service environment block in this stack file, so the AI sandbox "
        "was not attached (stack file unchanged)")


def _strip(content: str) -> str:
    """Remove exactly the block this module inserted."""
    start = content.find(MARK_START)
    if start < 0:
        raise ValueError("this workspace has no ai-sandbox block to remove")
    end = content.find(MARK_END, start)
    if end < 0:
        raise ValueError("the ai-sandbox block is malformed (no end marker); fix the stack file by hand")
    end += len(MARK_END)
    # Eat ONLY the newline the injector added in front of the block. The newline AFTER the end marker
    # belongs to the original file, so consuming it would shorten the file by one line and detach
    # would not be byte-exact (pinned by test_detach_restores_the_file_exactly).
    if content[start - 1:start] == "\n":
        start -= 1
    return content[:start] + content[end:]


def _guard(instance, want_attached: bool) -> tuple[int, int, str, list]:
    attached = bool(db._row_get(instance, "ai_sandbox", 0))
    if want_attached and attached:
        raise ValueError("the AI sandbox is already attached to this workspace")
    if not want_attached and not attached:
        raise ValueError("the AI sandbox is not attached to this workspace")
    if not db._row_get(instance, "managed", 1):
        raise ValueError(
            "this is an attached (externally managed) workspace; the portal does not own its compose "
            "file, so the AI sandbox cannot be changed here")
    stack_id = instance["stack_id"] if "stack_id" in instance.keys() else None
    if not stack_id or int(stack_id) <= 0:
        raise ValueError("this workspace has no Portainer stack record, so the AI sandbox cannot be changed")
    pc = PortainerClient()
    env_id = int(instance["environment_id"])
    stack_id = int(stack_id)
    detail = pc.get_stack(stack_id) or {}
    env_vars = list(detail.get("Env") or [])
    return stack_id, env_id, pc.get_stack_file(stack_id), env_vars


def attach(instance) -> dict:
    """Attach the shared sandbox to one workspace. Returns a summary WITHOUT the key."""
    if not settings.ai_sandbox_url or not settings.ai_sandbox_admin_key:
        raise ValueError("the portal has no sandbox service configured (AI_SANDBOX_URL / _ADMIN_KEY)")
    stack_id, env_id, content, env_vars = _guard(instance, want_attached=True)

    health = service_health()
    if not health.get("reachable"):
        raise SandboxError("the shared sandbox service is not reachable: %s" % health.get("reason", "no response"))

    tenant_id, tenant_key = _mint_tenant(instance)
    try:
        new_content = _inject(content, tenant_key)
    except ValueError:
        _drop_tenant(tenant_id)
        raise

    pc = PortainerClient()
    pc.update_stack_file(stack_id, env_id, new_content, env_vars)
    db.update_instance(instance["id"], ai_sandbox=1, ai_sandbox_tenant_id=tenant_id)
    return {
        "instance_id": instance["id"],
        "attached": True,
        "sandbox_url": settings.ai_sandbox_url,
        "search_url": settings.ai_sandbox_search_url,
        "tenant_id": tenant_id,
        "note": ("Stack redeployed against the same volume, so workflows and credentials were preserved. "
                 "The user still needs to choose a model in their instance's Assistant settings."),
    }


def detach(instance) -> dict:
    """Remove the sandbox from one workspace and revoke its tenant key."""
    stack_id, env_id, content, env_vars = _guard(instance, want_attached=False)
    tenant_id = db._row_get(instance, "ai_sandbox_tenant_id", "") or ""
    new_content = _strip(content)
    pc = PortainerClient()
    pc.update_stack_file(stack_id, env_id, new_content, env_vars)
    _drop_tenant(tenant_id)
    db.update_instance(instance["id"], ai_sandbox=0, ai_sandbox_tenant_id="")
    return {"instance_id": instance["id"], "attached": False,
            "note": "Sandbox removed and its tenant key revoked; the workspace was redeployed in place."}


def status(instance) -> dict:
    """What the admin UI needs to render the toggle, without any secret."""
    return {
        "instance_id": instance["id"],
        "attached": bool(db._row_get(instance, "ai_sandbox", 0)),
        "tenant_id": db._row_get(instance, "ai_sandbox_tenant_id", "") or "",
        "sandbox_url": settings.ai_sandbox_url,
        "search_url": settings.ai_sandbox_search_url,
        "service": service_health(),
    }
