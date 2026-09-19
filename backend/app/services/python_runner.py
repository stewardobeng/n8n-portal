# Python task-runner sidecar: attach/detach the official n8nio/runners
# container to a tenant stack (admin one-click, 0.1.63).
#
# Why: n8n >= 2.0 removed BOTH the bundled external task runner AND the
# Pyodide Python runtime, so our single-container tenant stacks have NO working
# Python in the Code node (JS still works via the internal runner). The
# official fix is the n8nio/runners sidecar in "external" mode.
#
# Design decisions (Steward, 2026-09-17):
# - Admin-only, ONE instance at a time, opt-in per workspace (never fleet-wide:
#   the sidecar costs every tenant a slice of RAM whether or not Python is used).
# - The runners image tag MUST match the n8n image tag (n8n requirement);
#   PortainerClient.update_stack_image re-tags the runners line in the SAME
#   PUT whenever one is present, so image updates keep versions in lockstep.
# - No new ports: the runner reaches the task broker (n8n:5679) over the
#   compose-internal network; nothing is published to the host.
# - The runner's import policy ships deny-by-default. We deliver an override
#   config (compose `configs`, inline content) that opens the Python STANDARD
#   LIBRARY only ("*") and keeps third-party packages (pandas, numpy, ...) OFF:
#   those would require a custom-built runners image we would have to maintain.
# - Attached workspaces (managed=0) are refused: we do not own their compose.

import json
import re
import time

from .. import db
from .portainer_client import PortainerClient

RUNNERS_IMAGE = "n8nio/runners"
BROKER_PORT = 5679
RUNNER_MEM_LIMIT = "384m"

# The whole override config delivered into the runners container at
# /etc/n8n-task-runners.json. Mirrors the upstream default (verified
# 2026-09-17, docker/images/runners/n8n-task-runners.json on master) with
# EXACTLY ONE change: N8N_RUNNERS_STDLIB_ALLOW "" -> "*".
RUNNER_CONFIG_JSON = json.dumps({
    "task-runners": [
        {
            "runner-type": "javascript",
            "workdir": "/home/runner",
            "command": "/usr/local/bin/node",
            "args": ["--disallow-code-generation-from-strings",
                     "--disable-proto=delete",
                     "/opt/runners/task-runner-javascript/dist/start.js"],
            "health-check-server-port": "5681",
            "allowed-env": ["PATH", "GENERIC_TIMEZONE", "NODE_OPTIONS",
                            "NODE_PATH", "N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT",
                            "N8N_RUNNERS_TASK_TIMEOUT",
                            "N8N_RUNNERS_MAX_CONCURRENCY", "N8N_SENTRY_DSN",
                            "N8N_VERSION", "ENVIRONMENT", "DEPLOYMENT_NAME",
                            "HOME"],
            "env-overrides": {
                "NODE_FUNCTION_ALLOW_BUILTIN": "crypto",
                "NODE_FUNCTION_ALLOW_EXTERNAL": "moment",
                "N8N_RUNNERS_HEALTH_CHECK_SERVER_HOST": "0.0.0.0",
            },
        },
        {
            "runner-type": "python",
            "workdir": "/home/runner",
            "command": "/opt/runners/task-runner-python/.venv/bin/python",
            "args": ["-I", "-B", "-X", "disable_remote_debug", "-m", "src.main"],
            "health-check-server-port": "5682",
            "allowed-env": ["PATH", "N8N_RUNNERS_LAUNCHER_LOG_LEVEL",
                            "N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT",
                            "N8N_RUNNERS_TASK_TIMEOUT",
                            "N8N_RUNNERS_MAX_CONCURRENCY", "N8N_SENTRY_DSN",
                            "N8N_VERSION", "ENVIRONMENT", "DEPLOYMENT_NAME"],
            "env-overrides": {
                "N8N_RUNNERS_STDLIB_ALLOW": "*",
                "N8N_RUNNERS_EXTERNAL_ALLOW": "",
            },
        },
    ]
}, separators=(",", ":"))

MARK = "portal-runner"

