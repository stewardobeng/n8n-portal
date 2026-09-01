"""Dry-run the provisioner's decision logic (no resources created):
  - resolve landing environment
  - resolve forward IP exactly as provisioner does
  - compute next free port on that environment
  - build the stack env + NPM payload that WOULD be sent
Prints everything for inspection; touches nothing.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = "/tmp/portal-dryrun.db"

from app.config import settings
from app import db
from app.services.portainer_client import PortainerClient
from app.services import provisioner

db.init_db("/tmp/portal-dryrun.db")

pc = PortainerClient()

print("== landing env resolution ==")
env_id, env_name = provisioner.resolve_landing_environment(pc)
print(f"  -> env {env_id} ({env_name})")

ep = pc.get_endpoint(env_id)
forward_ip = ep.get("PublicURL") or ep.get("URL", "").replace("tcp://", "").split(":")[0]
print(f"  forward_ip (NPM will use): {forward_ip}  (PublicURL={ep.get('PublicURL')!r}, URL={ep.get('URL')!r})")

print("\n== port allocation ==")
port = provisioner.next_free_port(pc, env_id)
print(f"  next free port on env {env_id}: {port}")

print("\n== domain + env vars (what the stack will get) ==")
username = "dryrun"
domain = f"{username}.{settings.base_domain}"
env_vars = provisioner.build_stack_env(username, port, "K" * 64, "dryrun@steprotech.com", "pw123")
for e in env_vars:
    print(f"  {e['name']}={e['value'][:60]}")

print("\n== NPM payload that WOULD be sent ==")
print(json.dumps({
    "domain_names": [domain],
    "forward_scheme": "http",
    "forward_host": forward_ip,
    "forward_port": port,
    "allow_websocket_upgrade": True,
    "ssl_forced": True,
}, indent=2))
print("\nDRY_RUN_OK — nothing was created.")
