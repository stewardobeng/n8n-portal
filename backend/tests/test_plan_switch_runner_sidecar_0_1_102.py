"""0.1.102: a workspace with a Python runner sidecar must still be able to change package.

The regression this pins: `rewrite_resources` scanned the WHOLE stack file for the resource lines.
Once the portal attaches the Python support, the file carries a second service (`runners`) with its
own `mem_limit: 384m`, so the scan found two memory lines and the switch aborted with

    Stack file has 2 memory limit line(s), expected exactly 1

which surfaced to Steward as a 409 on the plan preview and a UI that silently did nothing. Every
runner-enabled workspace (7 of them) could never change package.
"""
import os

import pytest

from app.config import settings
from app.services import plan_switch


@pytest.fixture(autouse=True)
def _repo_templates(monkeypatch):
    """Point both template settings at the repo copies (in the container they live at /app/templates),
    because the switch reads a template to know what a package IS."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setattr(settings, "compose_template_path",
                        os.path.join(repo, "templates", "n8n-stack-compose.yml"))
    monkeypatch.setattr(settings, "plan_b_compose_template_path",
                        os.path.join(repo, "templates", "n8n-stack-compose-plus.yml"))

# A deployed stack shaped like a real one with the runner sidecar attached: two services, each with
# its own resource lines, plus the inline runner config the portal injects.
STACK_WITH_RUNNER = '''services:
  n8n:
    image: n8nio/n8n:3.2.0
    restart: unless-stopped
    cpus: "0.30"
    mem_limit: "768m"
    mem_reservation: "256m"
    environment:
      - N8N_RUNNERS_ENABLED=true
      - N8N_RUNNERS_MODE=external
      - NODE_OPTIONS=--max-old-space-size=384
    ports:
      - "32792:5678"
    volumes:
      - n8n_data:/home/node/.n8n
  # >>> portal-runner:service-start
  runners:
    image: n8nio/runners:3.2.0
    restart: unless-stopped
    mem_limit: "384m"
    mem_reservation: "128m"
    environment:
      - N8N_RUNNERS_TASK_BROKER_URI=http://n8n:5679
    configs:
      - portal-runner-config
    depends_on:
      - n8n
  # >>> portal-runner:service-end
volumes:
  n8n_data:
configs:
  portal-runner-config:
    content: '{"task-runners":[]}'
'''


def test_switch_works_with_a_runner_sidecar_present():
    new, changes = plan_switch.rewrite_resources(STACK_WITH_RUNNER, "b")
    assert all(c["change"] == "changed" for c in changes), changes
    # the n8n service moved to the Plus package
    assert 'cpus: "0.50"' in new
    assert 'mem_limit: "1g"' in new
    assert "max-old-space-size=768" in new
    # and the old values are gone from the n8n service
    assert 'cpus: "0.30"' not in new
    assert "max-old-space-size=384" not in new


def test_the_runner_sidecar_is_never_retuned():
    new, _ = plan_switch.rewrite_resources(STACK_WITH_RUNNER, "b")
    assert 'mem_limit: "384m"' in new, "the runner's own memory limit must survive untouched"
    assert new.count('mem_limit: "1g"') == 1, "only the n8n service may get 1g"
    assert "n8nio/runners:3.2.0" in new, "the runner image must not be changed"
    assert "N8N_RUNNERS_TASK_BROKER_URI=http://n8n:5679" in new
    assert "portal-runner-config" in new


def test_switching_back_restores_the_standard_package():
    up, _ = plan_switch.rewrite_resources(STACK_WITH_RUNNER, "b")
    down, changes = plan_switch.rewrite_resources(up, "a")
    assert all(c["change"] == "changed" for c in changes)
    assert 'cpus: "0.30"' in down and 'mem_limit: "768m"' in down
    assert "max-old-space-size=384" in down
    assert 'mem_limit: "384m"' in down, "runner still untouched on the way back"


def test_a_single_service_stack_still_switches():
    """Regression: workspaces without a runner must keep working exactly as before."""
    with open(plan_switch._template_path("a")) as fh:
        plain = fh.read()
    assert "runners:" not in plain or "portal-runner" not in plain
    new, changes = plan_switch.rewrite_resources(plain, "b")
    assert all(c["change"] == "changed" for c in changes)
    assert 'cpus: "0.50"' in new and 'mem_limit: "1g"' in new


def test_already_matching_reports_no_change():
    with open(plan_switch._template_path("b")) as fh:
        plus = fh.read()
    _, changes = plan_switch.rewrite_resources(plus, "b")
    assert all(c["change"] == "unchanged" for c in changes), changes


def test_missing_n8n_service_refuses_clearly():
    with pytest.raises(ValueError) as exc:
        plan_switch.rewrite_resources("services:\n  web:\n    image: nginx\n", "b")
    assert "n8n" in str(exc.value) and "untouched" in str(exc.value)


def test_ambiguous_lines_inside_the_service_still_fail_closed():
    doubled = STACK_WITH_RUNNER.replace(
        '    mem_limit: "768m"', '    mem_limit: "768m"\n    mem_limit: "512m"', 1
    )
    with pytest.raises(ValueError) as exc:
        plan_switch.rewrite_resources(doubled, "b")
    assert "expected exactly 1" in str(exc.value)


def test_service_block_does_not_bleed_into_the_next_service():
    start, end = plan_switch._service_block(STACK_WITH_RUNNER, "n8n")
    block = STACK_WITH_RUNNER[start:end]
    assert "cpus:" in block and "mem_limit:" in block
    assert "runners:" not in block, "the block must end before the next service"
    assert "portal-runner" not in block
