# Latest official n8n release lookup (admin "update n8n" watch, 2026-09-03).
# Docker Hub is the source of truth: it is exactly what the environment servers
# pull from when a workspace image is updated. We fetch the most recently pushed
# tags for n8nio/n8n, keep only clean X.Y.Z release tags (dropping latest/nightly
# /beta and similar), and return the newest by semver order. Results are cached
# in-process for 15 minutes so the admin UI checks are cheap.

import re
import time

import httpx

_TAG_RE = re.compile(r"^\d+\.\d+\.\d+$")
_CACHE: dict = {"at": 0.0, "data": None}
_TTL_SECONDS = 15 * 60
_HUB_URL = "https://hub.docker.com/v2/repositories/n8nio/n8n/tags"


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


def latest_release(force: bool = False) -> dict:
    """Return {latest, recent, checked_at}. Serves the in-process cache when
    fresh; when Docker Hub is unreachable, stale cache is preferred over an
    error so the admin UI never hard-fails on the release check."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["at"] < _TTL_SECONDS:
        return _CACHE["data"]
    try:
        tags = _fetch_tags()
    except Exception:
        if _CACHE["data"] is not None:
            return _CACHE["data"]
        raise
    data = {
        "latest": tags[0] if tags else None,
        "recent": tags[:10],
        "checked_at": int(now),
    }
    _CACHE.update(at=now, data=data)
    return data
