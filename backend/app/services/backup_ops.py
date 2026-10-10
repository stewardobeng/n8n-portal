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
import re
import time
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

# ---------- true running version (0.1.80) ----------
#
# The image REFERENCE says nothing reliable: every workspace stack is written
# `image: n8nio/n8n:latest`, and "latest" is not a version. Comparing that string
# with the newest release made the update dialog claim "already on the latest
# release" for workspaces actually running 2.31.6 (Steward, 2026-10-06). So ask
# the image/binary what it really is.

_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)")
_IMAGE_CACHE: dict[int, tuple[float, dict]] = {}
_IMAGE_TTL = 60.0


def _image_versions(pc: PortainerClient, env: int) -> dict:
    """image id -> version, from the image's own OCI label, cached ~60s.

    One call per environment per minute, whatever the number of workspaces.
    """
    hit = _IMAGE_CACHE.get(env)
    now = time.time()
    if hit and now - hit[0] < _IMAGE_TTL:
        return hit[1]
    out: dict = {}
    try:
        for im in pc.list_images(env) or []:
            labels = im.get("Labels") or {}
            ver = str(labels.get("org.opencontainers.image.version") or "").strip()
            iid = im.get("Id") or im.get("ID") or ""
            if ver and iid and _VERSION_RE.match(ver):
                out[iid] = ver
    except Exception as e:
        log.warning("image versions env %s failed: %s", env, e)
    _IMAGE_CACHE[env] = (now, out)
    return out


def _tag_version(ref: str) -> str:
    """Explicit version from an image reference; '' for latest/<none>/sha256."""
    if not ref:
        return ""
    tag = ref.split(":")[-1] if ":" in ref else ""
    m = _VERSION_RE.match(tag)
    return m.group(1) if m else ""


def restart_workspace(instance) -> str:
    """Start this workspace's container again: same image, same volume, no update.

    Restart is deliberately not an update path. It resolves the container (never
    trusting a stored id, see resolve_container), asks Docker to restart that one
    container, and returns its id. The stack is not touched, so nothing is pulled,
    replaced or recreated, and the workspace keeps every byte of its data.

    Raises RuntimeError with a plain-language message when there is nothing to
    restart, because that is what the person who clicked needs to read.
    """
    pc = PortainerClient()
    resolved = resolve_container(instance)
    if not resolved:
        raise RuntimeError("its container is not running at the moment")
    env, cid = resolved
    pc.restart_container(env, cid)
    log.info("restarted workspace %s (container %s)", instance["stack_name"], cid[:12])
    return cid


def current_version(instance, allow_exec: bool = False) -> dict:
    """What this workspace is actually running: {version, source}.

    source is one of image-label | exec | tag | unknown. A floating tag never
    yields a version, and callers must treat '' as unknown rather than current.
    """
    resolved = resolve_container(instance)
    stored = _row_get(instance, "image", "") or ""
    tag_ver = _tag_version(stored)
    if not resolved:
        return {"version": tag_ver, "source": "tag" if tag_ver else "unknown"}
    env, cid = resolved
    pc = PortainerClient()
    try:
        insp = pc.get_container(env, cid)
    except Exception as e:
        log.warning("version inspect %s/%s failed: %s", env, cid, e)
        return {"version": tag_ver, "source": "tag" if tag_ver else "unknown"}
    image_id = insp.get("Image") or ""
    ref = (insp.get("Config") or {}).get("Image") or stored
    found = _image_versions(pc, env).get(image_id)
    if found:
        return {"version": found, "source": "image-label"}
    if allow_exec:
        try:
            out = pc.exec_in_container(env, cid, ["n8n", "--version"], timeout=30.0)
            text = (out or b"").decode("utf-8", "replace")
            m = _VERSION_RE.search(text.strip())
            if m:
                return {"version": m.group(1), "source": "exec"}
        except Exception as e:
            log.warning("version exec %s/%s failed: %s", env, cid, e)
    return {"version": _tag_version(ref), "source": "tag" if _tag_version(ref) else "unknown"}


