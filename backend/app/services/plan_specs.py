"""Human-readable resource specs for each package (billing page).

The compose TEMPLATES are the source of truth for what a workspace actually gets
(cpus, mem_limit, node heap, pruning). Parsing them here means the customer-facing
copy can never drift from what the provisioner really deploys: change the
template, and the plans page follows automatically.

Storage note: the templates mount a dedicated named volume and PRUNE execution
history (EXECUTIONS_DATA_MAX_AGE / PRUNE_MAX_COUNT); there is deliberately no
per-workspace storage cap, and this module says so rather than inventing a
number. If Steward ever wants an advertised allowance, that is a product +
provisioner decision, not a copy change.
"""

import re

from ..config import settings

# Bytes per docker memory unit, for turning "768m" / "1g" into human text.
_MEM_UNITS = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def _read_template(plan: str) -> str:
    path = (settings.plan_b_compose_template_path if plan == "b"
            else settings.compose_template_path)
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _human_mem(raw: str) -> str:
    """"768m" -> "768 MB", "1g" -> "1 GB" (best effort; falls back to raw)."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bkmgBKMG]?)\s*", raw or "")
    if not match:
        return (raw or "").strip()
    amount, unit = float(match.group(1)), match.group(2).lower() or "b"
    total = amount * _MEM_UNITS.get(unit, 1)
    if total >= _MEM_UNITS["g"]:
        value = total / _MEM_UNITS["g"]
        return f"{value:.0f} GB" if value == int(value) else f"{value:.1f} GB"
    if total >= _MEM_UNITS["m"]:
        return f"{total / _MEM_UNITS['m']:.0f} MB"
    return f"{total / 1024:.0f} KB"


def _first(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else default


def _hours_to_text(hours: str) -> str:
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return ""
    if h % 24 == 0:
        days = h // 24
        return "day" if days == 1 else f"{days} days"
    return f"{h} hours"


def specs_for_plan(plan: str = "a") -> list[dict]:
    """Ordered [{label, value}] spec rows for one package, read from its template.

    Returns [] when the template cannot be read, so the UI can fall back to its
    own copy instead of showing misleading numbers.
    """
    plan = "b" if plan == "b" else "a"
    compose = _read_template(plan)
    if not compose:
        return []

    cpus = _first(r'cpus:\s*"?([0-9.]+)"?', compose, "")
    mem_limit = _first(r'mem_limit:\s*"([^"]+)"', compose, "")
    heap_mb = _first(r"max-old-space-size=(\d+)", compose, "")

    rows: list[dict] = []
    if cpus:
        rows.append({"label": "vCPU", "value": f"{cpus} vCPU dedicated slice"})
    if mem_limit:
        rows.append({"label": "Memory", "value": _human_mem(mem_limit)})
    if heap_mb:
        rows.append({"label": "Node.js heap", "value": f"{heap_mb} MB"})
    if plan == "b":
        try:
            from .python_runner import RUNNER_MEM_LIMIT
            runner_mem = _human_mem(RUNNER_MEM_LIMIT)
        except Exception:
            runner_mem = ""
        rows.append({"label": "Python code node",
                     "value": ("Included - managed Python runner"
                               + (f" ({runner_mem})" if runner_mem else ""))})

    rows.append({"label": "Storage",
                 "value": "Dedicated workspace volume - no fixed cap"})

    age = _hours_to_text(_first(r"EXECUTIONS_DATA_MAX_AGE=(\d+)", compose, ""))
    keep = _first(r"EXECUTIONS_DATA_PRUNE_MAX_COUNT=(\d+)", compose, "")
    if age or keep:
        pieces = []
        if age:
            pieces.append(age)
        if keep:
            pieces.append(f"{int(keep):,} runs")
        rows.append({"label": "Execution history kept",
                     "value": " or ".join(pieces)})

    timeout = _first(r"EXECUTIONS_TIMEOUT=(\d+)", compose, "")
    if timeout:
        try:
            minutes = int(timeout) // 60
            rows.append({"label": "Max run time", "value": f"{minutes} minutes per execution"})
        except ValueError:
            pass

    rows.append({"label": "Database", "value": "SQLite on a private volume"})
    return rows
