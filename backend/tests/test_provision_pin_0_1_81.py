# 0.1.81 (2026-10-06): a NEW workspace is pinned to a real n8n release tag,
# never the floating `latest` tag.
#
# Why: `latest` is resolved per host at every container recreate, so
#   attaching a Python runner, a Portainer redeploy, or a host-level compose up
#   could silently move a customer onto a newer n8n and run its one-way DB
#   migrations with nobody deciding it. Steward approved: pin the newest
#   release at provision time, with a configurable hold-back tag and a
#   fallback tag so provisioning never floats.

import json
import re
from pathlib import Path

import pytest

from app.services import provisioner
from app.services.provisioner import ProvisionError
import app.config as cfg

TPL = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture(autouse=True)
def _templates(monkeypatch):
    """Point the settings at the real repo templates and clear the knobs."""
    monkeypatch.setattr(cfg.settings, "compose_template_path", str(TPL / "n8n-stack-compose.yml"))
    monkeypatch.setattr(cfg.settings, "plan_b_compose_template_path",
                        str(TPL / "n8n-stack-compose-plus.yml"))
    monkeypatch.setattr(cfg.settings, "n8n_provision_tag", "")
    monkeypatch.setattr(cfg.settings, "n8n_fallback_tag", "2.43.1")


def _release(tag, monkeypatch, calls=None):
    def fake(force=False):
        if calls is not None:
            calls.append(tag)
        return {"latest": {"tag": tag, "published_at": "2026-10-06T00:00:00Z"}, "recent": []}
    monkeypatch.setattr(provisioner.n8n_releases, "latest_release", fake)


def _broken(monkeypatch):
    def boom(force=False):
        raise RuntimeError("docker hub unreachable")
    monkeypatch.setattr(provisioner.n8n_releases, "latest_release", boom)


# ---------- which tag a new workspace gets ----------

def test_new_workspace_pins_the_newest_release(monkeypatch):
    _release("2.44.0", monkeypatch)
    assert provisioner.resolve_provision_tag() == ("2.44.0", "release")


def test_v_prefix_is_stripped(monkeypatch):
    _release("v2.44.0", monkeypatch)
    assert provisioner.resolve_provision_tag() == ("2.44.0", "release")


def test_hold_back_tag_wins_and_skips_the_lookup(monkeypatch):
    """N8N_PROVISION_TAG is Steward holding new workspaces on a chosen version."""
    calls = []
    _release("2.44.0", monkeypatch, calls)
    monkeypatch.setattr(cfg.settings, "n8n_provision_tag", "2.40.3")
    assert provisioner.resolve_provision_tag() == ("2.40.3", "override")
    assert calls == [], "the release lookup must not run when the tag is pinned by setting"


def test_unreachable_hub_falls_back_to_the_configured_tag(monkeypatch):
    _broken(monkeypatch)
    monkeypatch.setattr(cfg.settings, "n8n_fallback_tag", "2.41.2")
    assert provisioner.resolve_provision_tag() == ("2.41.2", "fallback")


def test_junk_or_empty_lookup_falls_back(monkeypatch):
    for junk in ("latest", "", "nightly", None):
        _release(junk, monkeypatch)
        assert provisioner.resolve_provision_tag() == ("2.43.1", "fallback"), junk


def test_fails_closed_when_no_real_tag_is_available(monkeypatch):
    """Worse to float a customer than to stop and say so."""
    _broken(monkeypatch)
    monkeypatch.setattr(cfg.settings, "n8n_fallback_tag", "")
    with pytest.raises(ProvisionError) as e:
        provisioner.resolve_provision_tag()
    assert "N8N_PROVISION_TAG" in str(e.value)


# ---------- what the rendered compose actually contains ----------

@pytest.mark.parametrize("plan", ["a", "b"])
def test_rendered_compose_never_floats(plan, monkeypatch):
    compose = provisioner.load_compose_template(plan, tag="2.44.0")
    assert "image: n8nio/n8n:2.44.0" in compose
    assert ":latest" not in compose
    # the rest of the template is untouched
    assert "restart: unless-stopped" in compose
    assert re.search(r"^\s+image: n8nio/n8n:2\.44\.0$", compose, re.M)


def test_bare_call_is_offline_and_deterministic(monkeypatch):
    """Previews and tests must not hit Docker Hub: fall back to the setting."""
    _broken(monkeypatch)          # would raise if the bare call tried the lookup
    monkeypatch.setattr(cfg.settings, "n8n_fallback_tag", "2.41.2")
    assert "image: n8nio/n8n:2.41.2" in provisioner.load_compose_template("a")
    monkeypatch.setattr(cfg.settings, "n8n_provision_tag", "2.40.3")
    assert "image: n8nio/n8n:2.40.3" in provisioner.load_compose_template("a")


def test_render_is_idempotent(monkeypatch):
    once = provisioner.render_compose("image: n8nio/n8n:latest\n", "2.44.0")
    assert provisioner.render_compose(once, "2.44.0") == once


def test_image_name_constant_matches_the_templates():
    """Drift guard: if a template renames the image, the substitution and the
    recorded version must not silently stop matching."""
    for name in ("n8n-stack-compose.yml", "n8n-stack-compose-plus.yml"):
        text = (TPL / name).read_text()
        assert f"image: {provisioner.N8N_IMAGE_NAME}:" in text, name


def test_provisioning_path_deploys_and_records_the_same_tag():
    """The wiring, asserted against the source: resolve once, record it, deploy
    it. A mismatch here is how a portal record starts lying about a workspace."""
    src = Path(provisioner.__file__).read_text()
    assert "image_tag, image_source = resolve_provision_tag()" in src
    assert "db.update_instance(instance_id, image=full_image)" in src
    assert "load_compose_template(plan_tier, tag=image_tag)" in src
    assert "full_image = f\"{N8N_IMAGE_NAME}:{image_tag}\"" in src
    assert json.dumps("load_compose_template(plan_tier)") not in src  # no unpinned call left
