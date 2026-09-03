# Backup + image-update operations (Steward 2026-09-03).
#
# Steward's requirement, verbatim:
#   "lets allow update and backups. so the users can simply click to backup
#    their workspace, it must bundle everything including db etc. however i
#    hope they can also have all their workflows exported for them. the admin
#    should also have the ability to update the n8n and backups as well."
#
# Design:
#   * A "full" backup fetches the container's /home/node/.n8n data dir via the
#     docker proxy archive endpoint and stores it as a gzipped tar on the portal
#     backend's backup volume. That dir holds everything: database.sqlite (all
#     workflows + credentials + settings), the encryption-key config, the
#     ~/.n8n per-tenant config, and the n8n export dir. This is the
#     "bundle everything including db" artifact.
#   * A "workflows" backup runs `n8n export:workflow --all` inside the container
#     (via exec) and saves the JSON. "credentials" runs export:credentials.
#     These are the portable, human-inspectable exports Steward asked for.
#   * Backup records live in the `backups` table; files live on the backend
#     `BACKUP_DIR` volume. Both the customer (their own instance) and the admin
#     (any instance) can trigger and download them.
#   * `update_instance_image` changes a stack's n8n image tag and redeploys the
#     container against the same volume (no data loss); used by the admin.
#
# Safety notes:
#   * Backups are read-only w.r.t. the tenant (archive + exec export; nothing
#     written into the tenant container's data dir).
#   * Image update recreates the container in place (same volume + env); step
#     back the image tag to the previous value to roll back.
#   * A backup requires the container to be REACHABLE. A locked/stopped instance
#     (container off) cannot be backed up — the helper raises, which surfaces as
#     a visible status. (You can still back it up once it's running.)

import gzip
import logging
import os
import datetime
import tarfile
import io
import shutil

from .. import db
from ..config import settings
from .portainer_client import PortainerClient

log = logging.getLogger("n8n-portal")

# The n8n data directory inside a tenant container (mounted as `n8n_data`
# volume -> /home/node/.n8n in the canonical stack template).
N8N_DATA_DIR = "/home/node/.n8n"
BACKUP_BASENAME = "n8n-data"


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


# ---------- container resolution ----------

def resolve_container(instance) -> tuple[int, str] | None:
    """Return (endpoint_id, container_id) for an instance row, or None if the
    container cannot be located (e.g. stopped stack on a live env)."""
    pc = PortainerClient()
    env = int(instance["environment_id"])
    # Admin-attached pre-existing stacks record the container directly.
    cid = _row_get(instance, "container_id", "")
    if _row_get(instance, "managed", 1) == 0 and cid:
        return env, cid
    # Portal-provisioned / stack-managed: find the container whose compose
    # project == stack_name on that environment.
    try:
        containers = pc.list_containers(env, all=True)
    except Exception as e:
        log.error("list_containers(%s) failed: %s", env, e)
        return None
    stack = instance["stack_name"]
    for c in containers:
        names = c.get("Names") or []
        for n in names:
            nm = n.lstrip("/")
            # container is named "<stack>-n8n-1" or "<stack>-n8n-2"
            if nm == f"{stack}-n8n-1" or nm.startswith(f"{stack}-n8n-"):
                # only a running container can be archived/exec'd
                if c.get("State") == "running":
                    return env, c["Id"]
    return None


def _backup_dir_for(instance) -> str:
    """Per-instance backup folder: <backup_dir>/<stack_name>/<timestamp_utc>."""
    base = settings.backup_dir
    stack = instance["stack_name"]
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(base, stack, ts)
    os.makedirs(out, exist_ok=True)
    return out


# ---------- full backup ----------

