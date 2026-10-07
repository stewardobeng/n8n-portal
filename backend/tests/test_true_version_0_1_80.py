# 0.1.80 (2026-10-06): the update dialog must report the version a workspace
# REALLY runs, not the image reference it was created from.
#
# Steward's report: the dialog said a workspace was already on the latest
# release while it was on an older one. Cause: every stack is written
# `image: n8nio/n8n:latest`; the old code read that string, could not parse
# "latest" as a version, and "cannot compare" was displayed as "already on the
# latest release". Ten workspaces on 2.31.6 were reported that way while the
# newest release was 2.43.0.

import pytest

from app.services import backup_ops

ENV = 4
CID = "cid-success"
LABEL_VERSION = "2.35.5"


class StubPC:
    """Counts what the resolver asks for, so batching stays honest."""

    def __init__(self, containers=None, images=None, image_ref="n8nio/n8n:latest",
                 image_id="sha256:abc", exec_out=b"", inspect_error=False):
        self.containers = containers if containers is not None else []
        self.images = images if images is not None else []
        self.image_ref = image_ref
        self.image_id = image_id
        self.exec_out = exec_out
        self.inspect_error = inspect_error
        self.calls = []

    def list_containers(self, env, all=True):
        self.calls.append("containers")
        return self.containers

    def list_images(self, env):
        self.calls.append("images")
        return self.images

    def get_container(self, env, cid):
        if self.inspect_error:
            raise RuntimeError("inspect failed")
        return {"Image": self.image_id, "Config": {"Image": self.image_ref}}

    def exec_in_container(self, env, cid, cmd, timeout=60.0):
        self.calls.append("exec")
        return self.exec_out


def _n8n_image(version):
    return [{"Id": "sha256:abc", "RepoTags": [], "RepoDigests": [],
             "Labels": {"org.opencontainers.image.title": "n8n",
                        "org.opencontainers.image.version": version}}]


def _inst(**kw):
    row = {"id": 1, "stack_name": "success", "environment_id": ENV,
           "container_id": CID, "managed": 1, "image": "n8nio/n8n:latest"}
    row.update(kw)
    return row


@pytest.fixture(autouse=True)
def _clear_image_cache():
    """The image-version map is cached per environment for 60s."""
    backup_ops._IMAGE_CACHE.clear()
    yield
    backup_ops._IMAGE_CACHE.clear()


def _wire(monkeypatch, pc):
    monkeypatch.setattr(backup_ops, "PortainerClient", lambda *a, **k: pc)
    monkeypatch.setattr(backup_ops, "resolve_container", lambda inst: (ENV, CID))
    return pc


# ---------- current_version ----------

def test_floating_latest_reports_the_running_version(monkeypatch):
    _wire(monkeypatch, StubPC(images=_n8n_image(LABEL_VERSION)))
    got = backup_ops.current_version(_inst(), allow_exec=True)
    assert got["version"] == LABEL_VERSION
    assert got["source"] == "image-label"
    # the whole point: it must NOT echo the floating tag
    assert got["version"] != "latest"


def test_label_missing_falls_back_to_the_container(monkeypatch):
    pc = _wire(monkeypatch, StubPC(images=[], exec_out=b"2.31.6\n"))
    got = backup_ops.current_version(_inst(), allow_exec=True)
    assert got == {"version": "2.31.6", "source": "exec"}
    assert "exec" in pc.calls


def test_no_label_and_no_exec_falls_back_to_an_explicit_tag(monkeypatch):
    _wire(monkeypatch, StubPC(images=[], image_ref="n8nio/n8n:2.40.3"))
    got = backup_ops.current_version(_inst(), allow_exec=False)
    assert got == {"version": "2.40.3", "source": "tag"}


