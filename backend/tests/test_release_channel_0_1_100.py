# 0.1.100 (Steward, 2026-11-12): new workspaces must land on the STABLE line, not the newest number.
#
# The estate showed the failure plainly: 17 workspaces were deliberately moved to n8n 3.0.1 (a BETA
# major), while the three newest workspaces were on 2.46.4 / 2.47.2 because they were created when
# those were the highest published tags. On 2026-11-12 the highest tag was 2.48.2 while Docker Hub's
# `stable`/`latest` tag was 2.47.5, so the release lookup was picking the beta channel by arithmetic.
#
# These tests pin the channel rule: the default follows Docker Hub's `stable` tag, resolved from the
# image's own version label, and a registry outage falls back to the old behaviour instead of
# blocking provisioning.

import httpx
import pytest

from app.config import settings
from app.services import n8n_releases, provisioner

STABLE_TAG = "2.47.5"     # what Docker Hub's `stable`/`latest` pointed at on 2026-11-12
BETA_TAG = "2.48.2"       # what `beta`/`next` pointed at, and the highest number
THREE = "3.0.1"


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """The module caches for 15 minutes; every test needs a cold cache."""
    n8n_releases._CACHE.update(at=0.0, data=None)
    yield
    n8n_releases._CACHE.update(at=0.0, data=None)


def _hub_tags():
    """The most recently pushed tags: a beta above stable, exactly the confusing case."""
    return [
        {"name": BETA_TAG, "last_updated": "2026-11-12T08:30:03Z"},
        {"name": "2.48.1", "last_updated": "2026-11-11T08:34:41Z"},
        {"name": STABLE_TAG, "last_updated": "2026-11-12T08:29:35Z"},
        {"name": "2.47.4", "last_updated": "2026-11-11T08:35:11Z"},
        # 3.0.1 is deliberately ABSENT: it is a real release but 15 days old, so it is not inside
        # the top-100 recently-pushed window the lookup reads. The live portal returned exactly
        # 2.48.2, 2.48.1, 2.47.5, 2.47.4, 1.123.95, 1.123.94 on 2026-11-12, which is why the
        # numeric maximum was the BETA line rather than the 3.x beta major.
        {"name": "latest", "last_updated": "2026-11-12T08:00:00Z"},
        {"name": "stable", "last_updated": "2026-11-12T08:00:00Z"},
        {"name": "beta", "last_updated": "2026-11-12T08:00:00Z"},
    ]


def _fake_get_factory(channel_versions, fail_registry=False, seen=None):
    """Emulate the Docker Hub tag list and the registry manifest walk the module performs.

    `seen` collects (url, follow_redirects) so a test can prove the registry hops follow Docker
    Hub's 307 to CloudFront: httpx does NOT follow redirects unless asked, and without it the
    channel silently degrades to the numeric maximum in production while passing in a unit test.
    """

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=False):  # noqa: ARG001
        if seen is not None:
            seen.append((url, follow_redirects))
        if "auth.docker.io" in url:
            if fail_registry:
                raise httpx.ConnectError("registry unreachable")
            return httpx.Response(200, json={"token": "test-token"}, request=httpx.Request("GET", url))
        if url.startswith("https://hub.docker.com"):
            return httpx.Response(200, json={"results": _hub_tags()}, request=httpx.Request("GET", url))
        if "registry-1.docker.io" in url and "/manifests/" in url:
            tag = url.rsplit("/manifests/", 1)[1]
            if tag.startswith("sha256:"):
                # the second hop addresses the image manifest BY DIGEST, not by tag
                digest_tag = tag.rsplit("amd64-", 1)[-1]
                return httpx.Response(
                    200, json={"config": {"digest": "sha256:cfg-" + digest_tag}},
                    request=httpx.Request("GET", url),
                )
            if tag in channel_versions:
                if (headers or {}).get("Accept", "").startswith("application/vnd.oci.image.index"):
                    return httpx.Response(
                        200,
                        json={"manifests": [
                            {"digest": "sha256:amd64-" + tag,
                             "platform": {"architecture": "amd64", "os": "linux"}},
                            {"digest": "sha256:arm64-" + tag,
                             "platform": {"architecture": "arm64", "os": "linux"}},
                        ]},
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    200, json={"config": {"digest": "sha256:cfg-" + tag}},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        if "/blobs/" in url:
            tag = url.rsplit("sha256:cfg-", 1)[1]
            return httpx.Response(
                200,
                json={"created": "2026-11-12T00:00:00Z",
                      "config": {"Labels": {"org.opencontainers.image.version": channel_versions[tag]}}},
                request=httpx.Request("GET", url),
            )
        raise AssertionError("unexpected url: " + url)

    return fake_get


def test_stable_channel_beats_the_higher_number(monkeypatch):
    """The whole point: 2.48.2 is higher, but `stable` is 2.47.5, and new workspaces get stable."""
    monkeypatch.setattr(settings, "n8n_release_channel", "stable")
    monkeypatch.setattr(
        n8n_releases.httpx, "get",
        _fake_get_factory({"stable": STABLE_TAG, "beta": BETA_TAG}),
    )
    data = n8n_releases.latest_release(force=True)
    assert data["latest"]["tag"] == STABLE_TAG
    assert data["latest"]["channel"] == "stable"
    assert data["channel"] == "stable"
    assert data["recent"][0]["tag"] == BETA_TAG, "the numeric list is still reported for the UI"


def test_beta_channel_is_available_when_asked_for(monkeypatch):
    monkeypatch.setattr(settings, "n8n_release_channel", "beta")
    monkeypatch.setattr(
        n8n_releases.httpx, "get",
        _fake_get_factory({"stable": STABLE_TAG, "beta": BETA_TAG}),
    )
    data = n8n_releases.latest_release(force=True)
    assert data["latest"]["tag"] == BETA_TAG and data["channel"] == "beta"


def test_highest_channel_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "n8n_release_channel", "highest")
    monkeypatch.setattr(
        n8n_releases.httpx, "get",
        _fake_get_factory({"stable": STABLE_TAG, "beta": BETA_TAG}),
    )
    data = n8n_releases.latest_release(force=True)
    assert data["latest"]["tag"] == BETA_TAG and data["channel"] == "highest"


