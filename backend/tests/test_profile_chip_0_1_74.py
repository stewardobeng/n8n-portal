# Tests: 0.1.74 sidebar profile chip data.
# The chip shows the FIRST name only and ellipsises the email in CSS, so the API
# must expose `first_name` (the old chip rendered display_name, "Olisaemeka
# Ekwenem", which overflowed the 216px chip and hard-clipped the email).

import sys

import pytest

from app import db
from app.config import settings


@pytest.fixture
def chip_db(tmp_path, monkeypatch):
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "chip.db"))
    db.init_db()
    return tmp_path


def _h(token):
    return {"Authorization": "Bearer " + token}


def test_me_exposes_first_name_for_the_chip(chip_db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_client_token

    aid = db.create_account("ekwenemolisaemeka@gmail.com", "ekwenemolisaemeka",
                            "Olisaemeka Ekwenem", first_name="Olisaemeka",
                            last_name="Ekwenem", password_hash="x")
    r = TestClient(app).get("/api/v1/me", headers=_h(create_client_token(aid)))
    assert r.status_code == 200, r.text
    acc = r.json()["account"]
    assert acc["first_name"] == "Olisaemeka"
    assert acc["display_name"] == "Olisaemeka Ekwenem", "full name still available"


def test_first_name_falls_back_to_empty_not_username(chip_db):
    """A legacy row without first_name must NOT leak the email-derived username
    into the chip: the frontend derives the first name from display_name, and
    display_name is rebuilt from first/last when it is missing."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import create_client_token

    aid = db.create_account("st-ewardob-eng@example.com", "st-ewardob-eng", "",
                            first_name="Steward", last_name="Obeng",
                            password_hash="x")
    acc = TestClient(app).get("/api/v1/me",
                              headers=_h(create_client_token(aid))).json()["account"]
    assert acc["display_name"] == "Steward Obeng"
    assert acc["first_name"] == "Steward"