def test_unknown_version_stays_unknown(monkeypatch):
    """A floating tag with nothing identifiable must yield '' - never a guess,
    because an invented version is what made the dialog claim 'up to date'."""
    _wire(monkeypatch, StubPC(images=[], image_ref="n8nio/n8n:latest"))
    got = backup_ops.current_version(_inst(), allow_exec=False)
    assert got["version"] == ""
    assert got["source"] == "unknown"


def test_untagged_digest_reference_is_not_mistaken_for_a_version(monkeypatch):
    _wire(monkeypatch, StubPC(images=[], image_ref="sha256:c5861e6016c8f283"))
    got = backup_ops.current_version(_inst(), allow_exec=False)
    assert got["version"] == ""
    assert got["source"] == "unknown"


def test_inspect_failure_does_not_invent_a_version(monkeypatch):
    _wire(monkeypatch, StubPC(inspect_error=True))
    got = backup_ops.current_version(_inst(), allow_exec=False)
    assert got["version"] == ""
    assert got["source"] == "unknown"


def test_stored_explicit_tag_is_used_when_the_container_is_gone(monkeypatch):
    monkeypatch.setattr(backup_ops, "PortainerClient", lambda *a, **k: StubPC())
    monkeypatch.setattr(backup_ops, "resolve_container", lambda inst: None)
    got = backup_ops.current_version(_inst(image="n8nio/n8n:2.41.2"))
    assert got == {"version": "2.41.2", "source": "tag"}
    got2 = backup_ops.current_version(_inst(image="n8nio/n8n:latest"))
    assert got2 == {"version": "", "source": "unknown"}


# ---------- true_versions (batch, used by the workspace list) ----------

def _container(name, image_id="sha256:abc", image="n8nio/n8n:latest"):
    return {"Id": "c-" + name, "Names": ["/" + name], "Image": image,
            "ImageID": image_id, "State": "running"}


def test_true_versions_is_one_call_set_per_environment(monkeypatch):
    pc = StubPC(containers=[
        _container("success-n8n-1"),
        _container("chibz-n8n-1", image_id="sha256:zzz", image="n8nio/n8n:2.40.3"),
    ], images=_n8n_image(LABEL_VERSION))
    insts = [_inst(id=1, stack_name="success"),
             _inst(id=2, stack_name="chibz")]
    got = backup_ops.true_versions(insts, pc=pc)
    assert got[1] == {"version": LABEL_VERSION, "source": "image-label"}
    assert got[2] == {"version": "2.40.3", "source": "tag"}   # no label for that id
    # batched: one container list + one image list for the whole environment
    assert pc.calls.count("containers") == 1
    assert pc.calls.count("images") == 1


def test_true_versions_second_env_is_scanned_separately(monkeypatch):
    class TwoEnvPC(StubPC):
        def list_containers(self, env, all=True):
            self.calls.append(("containers", env))
            return [_container(f"a-n8n-1")] if env == 4 else []

    pc = TwoEnvPC(images=_n8n_image("2.43.0"))
    got = backup_ops.true_versions([_inst(id=1, stack_name="a", environment_id=4),
                                    _inst(id=2, stack_name="b", environment_id=8)], pc=pc)
    assert got[1] == {"version": "2.43.0", "source": "image-label"}
    assert got[2]["source"] == "unknown"   # container not on that server
    assert pc.calls.count(("containers", 4)) == 1
    assert pc.calls.count(("containers", 8)) == 1


def test_true_versions_reports_unknown_rather_than_up_to_date(monkeypatch):
    """The reported bug in one assertion: a 2.31.6 workspace behind the newest
    release must never be resolved to a version equal to the latest tag."""
    latest = "2.43.0"
    pc = StubPC(containers=[_container("mustapha-n8n-1")],
                images=_n8n_image("2.31.6"))
    got = backup_ops.true_versions([_inst(id=7, stack_name="mustapha",
                                          environment_id=8)], pc=pc)[7]
    assert got["version"] == "2.31.6"
    assert got["version"] != latest
    assert tuple(int(x) for x in got["version"].split(".")) < \
        tuple(int(x) for x in latest.split("."))
