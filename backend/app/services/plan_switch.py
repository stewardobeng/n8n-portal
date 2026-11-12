"""Switch a workspace between the two package specs: GHS 300 standard <-> GHS 500 Plus.

A package is not a label, it is a resource pack, and the two compose templates differ in
exactly three lines:

    plan A (standard, GHS 300)   cpus 0.30   mem_limit 768m   node heap 384
    plan B (Plus, GHS 500)       cpus 0.50   mem_limit 1g     node heap 768

Everything else in those templates is identical, which is what makes a switch safe: this module
rewrites ONLY those three resource lines in the workspace's LIVE stack file and redeploys it.
Portainer then recreates the container against the same named volume (`n8n_data` mounted at
/home/node/.n8n), so workflows, execution history and stored credentials survive. The image tag,
the python-runner sidecar, the port, the environment and the proxy host are never touched.

Why rewrite the live file instead of re-rendering the template: a deployed stack may carry state
the template knows nothing about (a runner sidecar, an image tag updated months later, per-stack
env). Re-rendering would silently drop it; editing three lines cannot.

Fail closed: if any expected line is missing, or appears more than once, the switch aborts with
the stack file untouched rather than writing a half-edited compose.
"""
from __future__ import annotations

import re
from typing import Any

from .. import db
from ..config import settings
from .portainer_client import PortainerClient

PLAN_NAMES = {"a": "standard (GHS 300)", "b": "Plus (GHS 500)"}
PLAN_ORDER = ("a", "b")

# Each entry: (label, regex with a single capture group holding the value, template key parser)
_RESOURCE_PATTERNS = {
    "cpus": re.compile(r'(?m)^(\s*cpus:\s*)"([^"]*)"'),
    "mem_limit": re.compile(r'(?m)^(\s*mem_limit:\s*)"([^"]*)"'),
    "heap_mb": re.compile(r"(?m)^(\s*-\s*NODE_OPTIONS=--max-old-space-size=)(\d+)"),
}
_RESOURCE_LABELS = {
    "cpus": "CPU limit",
    "mem_limit": "memory limit",
    "heap_mb": "node heap",
}


def normalize_plan(plan: str | None) -> str:
    value = (plan or "").strip().lower()
    if value not in PLAN_ORDER:
        raise ValueError("Plan must be 'a' (standard) or 'b' (Plus).")
    return value


def plan_label(plan: str | None) -> str:
    return PLAN_NAMES.get((plan or "a").strip().lower(), PLAN_NAMES["a"])


def other_plan(plan: str | None) -> str:
    return "a" if (plan or "a").strip().lower() == "b" else "b"


def _template_path(plan: str) -> str:
    return settings.plan_b_compose_template_path if plan == "b" else settings.compose_template_path


def template_resources(plan: str) -> dict[str, str]:
    """The three resource values a package defines, read from its real template.

    Reading the template (rather than hardcoding numbers here) means the switch cannot drift
    from what a NEW workspace of that package actually gets: edit the template and both the
    provisioner and this switch follow.
    """
    plan = normalize_plan(plan)
    try:
        with open(_template_path(plan), "r") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError(f"Cannot read the {plan_label(plan)} template: {exc}") from exc
    values: dict[str, str] = {}
    for key, pattern in _RESOURCE_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            raise ValueError(
                f"The {plan_label(plan)} template has no {_RESOURCE_LABELS[key]} line; "
                "refusing to guess a value."
            )
        values[key] = match.group(2)
    return values


