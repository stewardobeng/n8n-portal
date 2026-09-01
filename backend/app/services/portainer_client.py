# Portainer API client (v2, CE).
# Auth: X-API-Key header (this Portainer version rejects Authorization: Bearer for
# access tokens — verified 2026-09-01).

import httpx
from typing import Any

from ..config import settings


class PortainerClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or settings.portainer_url).rstrip("/")
        self.token = token or settings.portainer_token

    def _headers(self) -> dict:
        return {"X-API-Key": self.token, "Accept": "application/json"}

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self.base_url}/api{path}"
        resp = httpx.request(method, url, headers=self._headers(), timeout=30.0, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Portainer {method} {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp

    # ---- environments ----
    def list_endpoints(self) -> list[dict]:
        return self._req("GET", "/endpoints").json()

    def get_endpoint(self, endpoint_id: int) -> dict:
        return self._req("GET", f"/endpoints/{endpoint_id}").json()

    # ---- containers (per environment) ----
    def list_containers(self, endpoint_id: int, all: bool = True) -> list[dict]:
        return self._req(
            "GET", f"/endpoints/{endpoint_id}/docker/containers/json",
            params={"all": "true" if all else "false"},
        ).json()

    def used_ports(self, endpoint_id: int) -> list[int]:
        """All published host ports currently bound on an environment."""
        containers = self.list_containers(endpoint_id, all=True)
        ports: set[int] = set()
        for c in containers:
            for p in c.get("Ports") or []:
                if p.get("PublicPort"):
                    ports.add(int(p["PublicPort"]))
        return sorted(ports)

    # ---- stacks ----
    def create_standalone_stack_string(
        self,
        endpoint_id: int,
        name: str,
        compose_content: str,
        env_vars: list[dict] | None = None,
    ) -> dict:
        """Create a compose stack from a raw string on a specific environment."""
        payload = {
            "name": name,
            "stackFileContent": compose_content,
            "env": env_vars or [],
        }
        return self._req(
            "POST",
            f"/stacks/create/standalone/string?endpointId={endpoint_id}",
            json=payload,
        ).json()

    def get_stack(self, stack_id: int) -> dict:
        return self._req("GET", f"/stacks/{stack_id}").json()

    def get_stack_file(self, stack_id: int) -> str:
        """Fetch a string stack's compose content. GET /stacks/{id} does NOT include
        StackFileContent (verified on Portainer 2.39.6 2026-09-01); the dedicated
        /stacks/{id}/file endpoint does."""
        data = self._req("GET", f"/stacks/{stack_id}/file").json()
        content = data.get("StackFileContent")
        if not content:
            raise RuntimeError(f"Stack {stack_id} file endpoint returned no content")
        return content

    def update_stack_env(self, stack_id: int, endpoint_id: int,
                         env_vars: list[dict], prune: bool = False) -> None:
        """Update a standalone string stack's env vars and redeploy it.
        Portainer PUT /api/stacks/{id}?endpointId=N requires env + stackFileContent
        for string stacks; fetch the content via the /file endpoint (the plain
        GET /stacks/{id} response does not carry it)."""
        payload = {
            "env": env_vars,
            "prune": prune,
            "stackFileContent": self.get_stack_file(stack_id),
        }
        self._req(
            "PUT", f"/stacks/{stack_id}?endpointId={endpoint_id}", json=payload
        )

    def delete_stack(self, stack_id: int, endpoint_id: int) -> None:
        self._req("DELETE", f"/stacks/{stack_id}?endpointId={endpoint_id}")

    def stop_stack(self, stack_id: int, endpoint_id: int) -> None:
        """Stop a stack (all its containers). 409 if already stopped is OK."""
        try:
            self._req("POST", f"/stacks/{stack_id}/stop?endpointId={endpoint_id}")
        except Exception as e:
            if "409" not in str(e) and "already" not in str(e).lower():
                raise

    def start_stack(self, stack_id: int, endpoint_id: int) -> None:
        """Start a stack. 409 if already running is OK."""
        try:
            self._req("POST", f"/stacks/{stack_id}/start?endpointId={endpoint_id}")
        except Exception as e:
            if "409" not in str(e) and "already" not in str(e).lower():
                raise

    def delete_volume(self, endpoint_id: int, volume_name: str) -> bool:
        """Best-effort named-volume removal (used after stack rollback so a retry
        starts clean). Portainer keeps stack named volumes on stack delete by
        default (verified 2026-09-01: stale gamma_n8n_data carried the old n8n
        DB into a re-provision, breaking owner setup). Returns True on 204/404."""
        try:
            resp = self._req("DELETE",
                             f"/endpoints/{endpoint_id}/docker/volumes/{volume_name}")
            return resp.status_code == 204
        except Exception:
            return False

    def list_stacks(self) -> list[dict]:
        return self._req("GET", "/stacks").json()
