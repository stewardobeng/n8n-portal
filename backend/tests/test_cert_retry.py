# Regression test for the transient Let's Encrypt cert failure fix.
# LE's ACME API can return transient errors at order-creation time ("No such
# authorization") even when DNS + port-80 are fine. The provisioner now retries
# the NPM certificate request before rolling back, so a transient ACME blip
# no longer destroys a healthy provision.
import time

import pytest

from app.services.provisioner import _CERT_RETRIES, _request_cert_with_retry


class FlakyNPM:
    """Mimics NPMClient.request_certificate: fails the first N times, then
    succeeds. Records how many attempts were made."""

    def __init__(self, fail_first_n: int):
        self.fail_first_n = fail_first_n
        self.calls = 0

    def request_certificate(self, domains, name=None):
        self.calls += 1
        if self.calls <= self.fail_first_n:
            raise RuntimeError(
                "NPM POST /nginx/certificates -> 500: "
                '{"error":{"code":500,"message":"Internal Error"},'
                '"debug":{"stack":["No such authorization"]}}'
            )
        return {"id": 999, "domain_names": domains}


def test_retry_recovers_after_transient_failures(monkeypatch):
    # fail twice, then succeed -> should return a cert id without raising
    npm = FlakyNPM(fail_first_n=2)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # no real backoff sleep
    cert = _request_cert_with_retry(npm, ["space.steprotech.com"], name="space")
    assert cert["id"] == 999
    assert npm.calls == 3


def test_retry_gives_up_after_max_attempts(monkeypatch):
    # always fail -> should raise after ALL retries, not roll back silently
    npm = FlakyNPM(fail_first_n=999)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(Exception) as ei:
        _request_cert_with_retry(npm, ["space.steprotech.com"], name="space")
    assert npm.calls == _CERT_RETRIES
    assert "5 attempts" in str(ei.value)


def test_retry_returns_first_success_immediately(monkeypatch):
    # succeeds first try -> exactly one call, no backoff
    npm = FlakyNPM(fail_first_n=0)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    cert = _request_cert_with_retry(npm, ["a.steprotech.com"], name="a")
    assert cert["id"] == 999
    assert npm.calls == 1