def _env_block() -> str:
    return (
        f"      # >>> {MARK}:env-start\n"
        "      - N8N_RUNNERS_MODE=external\n"
        "      - N8N_RUNNERS_AUTH_TOKEN=${N8N_RUNNERS_AUTH_TOKEN}\n"
        "      - N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0\n"
        f"      # >>> {MARK}:env-end"
    )

def _service_block(tag: str) -> str:
    return (
        "\n"
        f"  # >>> {MARK}:service-start\n"
        "  # Python/JS task-runner sidecar (official n8n image, version-matched).\n"
        "  # Broker connection stays on the compose network; no ports published.\n"
        "  runners:\n"
        f"    image: {RUNNERS_IMAGE}:{tag}\n"
        "    restart: unless-stopped\n"
        f"    mem_limit: \"{RUNNER_MEM_LIMIT}\"\n"
        "    mem_reservation: \"128m\"\n"
        "    environment:\n"
        "      - N8N_RUNNERS_AUTH_TOKEN=${N8N_RUNNERS_AUTH_TOKEN}\n"
        f"      - N8N_RUNNERS_TASK_BROKER_URI=http://n8n:{BROKER_PORT}\n"
        "      - N8N_RUNNERS_AUTO_SHUTDOWN_TIMEOUT=15\n"
        "    configs:\n"
        f"      - {MARK}-config\n"
        "    depends_on:\n"
        "      - n8n\n"
        f"  # >>> {MARK}:service-end"
    )

def _configs_block() -> str:
    return (
        f"# >>> {MARK}:configs-start\n"
        "configs:\n"
        f"  {MARK}-config:\n"
        f"    content: '{RUNNER_CONFIG_JSON}'\n"
        f"# >>> {MARK}:configs-end"
    )


def runners_tag_for(n8n_image: str) -> str:
    """The runners tag that matches an n8n image ('n8nio/n8n:2.40.2' ->
    '2.40.2'; bare 'latest' passes through)."""
    tag = (n8n_image or "").strip()
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    tag = tag.strip()
    return tag or "latest"


def _n8n_tag_from_stack_file(content: str) -> str:
    m = re.search(r"image:\s*n8nio/n8n:([^\s]+)", content)
    return m.group(1).strip() if m else "latest"


def attach_runner(instance) -> dict:
    """Add the official runners sidecar to ONE tenant stack and flip the DB
    flag. Raises ValueError for caller mistakes, RuntimeError for Portainer
    failures. One PUT redeploys the stack (n8n container restarts against the
    same volume; the new runners container starts alongside)."""
    if db._row_get(instance, "python_runner", 0):
        raise ValueError("Python runner is already attached to this workspace.")
    if not db._row_get(instance, "managed", 1):
        raise ValueError(
            "This is an attached (externally managed) workspace; the portal "
            "does not own its compose file, so the Python runner cannot be "
            "attached here.")
    stack_id = instance["stack_id"] if "stack_id" in instance.keys() else None
    if not stack_id or int(stack_id) <= 0:
        raise ValueError(
            "This workspace has no Portainer stack record, so the Python "
            "runner cannot be attached.")

    pc = PortainerClient()
    env_id = int(instance["environment_id"])
    stack_id = int(stack_id)
    content = pc.get_stack_file(stack_id)
    if f"# >>> {MARK}:service-start" in content:
        raise ValueError(
            "The stack file already carries a portal-runner block; detach it "
            "first or fix the stack file manually.")
    if re.search(r"^configs:", content, flags=re.M):
        raise ValueError(
            "The stack file already defines a top-level configs section; "
            "refusing to inject a second one.")

    tag = _n8n_tag_from_stack_file(content)
    detail = pc.get_stack(stack_id)
    env = list(detail.get("Env") or [])
    if any(e.get("name") == "N8N_RUNNERS_AUTH_TOKEN" for e in env):
        raise ValueError(
            "The stack already carries a runner auth token; refusing to "
            "overwrite it.")
    env.append({"name": "N8N_RUNNERS_AUTH_TOKEN",
                "value": db.new_key(prefix="", nbytes=24)})

    # 1. runner env lines go right after the existing N8N_RUNNERS_ENABLED line
    anchor = "- N8N_RUNNERS_ENABLED=true"
    n_anchors = content.count(anchor)
    if n_anchors != 1:
        raise ValueError(
            f"Expected exactly one N8N_RUNNERS_ENABLED line in the stack file, "
            f"found {n_anchors}; attach aborted (stack file unchanged).")
    content = content.replace(anchor, anchor + "\n" + _env_block(), 1)

    # 2. runners service goes INSIDE the existing services: mapping — inserted
    #    immediately before the top-level volumes: line. Appending it at the end
    #    of the file would nest it under volumes: (same 2-space indent) and it
    #    would never deploy as a service.
    mv = re.search(r"^volumes:\s*$", content, flags=re.M)
    if not mv:
        raise ValueError(
            "Unexpected stack file shape: no top-level 'volumes:' section; "
            "attach aborted (stack file unchanged).")
    content = content[:mv.start()] + _service_block(tag) + "\n" + content[mv.start():]

    # 3. configs section appended as a NEW top-level key (after volumes:,
    #    which is fine: top-level key order is free in compose YAML)
    content = content.rstrip("\n") + "\n" + _configs_block() + "\n"

    pc.update_stack_file(stack_id, env_id, content, env)
    db.update_instance(instance["id"], python_runner=1)
    # Best-effort: confirm the sidecar container actually came up (first pull
    # of the runners image can take minutes; not seeing it yet is not failure).
    seen = _wait_runner_container(pc, env_id, instance["stack_name"], timeout=60)
    return {"tag": tag, "runner_container_seen": seen}