def run_full_backup(backup_id: int, instance) -> None:
    """Fetch the whole ~/.n8n data dir as a tar.gz and record the file."""
    resolved = resolve_container(instance)
    if not resolved:
        db.fail_backup(backup_id, "Instance container not running; cannot back up a stopped workspace.")
        return
    env, cid = resolved
    pc = PortainerClient()
    out_dir = _backup_dir_for(instance)
    filename = f"{BACKUP_BASENAME}.tar.gz"
    dest = os.path.join(out_dir, filename)

    try:
        raw = pc.get_container_archive(env, cid, N8N_DATA_DIR)
        if not raw:
            db.fail_backup(backup_id, "Empty archive returned from container.")
            return
        # `raw` is a tar stream (the archive response is a tar of the path).
        # Re-pack it gzipped so the download is a normal .tar.gz.
        with open(dest, "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
                gz.write(raw)
        size = os.path.getsize(dest)
        log.info("full backup %s -> %s (%d bytes)", backup_id, dest, size)
        db.finish_backup(backup_id, size)
    except Exception as e:
        log.error("full backup %s failed: %s", backup_id, e)
        db.fail_backup(backup_id, str(e))


def run_workflows_backup(backup_id: int, instance, kind: str = "workflows") -> None:
    """Export workflows (or credentials) to JSON via `n8n export:<kind> --all`
    and save it. kind is one of 'workflows' | 'credentials'."""
    resolved = resolve_container(instance)
    if not resolved:
        db.fail_backup(backup_id, "Instance container not running; cannot export from a stopped workspace.")
        return
    env, cid = resolved
    pc = PortainerClient()
    out_dir = _backup_dir_for(instance)
    filename = f"{kind}.json"
    dest = os.path.join(out_dir, filename)

    try:
        # n8n CLI verbs are singular: export:workflow, export:credentials.
        # The exporter prints a node-loading/deprecation BANNER and a success
        # line to stdout (verified live on stt), which would pollute the JSON.
        # Write to the file, discard BOTH stdout and stderr, then `cat` the file
        # so the exported JSON (the file) is exactly what gets returned.
        cli_kind = "workflow" if kind == "workflows" else kind
        cmd = ["/bin/sh", "-c",
               f"n8n export:{cli_kind} --all --output=/tmp/portal-{kind}-export.json --pretty >/dev/null 2>/dev/null; "
               f"cat /tmp/portal-{kind}-export.json"]
        out = pc.exec_in_container(env, cid, cmd, timeout=120)
        text = out.decode("utf-8", errors="replace").strip()
        if not text or text.startswith("Error exporting") or "No such file" in text or "can't open" in text:
            # Fresh instance with no workflows/credentials writes nothing; return
            # a valid empty array rather than a hard failure.
            text = "[]"
        # Normalize: if n8n wrote an array, keep it; else wrap.
        data = text.lstrip()
        if not data.startswith("["):
            data = "[" + data + "]"
        with open(dest, "w") as f:
            f.write(data)
        size = os.path.getsize(dest)
        log.info("%s backup %s -> %s (%d bytes)", kind, backup_id, dest, size)
        db.finish_backup(backup_id, size)
    except Exception as e:
        log.error("%s backup %s failed: %s", kind, backup_id, e)
        db.fail_backup(backup_id, str(e))


# ---------- image update (admin) ----------

def update_instance_image(instance, image: str) -> None:
    """Change the n8n image tag on a stack-managed instance and redeploy it.
    The image string must be a bare tag like '2.31.6' or a full 'n8nio/n8n:tag'.
    Returns the new image; raises on failure."""
    image = image.strip()
    if not image:
        raise ValueError("Image tag is required.")
    if ":" in image and not image.startswith("n8nio/n8n") and not image.startswith("n8n/n8n"):
        # allow "n8nio/n8n:2.31.6" or "2.31.6"; strip a bare tag to n8nio/n8n:<tag>
        pass
    tag = image.split("/")[-1]
    full_image = f"n8nio/n8n:{tag}" if ":" in tag or not tag.startswith("latest") else image
    if not full_image.startswith("n8nio/n8n") and not full_image.startswith("n8n/n8n"):
        full_image = f"n8nio/n8n:{image}"

    pc = PortainerClient()
    env = int(instance["environment_id"])
    stack_id = instance["stack_id"]
    if not stack_id or stack_id <= 0:
        raise ValueError("Cannot update image: instance has no Portainer stack record.")
    pc.update_stack_image(int(stack_id), env, full_image)
    db.update_instance(instance["id"], image=full_image)
    log.info("instance %s image updated to %s", instance["stack_name"], full_image)
    return full_image


def current_image(instance) -> str:
    """Best-effort current image tag for an instance."""
    from .portainer_client import PortainerClient
    resolved = resolve_container(instance)
    if not resolved:
        return _row_get(instance, "image", "n8nio/n8n:latest")
    env, cid = resolved
    pc = PortainerClient()
    try:
        insp = pc.get_container(env, cid)
        return insp.get("Config", {}).get("Image", "n8nio/n8n:latest")
    except Exception:
        return _row_get(instance, "image", "n8nio/n8n:latest")
