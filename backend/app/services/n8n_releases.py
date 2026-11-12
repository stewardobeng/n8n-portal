# Latest official n8n release lookup (admin "update n8n" watch, 2026-09-03).
# Docker Hub is the source of truth: it is exactly what the environment servers
# pull from when a workspace image is updated.
#
# 0.1.100 (Steward, 2026-11-12): "newest" and "safest" stopped being the same answer. n8n publishes
# several lines at once, and on 2026-11-12 the numeric maximum was 2.48.2 while Docker Hub's own
# `stable`/`latest` tag pointed at 2.47.5, i.e. the highest number was the BETA line. Taking the
# maximum therefore meant every NEW workspace silently rode the beta channel. The channel is now
# explicit (N8N_RELEASE_CHANNEL, default `stable`) and resolved from the image's own version label,
# so "which line do new workspaces get" is a decision instead of an accident of arithmetic.
#
#   stable  (default) - the version behind Docker Hub's `stable` tag
#   beta              - the version behind Docker Hub's `beta` tag
#   highest           - the old behaviour: highest semver among the most recently pushed tags
#
# The pin is always a concrete X.Y.Z version, never a floating tag: a workspace on `latest` follows
# whatever that tag resolves to on every container recreate, which is an unattended upgrade plus
# one-way database migrations with nobody deciding it.

import re
import time

import httpx

_TAG_RE = re.compile(r"^\d+\.\d+\.\d+$")
_CACHE: dict = {"at": 0.0, "data": None}
_TTL_SECONDS = 15 * 60
_HUB_URL = "https://hub.docker.com/v2/repositories/n8nio/n8n/tags"
_REGISTRY = "https://registry-1.docker.io/v2/n8nio/n8n"
_AUTH_URL = (
    "https://auth.docker.io/token?service=registry.docker.io&scope=repository:n8nio/n8n:pull"
)
_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _semver(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.split("."))


def _fetch_tags() -> list[dict]:
    """Pull the most recently pushed tags and return clean release tags,
    newest semver first."""
    resp = httpx.get(
        _HUB_URL,
        params={"page_size": 100, "ordering": "last_updated"},
        timeout=20.0,
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("results", []) or []
    tags = []
    for it in items:
        name = str(it.get("name", ""))
        if _TAG_RE.match(name):
            tags.append({"tag": name, "published_at": it.get("last_updated") or ""})
    tags.sort(key=lambda t: _semver(t["tag"]), reverse=True)
    return tags


def channel_version(channel: str) -> dict | None:
    """The concrete X.Y.Z version behind a moving Docker Hub tag ('stable' / 'beta').

    Read from the image's own `org.opencontainers.image.version` label rather than guessed from tag
    names, because the label is what the image contains while a tag is only where it is filed. This
    is the same reading that showed 2.48.2 was `beta` and 2.47.5 was `stable` on 2026-11-12.
    Returns None when the tag cannot be resolved, so the caller can fall back instead of failing.
    """
    auth = httpx.get(_AUTH_URL, timeout=20.0, follow_redirects=True)
    auth.raise_for_status()
    token = str((auth.json() or {}).get("token") or "")
    if not token:
        return None
    headers = {"Authorization": "Bearer " + token}

    index = httpx.get(
        f"{_REGISTRY}/manifests/{channel}",
        headers={**headers, "Accept": _INDEX_TYPES},
        timeout=25.0,
        follow_redirects=True,
    )
    index.raise_for_status()
    digest = None
    for entry in (index.json() or {}).get("manifests", []) or []:
        platform = entry.get("platform") or {}
        if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
            digest = entry.get("digest")
            break
    if not digest:
        return None

    manifest = httpx.get(
        f"{_REGISTRY}/manifests/{digest}",
        headers={**headers, "Accept": _MANIFEST_TYPES},
        timeout=25.0,
        follow_redirects=True,
    )
    manifest.raise_for_status()
    config_digest = ((manifest.json() or {}).get("config") or {}).get("digest")
    if not config_digest:
        return None

    # Docker Hub answers a blob request with 307 -> production.cloudfront.docker.com.
    # Without follow_redirects this raises HTTPStatusError and the channel silently falls back.
    blob = httpx.get(
        f"{_REGISTRY}/blobs/{config_digest}", headers=headers, timeout=30.0,
        follow_redirects=True,
    )
    blob.raise_for_status()
    body = blob.json() or {}
    labels = (body.get("config") or {}).get("Labels") or {}
    version = labels.get("org.opencontainers.image.version") or labels.get("org.label-schema.version")
    version = str(version or "").strip().lstrip("v")
    if not _TAG_RE.match(version):
        return None
    return {"tag": version, "published_at": str(body.get("created") or ""), "channel": channel}


def latest_release(force: bool = False) -> dict:
    """Return {latest, recent, channel, checked_at}. Serves the in-process cache when
    fresh; when Docker Hub is unreachable, stale cache is preferred over an
    error so the admin UI never hard-fails on the release check."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["at"] < _TTL_SECONDS:
        return _CACHE["data"]

    from ..config import settings  # read at call time so a test can override it

    channel = (getattr(settings, "n8n_release_channel", "stable") or "stable").strip().lower()
    try:
        tags = _fetch_tags()
    except Exception:
        if _CACHE["data"] is not None:
            return _CACHE["data"]
        raise

    latest = tags[0] if tags else None
    if channel in ("stable", "beta"):
        # Resolve the channel to a concrete version. If the registry cannot answer, keep the
        # numeric maximum rather than blocking provisioning over a registry hiccup, and report
        # `channel: highest` so the caller can see the fallback happened.
        try:
            resolved = channel_version(channel)
        except Exception:
            resolved = None
        if resolved:
            latest = resolved

    data = {
        "latest": latest,
        "recent": tags[:10],
        "channel": "highest" if not (latest or {}).get("channel") else (latest or {}).get("channel"),
        "checked_at": int(now),
    }
    _CACHE.update(at=now, data=data)
    return data
