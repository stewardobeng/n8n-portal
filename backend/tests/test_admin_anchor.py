# Tests for the NPM created_on subscription anchor (2026-09-02):
# proxy-host creation date is the source of truth for subscription start,
# expiry = created_on + exactly one calendar year.
# Run with DB_PATH set (the suite runner does this), like test_admin_ops.py.

import pytest

from app import db
from app.services import admin_ops


class AnchorNPM:
    """Stub NPMClient: list_proxy_hosts (id only) + get_proxy_host (created_on)."""

    def __init__(self, host_id, domain, created_on):
        self._id = host_id
        self._domain = domain
        self._created_on = created_on

    def list_proxy_hosts(self):
        return [{"id": self._id, "domain_names": [self._domain]}]

    def get_proxy_host(self, host_id):
        return {"id": host_id, "domain_names": [self._domain],
                "created_on": self._created_on}


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "t.db"))
    # point the app's connection helper at the temp file for this test
    import app.db as dbm
    real = dbm.get_conn

    def _conn(db_path=None):
        return real(db_path or str(tmp_path / "t.db"))

    monkeypatch.setattr(dbm, "get_conn", _conn)
    yield
    monkeypatch.setattr(dbm, "get_conn", real)


def _mk_account_with_instance(npm_host_id):
    acc = db.create_account("anchor@steprotech.com", "anchor", "", "", "",
                            db.new_key(prefix="h", nbytes=16))
    db.create_instance(acc, "anchor", 8, "n8n Server 2", 32770,
                       "anchor.steprotech.com", "anchor@steprotech.com", "",
                       "k" * 64, stack_id=None, container_id="c1",
                       managed=0, status="healthy")
    if npm_host_id:
        db.update_instance(db.get_active_instance(acc)["id"], npm_host_id=npm_host_id)
    return acc


def test_anchor_mid_year(monkeypatch):
    monkeypatch.setattr(admin_ops, "NPMClient",
                        lambda: AnchorNPM(55, "anchor.steprotech.com",
                                          "2026-03-14 08:24:46"))
    acc = _mk_account_with_instance(55)
    a = admin_ops.npm_subscription_anchor(acc)
    assert a is not None
    assert a["start"] == "2026-03-14"
    assert a["expiry"] == "2027-03-14"
    assert a["created_on"] == "2026-03-14 08:24:46"


def test_anchor_matches_by_domain_when_host_id_missing(monkeypatch):
    monkeypatch.setattr(admin_ops, "NPMClient",
                        lambda: AnchorNPM(9, "anchor.steprotech.com",
                                          "2026-02-05 23:48:03"))
    acc = _mk_account_with_instance(None)  # no stored npm_host_id
    a = admin_ops.npm_subscription_anchor(acc)
    assert a is not None
    assert a["start"] == "2026-02-05"
    assert a["expiry"] == "2027-02-05"


def test_anchor_leap_day_clamps(monkeypatch):
    class LeapNPM:
        def list_proxy_hosts(self):
            return [{"id": 1, "domain_names": ["anchor.steprotech.com"]}]

        def get_proxy_host(self, host_id):
            return {"id": 1, "domain_names": ["anchor.steprotech.com"],
                    "created_on": "2024-02-29 10:00:00"}

    monkeypatch.setattr(admin_ops, "NPMClient", lambda: LeapNPM())
    acc = _mk_account_with_instance(None)
    a = admin_ops.npm_subscription_anchor(acc)
    assert a["start"] == "2024-02-29"
    assert a["expiry"] == "2025-02-28"


def test_anchor_none_without_instance():
    acc = db.create_account("noinst@steprotech.com", "noinst", "", "", "",
                            db.new_key(prefix="h", nbytes=16))
    assert admin_ops.npm_subscription_anchor(acc) is None


def test_anchor_none_when_host_missing(monkeypatch):
    class EmptyNPM:
        def list_proxy_hosts(self):
            return []

    monkeypatch.setattr(admin_ops, "NPMClient", lambda: EmptyNPM())
    acc = _mk_account_with_instance(None)
    assert admin_ops.npm_subscription_anchor(acc) is None


def test_mark_paid_auto_anchors_when_dates_omitted(monkeypatch):
    monkeypatch.setattr(admin_ops, "NPMClient",
                        lambda: AnchorNPM(55, "anchor.steprotech.com",
                                          "2026-03-14 08:24:46"))
    monkeypatch.setattr(admin_ops, "_ensure_started", lambda aid: True)
    monkeypatch.setattr(admin_ops, "_ensure_stopped", lambda aid: True)
    import datetime as _dt
    import calendar
    acc = _mk_account_with_instance(55)
    out = admin_ops.mark_paid(acc, paid_until=0, paid_from=None)
    # anchored: start = 2026-03-14, expiry = 2027-03-14
    start = _dt.datetime.fromtimestamp(out["paid_from"])
    expiry = _dt.datetime.fromtimestamp(out["paid_until"])
    assert (start.year, start.month, start.day) == (2026, 3, 14)
    assert (expiry.year, expiry.month, expiry.day) == (2027, 3, 14)
    acc_row = db.get_account(acc)
    assert acc_row["subscription_status"] == "active"
    assert acc_row["paid_from"] == out["paid_from"]


def test_mark_paid_requires_expiry_without_anchor(monkeypatch):
    class EmptyNPM:
        def list_proxy_hosts(self):
            return []

    monkeypatch.setattr(admin_ops, "NPMClient", lambda: EmptyNPM())
    acc = _mk_account_with_instance(None)
    with pytest.raises(admin_ops.AdminOpsError):
        admin_ops.mark_paid(acc, paid_until=0, paid_from=None)
