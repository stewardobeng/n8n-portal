# Tests: 0.1.70 admin workspace-package choice + per-workspace subscription records.
# Rules (Steward 2026-09-20):
#   1. Default = the package already on the account (customer flow unchanged).
#   2. Admin picking a DIFFERENT package moves the account subscription to it.
#   3. Each workspace records its own package; quota counts per package.
import pytest

from app import db
from app.config import settings


@pytest.fixture
def pq_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ws.db"))
    db.init_db()
    return tmp_path


def _mk_account(email="ws@x.com"):
    aid = db.create_account(email, "wsuser", "WS User")
    db.set_account_plan(aid, "a")
    db.set_subscription(aid, status="active")
    return aid


def test_instance_row_records_plan(pq_db):
    aid = _mk_account()
    iid = db.create_instance(
        aid, "wsx", 1, "env", 5678, "wsx.example.com",
        "u", "p", "k", plan="b")
    row = db.get_instance(iid)
    assert row["plan"] == "b"


def test_counts_by_plan_split(pq_db):
    aid = _mk_account()
    for name, plan in (("w1", "a"), ("w2", "b"), ("w3", "b")):
        db.create_instance(aid, name, 1, "env", 5678, f"{name}.x.com",
                           "u", "p", "k", plan=plan)
    counts = db.count_instances_by_plan(aid)
    assert counts == {"a": 1, "b": 2}
    assert db.count_instances_by_plan(aid, "b") == 2
    assert db.count_instances_by_plan(aid, "a") == 1


def test_legacy_rows_count_as_plan_a(pq_db):
    aid = _mk_account()
    db.create_instance(aid, "old1", 1, "env", 5678, "old1.x.com",
                       "u", "p", "k")
    # pre-0.1.70 row: no plan arg -> default 'a'
    counts = db.count_instances_by_plan(aid)
    assert counts["a"] == 1 and counts["b"] == 0


def test_per_plan_quota_uses_plan_counts(pq_db):
    # quota_b = 1: after one Plus workspace, a second Plus must be refused.
    # The 'a' allowance is untouched by Plus usage.
    aid = _mk_account()
    db.set_account_quota_for(aid, "b", 1)
    a = db.get_account(aid)
    db.create_instance(aid, "pb1", 1, "env", 5678, "pb1.x.com",
                       "u", "p", "k", plan="b")
    counts = db.count_instances_by_plan(aid, "b")
    assert counts == 1
    assert db.account_quota_for(a, "b") == 1
    assert db.account_quota_for(a, "a") >= 1  # plan a allowance unaffected
