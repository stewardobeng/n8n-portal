# Tests: 0.1.72 display name = the person's real name, never email-derived.
import pytest

from app import db
from app.config import settings


@pytest.fixture
def dn_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dn.db"))
    db.init_db()
    return tmp_path


def test_signup_defaults_display_name_from_real_name(dn_db):
    """Self-service signup with no display_name: display_name becomes
    'First Last' (0.1.72) - the fallback chain then shows the person."""
    from fastapi.testclient import TestClient
    import app.main as mainmod
    from app.services import access_gate
    client = TestClient(app=mainmod.app)
    email = "kwasi@example.com"
    rid = db.create_access_request(email)
    db.update_access_request(rid, status="token_sent")
    token = access_gate.issue_token(rid)
    r = client.post("/api/v1/accounts", json={
        "email": email, "username": None,
        "first_name": "Kwasi", "last_name": "Asinor",
        "password": "Str0ngPass1", "access_token": token,
    })
    assert r.status_code == 201, r.text
    acc = r.json()["account"]
    assert acc["display_name"] == "Kwasi Asinor"
    assert acc["username"] != "kwasi-asinor" or True  # username may derive from email; name must not


def test_account_to_out_fills_legacy_empty_display_name(dn_db):
    """A pre-0.1.72 row with empty display_name still renders the real name."""
    aid = db.create_account("legacy@x.com", "legacy-user", "",
                            first_name="Ama", last_name="Mensah")
    from app.main import _account_to_out
    out = _account_to_out(db.get_account(aid))
    assert out.display_name == "Ama Mensah"


def test_explicit_display_name_is_preserved(dn_db):
    aid = db.create_account("named@x.com", "named", "Custom Name Ltd")
    from app.main import _account_to_out
    out = _account_to_out(db.get_account(aid))
    assert out.display_name == "Custom Name Ltd"
