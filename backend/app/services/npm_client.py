# Nginx Proxy Manager API client.
# Auth: POST /api/tokens {identity, secret} -> Bearer JWT.

import httpx
from typing import Any

from ..config import settings


class NPMClient:
    def __init__(self, base_url: str | None = None, email: str | None = None,
                 password: str | None = None):
        self.base_url = (base_url or settings.npm_url).rstrip("/")
        self.email = email or settings.npm_email
        self.password = password or settings.npm_password
        self._token: str | None = None

    def _auth_token(self) -> str:
        if self._token:
            return self._token
        resp = httpx.post(
            f"{self.base_url}/api/tokens",
            json={"identity": self.email, "secret": self.password},
            timeout=20.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"NPM token -> {resp.status_code}: {resp.text[:300]}")
        self._token = resp.json()["token"]
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._auth_token()}", "Accept": "application/json"}

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self.base_url}/api{path}"
        resp = httpx.request(method, url, headers=self._headers(), timeout=30.0, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"NPM {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    # ---- proxy hosts ----
    def list_proxy_hosts(self) -> list[dict]:
        return self._req("GET", "/nginx/proxy-hosts").json()

    def get_proxy_host(self, host_id: int) -> dict:
        """Single proxy host detail. IMPORTANT: only this endpoint returns the
        host's creation timestamp (`created_on` 'YYYY-MM-DD HH:MM:SS'); the list
        endpoint omits it (verified 2026-09-02). Used as the subscription start
        anchor when admin marks a pre-existing workspace as paid."""
        return self._req("GET", f"/nginx/proxy-hosts/{host_id}").json()

    def create_proxy_host(self, domain: str, forward_host: str, forward_port: int,
                          certificate_id: int | None = None) -> dict:
        payload = {
            "domain_names": [domain],
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": forward_port,
            "allow_websocket_upgrade": True,
            "block_exploits": True,
            "caching_enabled": True,
            "ssl_forced": True,
            "http2_support": False,
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "locations": [],
            "advanced_config": "",
            "meta": {"nginx_online": True, "nginx_err": None},
            "access_list_id": 0,
        }
        if certificate_id is not None:
            payload["certificate_id"] = certificate_id
        return self._req("POST", "/nginx/proxy-hosts", json=payload).json()

    def update_proxy_host(self, host_id: int, **fields) -> dict:
        """Update a proxy host with a CLEAN payload (this NPM version rejects any
        read-only/extra fields copied back from GET — 'must NOT have additional
        properties', verified 2026-09-01)."""
        allowed = {
            "domain_names", "forward_scheme", "forward_host", "forward_port",
            "access_list_id", "certificate_id", "ssl_forced", "caching_enabled",
            "block_exploits", "advanced_config", "meta", "allow_websocket_upgrade",
            "http2_support", "hsts_enabled", "hsts_subdomains", "locations",
            "enabled", "trust_forwarded_proto",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        return self._req("PUT", f"/nginx/proxy-hosts/{host_id}", json=payload).json()

    def delete_proxy_host(self, host_id: int) -> None:
        self._req("DELETE", f"/nginx/proxy-hosts/{host_id}")

    def delete_certificate(self, cert_id: int) -> None:
        self._req("DELETE", f"/nginx/certificates/{cert_id}")

    # ---- certificates ----
    def request_certificate(self, domains: list[str], name: str | None = None) -> dict:
        """Request a Let's Encrypt cert via NPM's built-in provider.
        NPM 2.15.1 stores meta:{} and fills LE details itself (global settings email) —
        sending letsencrypt_email/agree/dns_challenge in meta returns 400
        'must NOT have additional properties' (verified 2026-09-01)."""
        payload = {
            "domain_names": domains,
            "meta": {},
            "provider": "letsencrypt",
        }
        if name:
            payload["nice_name"] = name
        return self._req("POST", "/nginx/certificates", json=payload).json()

    def list_certificates(self) -> list[dict]:
        return self._req("GET", "/nginx/certificates").json()

    def get_certificate(self, cert_id: int) -> dict:
        return self._req("GET", f"/nginx/certificates/{cert_id}").json()

    def update_certificate(self, cert_id: int, **fields) -> dict:
        return self._req("PUT", f"/nginx/certificates/{cert_id}", json=fields).json()
