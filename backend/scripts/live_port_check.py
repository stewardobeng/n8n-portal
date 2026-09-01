"""Live validation of the fixed port allocator against env 4 (18 stopped tenants).
Reads real Portainer + NPM data; allocates nothing."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = "/tmp/portal-livecheck.db"

from app.config import settings
from app import db
from app.services.portainer_client import PortainerClient
from app.services.npm_client import NPMClient
from app.services import provisioner

db.init_db("/tmp/portal-livecheck.db")

pc = PortainerClient()
npm = NPMClient()

for env_id in (4, 8, 9):
    ep = pc.get_endpoint(env_id)
    forward_ip = ep.get("PublicURL") or ep.get("URL", "").replace("tcp://", "").split(":")[0]
    used = provisioner.used_ports_all_sources(pc, npm, env_id, forward_ip)
    docker_only = [p for p in pc.used_ports(env_id)
                   if settings.port_range_start <= p <= settings.port_range_end]
    free = provisioner.next_free_port(pc, npm, env_id, forward_ip)
    print(f"env {env_id} ({ep.get('Name')}) ip={forward_ip}")
    print(f"  docker-only ports : {docker_only}")
    print(f"  ALL sources       : {used}")
    print(f"  next free port    : {free}  <- allocator would use this")
    print()

print("LIVE_CHECK_OK")
