# Tests: 0.1.71 auto-provision after charge.success + plan-aware planName.
import pytest

from app import db
from app.config import settings


@pytest.fixture
def ap_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ap.db"))
    db.init_db()
    return tmp_path


def test_charge_success_autoprovisions_first_workspace(ap_db, monkeypatch):
    """A paid customer with zero workspaces gets one created automatically
    from the signup details (0.1.71, Steward)."""
    from app.services import paystack as ps
    aid = db.create_account("auto@x.com", "autouser", "Auto User")
    db.set_account_plan(aid, "b")
    data = {"metadata": {"account_id": str(aid), "app": "n8n-portal"},
            "customer": {"customer_code": "CUS_x"},
            "amount": 50000}
    # stub the heavy provisioner so no Docker/NPM is touched; record the call
    calls = {}
    import app.services.provisioner as prov
    monkeypatch.setattr(prov, "provision_account",
                        lambda account_id, password=None, workspace=None, plan=None:
                        calls.update({"id": account_id}) or {"ok": True})
    res = ps._on_charge_success(data)
    assert res["status"] == "active"
    assert calls.get("id") == aid


def test_charge_success_skips_autoprovision_when_workspaces_exist(ap_db, monkeypatch):
    from app.services import paystack as ps
    import app.services.provisioner as prov
    aid = db.create_account("has@x.com", "hasuser", "Has User")
    db.create_instance(aid, "existing", 1, "env", 5678, "e.x.com", "u", "p", "k",
                       status="healthy")
    called = {"n": 0}
    monkeypatch.setattr(prov, "provision_account",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = ps._on_charge_success({"metadata": {"account_id": str(aid)},
                                 "customer": {}, "amount": 30000})
    assert res["status"] == "active"
    assert called["n"] == 0, "must not re-provision; existing workspaces untouched"
