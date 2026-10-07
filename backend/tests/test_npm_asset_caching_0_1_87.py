"""Portal-created proxy hosts must not enable NPM asset caching (0.1.87).

Found live on 2026-10-07, on the first GHS 500 workspace (omotade-duodu). While the
new n8n container was still booting, the customer's browser requested the app's assets
and nginx cached 'connection refused' as a 502 against those exact URLs. NPM's
asset-caching location keeps ANY status for 30 minutes, so the browser went on loading
the HTML but none of its JS: a blank page that stayed blank after the container was
healthy, and whose entries live in nginx's shared-memory zone (a reload cannot clear
them). With caching off, the same early request is simply re-tried by the browser.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/portal-test-npm-caching.db")

from app.services.npm_client import NPMClient  # noqa: E402


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _capture(monkeypatch, client, captured):
    def fake_req(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kw.get("json")
        return _Resp(dict(kw.get("json") or {}, id=4242))

    monkeypatch.setattr(client, "_req", fake_req)


def test_a_new_workspace_host_is_created_without_asset_caching(monkeypatch):
    client = NPMClient()
    captured = {}
    _capture(monkeypatch, client, captured)

    client.create_proxy_host("sandbox.steprotech.com", "10.0.0.9", 32790, certificate_id=7)
    sent = captured["json"]

    assert captured["method"] == "POST" and captured["path"] == "/nginx/proxy-hosts"
    assert sent["caching_enabled"] is False, "caching a startup 502 is what makes a blank page"
    # and the rest of the host definition is untouched
    assert sent["domain_names"] == ["sandbox.steprotech.com"]
    assert sent["forward_host"] == "10.0.0.9" and sent["forward_port"] == 32790
    assert sent["allow_websocket_upgrade"] is True
    assert sent["block_exploits"] is True
    assert sent["ssl_forced"] is True
    assert sent["certificate_id"] == 7


def test_an_host_update_carries_the_caching_value_it_is_given(monkeypatch):
    """The repair path must not quietly re-enable caching on a host that had it turned
    off for a customer (provisioner passes the host's own value through)."""
    client = NPMClient()
    captured = {}
    _capture(monkeypatch, client, captured)

    client.update_proxy_host(72, domain_names=["omotade-duodu.steprotech.com"],
                             forward_scheme="http", forward_host="150.136.254.19",
                             forward_port=32769, caching_enabled=False, ssl_forced=True)
    assert captured["json"]["caching_enabled"] is False
    assert captured["json"]["forward_port"] == 32769
    # a read-only field copied from a GET must not leak into the payload
    assert "created_on" not in captured["json"] and "id" not in captured["json"]
