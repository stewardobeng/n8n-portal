"""Plan B (GHS 500 Plus tier) — 0.1.61 (Steward 2026-09-05).

The 500 package provisions on the chibz/bigcroc owner-tier compose
(0.50 CPU / 1 GiB / heap 768) instead of the lean canonical template.
Covers: template selection, plans endpoint activation, Paystack Plan B
plan creation payload, and webhook plan resolution.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TPL_A = str(REPO / "templates" / "n8n-stack-compose.yml")
TPL_B = str(REPO / "templates" / "n8n-stack-compose-plus.yml")


def test_plans_endpoint_b_toggles_active():
    from app.main import app
    from fastapi.testclient import TestClient
    import app.config as cfg

    old = cfg.settings.plan_b_active
    try:
        client = TestClient(app)
        cfg.settings.plan_b_active = True
        r = client.get("/api/v1/plans")
        assert r.status_code == 200
        plans = r.json()["plans"]
        assert len(plans) == 2
        p2 = plans[1]
        assert p2["amount_minor"] == 50000 and p2["currency"] == "GHS"
        assert p2["active"] is True

        cfg.settings.plan_b_active = False
        r = client.get("/api/v1/plans")
        assert r.json()["plans"][1]["active"] is False
    finally:
        cfg.settings.plan_b_active = old


def test_compose_template_plan_selection():
    from app.services import provisioner
    import app.config as cfg

    old_a = cfg.settings.compose_template_path
    old_b = cfg.settings.plan_b_compose_template_path
    try:
        cfg.settings.compose_template_path = TPL_A
        cfg.settings.plan_b_compose_template_path = TPL_B
        lean = provisioner.load_compose_template("a")
        plus = provisioner.load_compose_template("b")
        assert 'cpus: "0.30"' in lean and 'mem_limit: "768m"' in lean
        assert "--max-old-space-size=384" in lean
        assert 'cpus: "0.50"' in plus and 'mem_limit: "1g"' in plus
        assert "--max-old-space-size=768" in plus
        # default (no plan arg / legacy callers) stays Plan A canonical
        assert provisioner.load_compose_template() == lean
    finally:
        cfg.settings.compose_template_path = old_a
        cfg.settings.plan_b_compose_template_path = old_b


def test_paystack_ensure_plan_b_payload(monkeypatch):
    from app.services import paystack
    import app.config as cfg

    created = []
    calls = []

    def fake_req(method, path, body=None):
        if path == "/plan?perPage=100" and method == "GET":
            return {"status": True, "data": []}
        if path == "/plan" and method == "POST":
            created.append(body)
            return {"status": True, "data": {"plan_code": "PLN_" + body["name"][:4].upper() + "_B"}}
        return {"status": True, "data": {}}

    old_key = paystack.settings.paystack_secret_key
    old_code = paystack.settings.paystack_plan_code
    old_code_b = paystack.settings.paystack_plan_code_b
    try:
        paystack.settings.paystack_secret_key = "sk_test_planb"
        paystack.settings.paystack_plan_code = ""
        paystack.settings.paystack_plan_code_b = ""
        monkeypatch.setattr(paystack, "_req", fake_req)
        code_b = paystack.ensure_plan_b()
        assert code_b.endswith("_B")
        b = created[-1]
        assert b["name"] == paystack.settings.plan_b_name
        assert b["amount"] == 50000 and b["interval"] == "annually"
        # Plan A still uses its own name/amount
        code_a = paystack.ensure_plan()
        assert code_a.endswith("_B")  # fake registry maps any POST; name checked below
        assert created[-1]["name"] == paystack.settings.plan_name
        assert created[-1]["amount"] == 30000
    finally:
        paystack.settings.paystack_secret_key = old_key
        paystack.settings.paystack_plan_code = old_code
        paystack.settings.paystack_plan_code_b = old_code_b


def test_webhook_plan_resolution():
    from app.services import paystack
    import app.config as cfg

    old_active = cfg.settings.plan_b_active
    old_code_b = cfg.settings.paystack_plan_code_b
    try:
        cfg.settings.plan_b_active = False
        cfg.settings.paystack_plan_code_b = ""
        assert paystack._resolve_plan({}) == "a"
        # amount only counts when Plan B is active
        assert paystack._resolve_plan({"amount": 50000}) == "a"
        cfg.settings.plan_b_active = True
        assert paystack._resolve_plan({"amount": 50000}) == "b"
        assert paystack._resolve_plan({"amount": 30000}) == "a"
        # plan code wins regardless of the amount
        cfg.settings.paystack_plan_code_b = "PLN_PLUS"
        assert paystack._resolve_plan({"plan": {"plan_code": "PLN_PLUS"}, "amount": 1}) == "b"
    finally:
        cfg.settings.plan_b_active = old_active
        cfg.settings.paystack_plan_code_b = old_code_b
