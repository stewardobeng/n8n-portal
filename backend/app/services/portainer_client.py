# Portainer API client (v2, CE).
# Auth: X-API-Key header (this Portainer version rejects Authorization: Bearer for
# access tokens — verified 2026-09-01).

import httpx
from typing import Any

from ..config import settings


def _strip_docker_stream(data: bytes) -> bytes:
    """Docker's exec/attach multiplexes streams into 8-byte frames
    (header: [stream_type, 0,0,0,4-byte-be-len]). Return the de-framed stdout."""
    if not data:
        return b""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 8 > len(data):
            out += data[i:]
            break
        # 4th..7th bytes are the frame length (big-endian)
        flen = int.from_bytes(data[i + 4:i + 8], "big")
        out += data[i + 8:i + 8 + flen]
        i += 8 + flen
    return bytes(out)


class PortainerClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or settings.portainer_url).rstrip("/")
        self.token = token or settings.portainer_token

    def _headers(self) -> dict:
        return {"X-API-Key": self.token, "Accept": "application/json"}

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self.base_url}/api{path}"
        headers = dict(self._headers())
        headers.update(kw.pop("headers", {}) or {})
        timeout = kw.pop("timeout", 30.0)
        resp = httpx.request(method, url, headers=headers, timeout=timeout, **kw)
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

    def get_container(self, endpoint_id: int, container_id: str) -> dict:
        """Full container inspect (env vars, labels) on an environment."""
        return self._req(
            "GET", f"/endpoints/{endpoint_id}/docker/containers/{container_id}/json"
        ).json()

    def get_container_archive(self, endpoint_id: int, container_id: str, path: str) -> bytes:
        """Fetch a path inside a container as a raw tar stream (docker proxy
        GET .../containers/{id}/archive?path=...). Used for full workspace
        backups (the whole ~/.n8n data dir). Not an error on missing files."""
        resp = self._req(
            "GET", f"/endpoints/{endpoint_id}/docker/containers/{container_id}/archive",
            params={"path": path},
            headers={"Accept": "*/*"},
        )
        return resp.content

    def exec_in_container(self, endpoint_id: int, container_id: str, cmd: list[str],
                          timeout: float = 60.0) -> bytes:
        """Run a command inside a container via the docker proxy and return its
        stdout. Create the exec then start with an EMPTY body (see the skill
        note: newer Docker API rejects a body on start). Docker multiplexes stdout
        with an 8-byte header per frame, so strip those before returning."""
        r = self._req("POST", f"/endpoints/{endpoint_id}/docker/containers/{container_id}/exec",
                      json={"AttachStdout": True, "AttachStderr": True, "Cmd": cmd, "Tty": False})
        exec_id = r.json()["Id"]
        sr = self._req("POST", f"/endpoints/{endpoint_id}/docker/exec/{exec_id}/start",
                       content=b"{}", headers={"Content-Type": "application/json"}, timeout=timeout)
        return _strip_docker_stream(sr.content)

    def update_stack_image(self, stack_id: int, endpoint_id: int, image: str) -> None:
        """Update an n8n stack's image and redeploy it. The stack file content
        carries `image: n8nio/n8n:<tag>`; swap that tag and PUT the stack (which
        recreates the container against the same volume). Preserve stack env.
        `image` may be a bare tag ('2.31.6') or a full 'n8nio/n8n:2.31.6'."""
        import re
        # Normalize to the exact registry image we expect in the compose.
        tag = image.split("/")[-1]
        if ":" in tag:
            tag = tag.split(":")[-1]
        full = f"n8nio/n8n:{tag}" if tag != "latest" else "n8nio/n8n:latest"
        content = self.get_stack_file(stack_id)
        new_content = re.sub(r"(image:\s*n8nio/n8n:)[^\s]+", r"\g<1>" + tag, content)
        if new_content == content:
            raise RuntimeError(f"Image '{image}' not found in stack {stack_id} compose.")
        detail = self.get_stack(stack_id)
        env = detail.get("Env") or []
        payload = {"env": env, "prune": False, "stackFileContent": new_content}
        self._req("PUT", f"/stacks/{stack_id}?endpointId={endpoint_id}", json=payload)
        return full

    def stop_container(self, endpoint_id: int, container_id: str) -> None:
        """Stop a single container on an environment (fallback when a stack has
        no valid Portainer stack record — e.g. compose stacks created directly on
        the agent whose Portainer record is stale or missing)."""
        try:
            self._req("POST",
                      f"/endpoints/{endpoint_id}/docker/containers/{container_id}/stop")
        except Exception as e:
            if "304" not in str(e) and "already" not in str(e).lower():
                raise

    def start_container(self, endpoint_id: int, container_id: str) -> None:
        # Portainer's docker proxy forwards a non-empty body on plain POSTs,
        # which Docker >=1.24 rejects for container start ("starting container
        # with non-empty request body... removed in v1.24" — verified live
        # 2026-09-02). Send an explicit empty JSON body + content-type; returns
        # 204 and the container starts.
        try:
            self._req("POST",
                      f"/endpoints/{endpoint_id}/docker/containers/{container_id}/start",
                      content=b"{}",
                      headers={"Content-Type": "application/json"})
        except Exception as e:
            if "304" not in str(e) and "already" not in str(e).lower():
                raise

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