def true_versions(instances, pc: PortainerClient | None = None) -> dict:
    """{instance_id: {version, source}} for a batch, without per-instance calls.

    One container list and one image list per environment, so the admin UI can
    show real versions for every workspace cheaply.
    """
    pc = pc or PortainerClient()
    by_env: dict = {}
    for inst in instances or []:
        try:
            by_env.setdefault(int(inst["environment_id"]), []).append(inst)
        except Exception:
            continue
    out: dict = {}
    for env, rows in by_env.items():
        try:
            containers = pc.list_containers(env, all=True)
        except Exception as e:
            log.warning("version scan env %s failed: %s", env, e)
            containers = []
        versions = _image_versions(pc, env)
        for inst in rows:
            name = f"{inst['stack_name']}-n8n-1"
            cont = None
            for c in containers:
                if any((n or "").lstrip("/") == name for n in (c.get("Names") or [])):
                    cont = c
                    break
            if cont is None:
                cid = _row_get(inst, "container_id", "")
                cont = next((c for c in containers if cid and c.get("Id") == cid), None)
            stored = _row_get(inst, "image", "") or ""
            if cont is None:
                tv = _tag_version(stored)
                out[inst["id"]] = {"version": tv, "source": "tag" if tv else "unknown"}
                continue
            ver = versions.get(cont.get("ImageID") or "") or versions.get(cont.get("Image") or "")
            if ver:
                out[inst["id"]] = {"version": ver, "source": "image-label"}
            else:
                tv = _tag_version(cont.get("Image") or stored)
                out[inst["id"]] = {"version": tv, "source": "tag" if tv else "unknown"}
    return out


def resolve_container(instance, require_running: bool = True) -> tuple[int, str] | None:
    """Return (endpoint_id, container_id) for an instance row, or None if the
    container cannot be located (e.g. stopped stack on a live env).

    A stored ``container_id`` is only a HINT, never the truth. Every recreate (an
    image update, an environment change) replaces the container with a NEW id, and
    trusting the old one had exactly one observable effect: Portainer answered
    ``404 No such container``, the version read fell back to the portal's own
    ``instances.image`` row, and that row always named the version we were updating
    FROM or TO rather than what was live. So verification could never succeed and
    the queue rolled a good update back, while a manual update (which never verifies)
    looked fine. This hit every ``managed=0`` workspace on endpoint 4 and was found
    on 2026-10-08 after seven recorded failures.
    """
    pc = PortainerClient()
    env = int(instance["environment_id"])
    cid = _row_get(instance, "container_id", "")
    if cid:
        try:
            insp = pc.get_container(env, cid)
            running = bool((insp.get("State") or {}).get("Running"))
            if running or not require_running:
                return env, cid
        except Exception:  # noqa: BLE001 - stale id or unreadable: fall through
            pass
    # Find the container whose compose project == stack_name on that environment.
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
                # only a running container can be archived/exec'd by default
                if c.get("State") == "running" or not require_running:
                    found = c["Id"]
                    if found != cid:
                        # self-heal, so the next call does not need the list scan
                        try:
                            db.update_instance(instance["id"], container_id=found)
                        except Exception as e:  # noqa: BLE001
                            log.warning("could not refresh container id for %s: %s", stack, e)
                    return env, found
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
    # Normalise to n8nio/n8n:<tag>. The old expression took the last PATH segment of
    # "n8nio/n8n:2.43.2" (that is "n8n:2.43.2") and prefixed it again, so every
    # workspace the QUEUE updated was recorded as "n8nio/n8n:n8n:2.43.2". The manual
    # path passes a bare tag, which is why its rows looked clean, and the doubled
    # value made the stored record disagree with what the stack really runs.
    tag = image.split("/")[-1]
    if ":" in tag:
        tag = tag.split(":")[-1]
    full_image = f"n8nio/n8n:{tag or 'latest'}"

    pc = PortainerClient()
    env = int(instance["environment_id"])
    stack_id = instance["stack_id"] if "stack_id" in instance.keys() else None
    if not stack_id or int(stack_id) <= 0:
        # Admin-attached workspaces (managed=0) may predate the portal and carry
        # no stack_id; resolve the Portainer stack by name so image updates work
        # on them exactly like portal-provisioned ones (Steward 2026-09-03).
        try:
            for s in pc.list_stacks():
                if (int(s.get("EndpointId", 0)) == env
                        and s.get("Name") == instance["stack_name"]):
                    stack_id = s.get("Id")
                    break
        except Exception as e:
            log.warning("image update: stack lookup for %s failed: %s",
                        instance["stack_name"], e)
        if stack_id:
            db.update_instance(instance["id"], stack_id=stack_id)
        else:
            raise ValueError(
                "Cannot update image: this workspace has no Portainer stack "
                "record. Attach it again or update it manually.")
    # Pull only when the tag is genuinely missing on that server. A blind
    # pullImage=true re-downloads roughly 1.8 GB per workspace even when the image is
    # already cached, which is what made the env 4 batch take longer than the whole
    # verification window and fail every workspace in it (2026-10-08).
    try:
        needs_pull = not pc.image_present(env, full_image)
    except Exception as e:  # noqa: BLE001
        log.warning("image presence check failed for %s: %s (pulling to be safe)", full_image, e)
        needs_pull = True
    pc.update_stack_image(int(stack_id), env, full_image, pull=needs_pull)
    db.update_instance(instance["id"], image=full_image)
    log.info("instance %s image updated to %s (pull=%s)",
             instance["stack_name"], full_image, needs_pull)
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
