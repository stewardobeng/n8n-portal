# 0.1.79 (2026-10-05): instances whose container references its image by ID were
# invisible to every portal listing, including the attach dropdown.
#
# Real case Steward reported: success.steprotech.com. The container runs on n8n
# server 1, publishes 32770, and nginx-proxy-manager forwards the domain to it -
# but its image (n8n 2.35.5) had lost its tags, so the Docker API reported
#   Image   = "sha256:c5861e6016c8f283142584190e3874e6aa6f322eca8771ceead09d08b4766a1e"
#   RepoTags = []   RepoDigests = []
# and the old check (substring "n8nio/n8n" against Image) could never match.
# Ten instances on that server were affected; servers 2 and 3 were fine.

import pytest

from app import db
from app.services import admin_ops

DIGEST = "sha256:c5861e6016c8f283142584190e3874e6aa6f322eca8771ceead09d08b4766a1e"


class StubPC:
    def __init__(self, endpoints=None, containers_by_env=None, images_by_env=None):
        self._eps = endpoints or []
        self._containers = containers_by_env or {}
        self._images = images_by_env or {}

    def list_endpoints(self):
        return self._eps

    def get_endpoint(self, endpoint_id):
        for e in self._eps:
            if e["Id"] == endpoint_id:
                return e
        raise RuntimeError("not found")

    def list_containers(self, endpoint_id, all=True):
        return self._containers.get(endpoint_id, [])

    def list_images(self, endpoint_id):
        if endpoint_id not in self._images:
            raise RuntimeError("Portainer image list unavailable")
        return self._images[endpoint_id]

    def list_stacks(self):
        return []

    def stop_container(self, endpoint_id, container_id):
        pass

    def start_container(self, endpoint_id, container_id):
        pass


class StubNPM:
    def __init__(self, hosts=None):
        self._hosts = hosts or []

    def list_proxy_hosts(self):
        return self._hosts


def _endpoints():
    return [
        {"Id": 3, "Name": "local", "URL": "unix:///var/run/docker.sock"},
        {"Id": 4, "Name": "n8n-cloud", "URL": "tcp://141.148.139.50:9001",
         "PublicURL": "141.148.139.50"},
    ]


def _container(name, project, state="running", port=32770, image=DIGEST,
               image_id=DIGEST, private_port=5678):
    return {
        "Id": name,
        "Names": ["/" + name],
        "Image": image,
        "ImageID": image_id,
        "State": state,
        "Ports": [{"PrivatePort": private_port, "PublicPort": port, "Type": "tcp"}],
        "Labels": {"com.docker.compose.project": project},
    }


def _n8n_image():
    """The real thing: untagged, no digests, identified only by its OCI label."""
    return [{"Id": DIGEST, "RepoTags": [], "RepoDigests": [],
             "Labels": {"org.opencontainers.image.title": "n8n",
                        "org.opencontainers.image.version": "2.35.5"}}]


def _npm_host(domain, port=32770):
    return [{"domain_names": [domain], "forward_host": "141.148.139.50",
             "forward_port": port}]


def _account(email):
    return db.create_account(email, email.split("@")[0], "", "", "",
                             db.new_key(prefix="h", nbytes=16))


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Own database per test (get_conn must be redirected too, since it falls
    back to settings.db_path), so this module also passes when run on its own."""
    path = str(tmp_path / "t.db")
    db.init_db(path)
    real = db.get_conn

    def _conn(db_path=None):
        return real(db_path or path)

    monkeypatch.setattr(db, "get_conn", _conn)
    yield


# ---------- discovery ----------

def test_digest_referenced_n8n_is_discovered():
    pc = StubPC(_endpoints(), {4: [_container("success-n8n-1", "success")]},
                {4: _n8n_image()})
    found = admin_ops.discover_unlinked_stacks(pc, StubNPM(_npm_host("success.steprotech.com")))
    names = {f["stack_name"]: f for f in found}
    assert "success" in names, f"digest-referenced instance still hidden: {list(names)}"
    assert names["success"]["running"] is True
    assert names["success"]["port"] == 32770
    assert names["success"]["domain"] == "success.steprotech.com"
    assert names["success"]["environment_id"] == 4


def test_untagged_image_without_the_n8n_label_is_not_discovered():
    """A resolved image that is NOT n8n must stay excluded (no false positives)."""
    pc = StubPC(_endpoints(), {4: [_container("other-n8n-1", "other")]},
                {4: [{"Id": DIGEST, "RepoTags": [], "RepoDigests": [],
                      "Labels": {"org.opencontainers.image.title": "postgres"}}]})
    found = admin_ops.discover_unlinked_stacks(pc, StubNPM([]))
    assert all(f["stack_name"] != "other" for f in found)


def test_named_tag_still_discovered():
    """Regression: the original name-based path must keep working."""
    pc = StubPC(_endpoints(),
                {4: [_container("olisa-n8n-1", "olisa", port=32771,
                                image="n8nio/n8n:latest", image_id="sha256:aaa")]},
                {4: []})
    found = admin_ops.discover_unlinked_stacks(pc, StubNPM([]))
    assert any(f["stack_name"] == "olisa" for f in found)


def test_discovery_survives_an_unreadable_image_list():
    """If Portainer cannot list images, fall back to n8n's 5678 signature
    rather than hiding live instances."""
    pc = StubPC(_endpoints(), {4: [_container("success-n8n-1", "success")]}, {})
    found = admin_ops.discover_unlinked_stacks(pc, StubNPM([]))
    assert any(f["stack_name"] == "success" for f in found)


# ---------- attach ----------

def test_attach_accepts_digest_referenced_stack():
    acc = _account("succ@steprotech.com")
    pc = StubPC(_endpoints(), {4: [_container("success-n8n-1", "success")]},
                {4: _n8n_image()})
    res = admin_ops.attach_instance(acc, 4, "success", 32770,
                                    "success.steprotech.com", pc=pc)
    assert res
    inst = db.get_instance_by_stack_name("success")
    assert inst is not None
    assert inst["domain"] == "success.steprotech.com"
    assert inst["port"] == 32770


def test_attach_still_refuses_what_it_cannot_identify():
    acc = _account("nope@steprotech.com")
    pc = StubPC(_endpoints(), {4: [_container("other-n8n-1", "other")]},
                {4: [{"Id": DIGEST, "RepoTags": [], "RepoDigests": [],
                      "Labels": {"org.opencontainers.image.title": "postgres"}}]})
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.attach_instance(acc, 4, "other", 0, "other.steprotech.com", pc=pc)


# ---------- the helper itself ----------

def test_is_n8n_container_paths():
    ids = {DIGEST}
    assert admin_ops._is_n8n_container(_container("a-n8n-1", "a"), ids) is True
    assert admin_ops._is_n8n_container(_container("b-n8n-1", "b"), set()) is False
    # unreadable image list -> port signature decides
    assert admin_ops._is_n8n_container(_container("c-n8n-1", "c"), None) is True
    assert admin_ops._is_n8n_container(_container("d-n8n-1", "d", private_port=5432), None) is False
    # a named tag wins even with no image list
    assert admin_ops._is_n8n_container(
        _container("e-n8n-1", "e", image="n8nio/n8n:2.40.5"), set()) is True