def _service_block(content: str, service: str) -> tuple[int, int]:
    """Character span of one service's block inside a compose file.

    A deployed workspace may carry more than the n8n service: the portal's Python support adds a
    `runners` sidecar, which has resource lines of its own (`mem_limit: 384m`). Scanning the whole
    file therefore finds several `mem_limit` lines and a naive rewrite either refuses (0.1.99 did,
    with "expected exactly 1") or, worse, would retune the sidecar. Scope the search to the named
    service so only the n8n container's resources move.

    Raises ValueError when the service cannot be identified unambiguously.
    """
    lines = content.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)

    header = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.lstrip().startswith("#"):
            continue
        if re.match(r"^\s*%s:\s*$" % re.escape(service), stripped):
            if header is not None:
                raise ValueError(
                    "Stack file defines the '%s' service more than once; refusing to guess "
                    "which one to change." % service
                )
            header = idx
    if header is None:
        raise ValueError(
            "Stack file has no '%s' service block, so its resources cannot be changed safely. "
            "Switch aborted so the file is left untouched." % service
        )

    indent = len(lines[header]) - len(lines[header].lstrip())
    end = len(content)
    for idx in range(header + 1, len(lines)):
        stripped = lines[idx].rstrip("\r\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        if (len(stripped) - len(stripped.lstrip())) <= indent:
            end = starts[idx]
            break
    # Trailing blank lines and comments (such as the next service's marker comment) are not part of
    # this service; the span ends at the last real line of its own body.
    last_real = header
    for idx in range(header + 1, len(lines)):
        if starts[idx] >= end:
            break
        text = lines[idx].strip()
        if text and not text.startswith("#"):
            last_real = idx
    return starts[header], starts[last_real] + len(lines[last_real])


def rewrite_resources(content: str, target_plan: str) -> tuple[str, list[dict[str, str]]]:
    """Rewrite the three resource lines of the N8N SERVICE. Returns (new content, changes).

    Only the `n8n` service block is touched: a `runners` sidecar keeps its own limits, its image
    and its config. Raises ValueError without touching anything when a line is missing or
    ambiguous inside that block, because half a resource change is worse than none: the workspace
    would then claim a package its container is not actually running.
    """
    target = template_resources(target_plan)
    changes: list[dict[str, str]] = []
    new_content = content
    for key, pattern in _RESOURCE_PATTERNS.items():
        start, end = _service_block(new_content, "n8n")
        matches = list(pattern.finditer(new_content[start:end]))
        if len(matches) != 1:
            raise ValueError(
                f"The n8n service has {len(matches)} {_RESOURCE_LABELS[key]} line(s), expected "
                "exactly 1; switch aborted so the stack file is left untouched."
            )
        match = matches[0]
        current = match.group(2)
        wanted = target[key]
        if current == wanted:
            changes.append({"resource": key, "label": _RESOURCE_LABELS[key],
                            "from": current, "to": wanted, "change": "unchanged"})
            continue
        # offsets inside the slice are relative to it, so shift them back to the whole document
        s = start + match.start(2)
        e = start + match.end(2)
        new_content = new_content[:s] + wanted + new_content[e:]
        changes.append({"resource": key, "label": _RESOURCE_LABELS[key],
                        "from": current, "to": wanted, "change": "changed"})
    return new_content, changes


def _stack_ids(instance, pc: PortainerClient) -> tuple[int, int]:
    """(stack_id, endpoint_id), resolving a missing stack_id by name like the image path does."""
    env = int(instance["environment_id"] if "environment_id" in instance.keys() else instance["environment_id"])
    stack_id = instance["stack_id"] if "stack_id" in instance.keys() else None
    if not stack_id or int(stack_id) <= 0:
        for s in pc.list_stacks():
            if int(s.get("EndpointId", 0)) == env and s.get("Name") == instance["stack_name"]:
                stack_id = s.get("Id")
                break
        if stack_id:
            db.update_instance(instance["id"], stack_id=stack_id)
        else:
            raise ValueError(
                "This workspace has no Portainer stack record, so its resources cannot be changed. "
                "Attach it again first."
            )
    return int(stack_id), env


def preview(instance, target_plan: str) -> dict[str, Any]:
    """What a switch would change, without changing anything (drives the confirm dialog)."""
    target_plan = normalize_plan(target_plan)
    pc = PortainerClient()
    stack_id, env = _stack_ids(instance, pc)
    content = pc.get_stack_file(stack_id)
    _, changes = rewrite_resources(content, target_plan)
    current_plan = (instance["plan"] if "plan" in instance.keys() else None) or "a"
    return {
        "instance_id": instance["id"],
        "stack_name": instance["stack_name"],
        "domain": instance["domain"] if "domain" in instance.keys() else None,
        "from_plan": current_plan,
        "to_plan": target_plan,
        "from_label": plan_label(current_plan),
        "to_label": plan_label(target_plan),
        "action": "downgrade" if target_plan == "a" and current_plan == "b" else "upgrade",
        "changes": changes,
        "already_matching": all(c["change"] == "unchanged" for c in changes),
    }


def switch(instance, target_plan: str) -> dict[str, Any]:
    """Apply a package switch to one workspace and record it.

    The container is RECREATED, so the workspace blinks offline for a few seconds and anything
    mid-execution is lost. Callers must say that plainly before running this; an admin takes a
    backup first (the endpoint does it by default).
    """
    target_plan = normalize_plan(target_plan)
    pc = PortainerClient()
    stack_id, env = _stack_ids(instance, pc)
    detail = pc.get_stack(stack_id) or {}
    env_vars = list(detail.get("Env") or [])
    content = pc.get_stack_file(stack_id)
    new_content, changes = rewrite_resources(content, target_plan)
    if all(c["change"] == "unchanged" for c in changes):
        # The file already carries the target spec; still record the plan so the portal and the
        # container agree, but do not bounce a healthy workspace for nothing.
        db.update_instance(instance["id"], plan=target_plan)
        return {"instance_id": instance["id"], "changed": False, "plan": target_plan,
                "changes": changes,
                "note": "The stack already runs this package's resources; only the record was updated."}
    pc.update_stack_file(stack_id, env, new_content, env_vars)
    # Verify from the source of truth: read the file back and confirm every value landed.
    verify_content = pc.get_stack_file(stack_id)
    _, after = rewrite_resources(verify_content, target_plan)
    if not all(c["change"] == "unchanged" for c in after):
        still = [c for c in after if c["change"] != "unchanged"]
        raise ValueError(
            "The redeploy did not take: " + ", ".join(
                f"{c['label']} still {c['from']} (wanted {c['to']})" for c in still
            )
        )
    db.update_instance(instance["id"], plan=target_plan)
    return {"instance_id": instance["id"], "changed": True, "plan": target_plan,
            "changes": changes,
            "note": "Stack redeployed against the same volume, so data was preserved."}