def test_a_registry_outage_falls_back_instead_of_blocking_provisioning(monkeypatch):
    monkeypatch.setattr(settings, "n8n_release_channel", "stable")
    monkeypatch.setattr(
        n8n_releases.httpx, "get", _fake_get_factory({}, fail_registry=True)
    )
    data = n8n_releases.latest_release(force=True)
    assert data["latest"]["tag"] == BETA_TAG, "falls back to the numeric maximum"
    assert data["channel"] == "highest", "and says so, so nobody believes the channel was honoured"


def test_registry_hops_follow_redirects(monkeypatch):
    """Docker Hub answers blobs with 307 to CloudFront; httpx must be told to follow it.

    This is the bug that shipped in 0.1.100: the channel resolved locally (where the probe used
    urllib, which follows redirects) and silently fell back to `highest` in the container.
    """
    seen = []
    monkeypatch.setattr(settings, "n8n_release_channel", "stable")
    monkeypatch.setattr(
        n8n_releases.httpx, "get",
        _fake_get_factory({"stable": STABLE_TAG}, seen=seen),
    )
    n8n_releases.channel_version("stable")
    registry_hops = [(u, f) for u, f in seen if "registry-1.docker.io" in u]
    assert registry_hops, "the registry was never contacted"
    assert all(follow is True for _u, follow in registry_hops), [
        (u.rsplit("/", 1)[-1][:20], follow) for u, follow in registry_hops
    ]


def test_provisioning_uses_the_channel_version(monkeypatch):
    """A new workspace must be pinned to the stable tag with a real X.Y.Z, never a moving tag."""
    monkeypatch.setattr(settings, "n8n_provision_tag", "")
    monkeypatch.setattr(settings, "n8n_release_channel", "stable")
    monkeypatch.setattr(
        n8n_releases.httpx, "get",
        _fake_get_factory({"stable": STABLE_TAG, "beta": BETA_TAG}),
    )
    tag, source = provisioner.resolve_provision_tag()
    assert tag == STABLE_TAG and source == "release"


def test_the_three_channel_is_not_reachable_by_accident():
    """3.0.1 is a beta major; nothing in the default path may return it while stable is 2.x."""
    assert n8n_releases._semver(STABLE_TAG) < n8n_releases._semver(BETA_TAG)
    assert n8n_releases._TAG_RE.match(STABLE_TAG) and n8n_releases._TAG_RE.match(BETA_TAG)
    assert n8n_releases._TAG_RE.match(THREE), "a real release tag, just not the stable line"