def detach_runner(instance) -> None:
    """Remove the sidecar (service block, configs block, n8n env lines, auth
    token env var) and flip the DB flag off. Rollback path for attach."""
    if not db._row_get(instance, "python_runner", 0):
        raise ValueError("No Python runner is attached to this workspace.")
    stack_id = instance["stack_id"] if "stack_id" in instance.keys() else None
    if not stack_id or int(stack_id) <= 0:
        raise ValueError("This workspace has no Portainer stack record.")
    pc = PortainerClient()
    env_id = int(instance["environment_id"])
    stack_id = int(stack_id)
    content = pc.get_stack_file(stack_id)
    detail = pc.get_stack(stack_id)
    env = [e for e in (detail.get("Env") or [])
           if e.get("name") != "N8N_RUNNERS_AUTH_TOKEN"]

    def _cut(text: str, start_m: str, end_m: str) -> str:
        # Cut whole lines: extend to the start-of-line before the marker and
        # swallow the newline after the end marker, so the file returns
        # byte-identical to its pre-attach shape.
        s = text.find(start_m)
        if s < 0:
            return text
        s = text.rfind("\n", 0, s) + 1
        e = text.find(end_m, s)
        if e < 0:
            return text
        e += len(end_m)
        if e < len(text) and text[e] == "\n":
            e += 1
        return text[:s] + text[e:]

    content = _cut(content, f"# >>> {MARK}:env-start", f"# >>> {MARK}:env-end")
    content = _cut(content, f"  # >>> {MARK}:service-start", f"  # >>> {MARK}:service-end")
    content = _cut(content, f"# >>> {MARK}:configs-start", f"# >>> {MARK}:configs-end")
    content = re.sub(r"\n{3,}", "\n\n", content)

    pc.update_stack_file(stack_id, env_id, content, env)
    db.update_instance(instance["id"], python_runner=0)


def _wait_runner_container(pc: PortainerClient, env_id: int,
                           stack_name: str, timeout: int = 60) -> bool:
    """True when a container named {stack}-runners-1 reports running."""
    want = f"{stack_name}-runners-1"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for c in pc.list_containers(env_id, all=True):
                names = " ".join(c.get("Names") or []) + (c.get("Name") or "")
                if want in names and c.get("State") == "running":
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


def sync_runner_image(content: str) -> str:
    """Re-tag the runners image line to whatever the n8n line becomes (used by
    PortainerClient.update_stack_image; n8n requires matched versions)."""
    tag = _n8n_tag_from_stack_file(content)
    content, _n = re.subn(r"(image:\s*n8nio/runners:)[^\s]+",
                          r"\g<1>" + tag, content)
    return content
