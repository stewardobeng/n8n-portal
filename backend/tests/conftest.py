# Shared test setup: the hardened auth endpoints now require a signed JWT, so
# every test run needs a JWT_SECRET configured on the settings singleton
# (the app reads app.config.settings at request time). Individual tests may
# override it (with try/finally) but this guarantees a sane default.
#
# Also disable the in-memory rate limiter + auto-ban for the whole suite: all
# TestClient requests share one client IP ("testclient"), so an IP-scope bucket
# (register 5/hr, login 10/min) would exhaust across unrelated tests. The rate
# limiter + IP-ban logic is covered directly by test_security_hardening.py which
# toggles it back on.

import os
import pytest

from app.config import settings
from app.services import security_controls as sc

settings.jwt_secret = os.getenv("TEST_JWT_SECRET", "test-secret-for-uitests")


@pytest.fixture(autouse=True)
def _disable_rate_limiter_for_tests():
    sc.RATE_LIMIT_ENABLED = False
    sc._RATE.clear()
    yield
    sc.RATE_LIMIT_ENABLED = True
    sc._RATE.clear()
