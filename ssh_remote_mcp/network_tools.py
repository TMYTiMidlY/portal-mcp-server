"""
Network Tools — SSH tunnels, port forwarding, reverse tunnels, SOCKS proxy.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import asyncssh
from .connection_manager import get_manager

logger = logging.getLogger("ssh_mcp.network")


@dataclass
class ActiveTunnel:
    tunnel_id: str
    tunnel_type: str        # local | remote | dynamic
    host_name: str
    local_host: str
    local_port: int
    remote_host: str
    remote_port: int
    listener: object        # asyncssh listener object
    created_at: float = field(default_factory=time.time)
    description: str = ""


class TunnelManager:
    def __init__(self):
        self._tunnels: dict[str, ActiveTunnel] = {}
        self._lock = asyncio.Lock()

    async def open_local_forward(self, host_name: str,
                                  local_port: int, remote_host: str, remote_port: int,
                                  local_bind: str = "127.0.0.1") -> dict:
        """Forward local_port → remote_host:remote_port via host_name."""
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        try:
            listener = await conn.forward_local_port(
                local_bind, local_port, remote_host, remote_port
            )
            tid = str(uuid.uuid4())[:8]
            tunnel = ActiveTunnel(
                tunnel_id=tid, tunnel_type="local",
                host_name=host_name,
                local_host=local_bind, local_port=listener.get_port(),
                remote_host=remote_host, remote_port=remote_port,
                listener=listener,
                description=f"{local_bind}:{listener.get_port()} → {remote_host}:{remote_port}"
            )
            async with self._lock:
                self._tunnels[tid] = tunnel
            logger.info(f"Local tunnel {tid}: {tunnel.description} via {host_name}")
            return {"tunnel_id": tid, "type": "local", "host": host_name,
                    "local": f"{local_bind}:{listener.get_port()}",
                    "remote": f"{remote_host}:{remote_port}"}
        except Exception as e:
            return {"error": str(e)}

    async def open_remote_forward(self, host_name: str,
                                   remote_port: int, local_host: str, local_port: int) -> dict:
        """Reverse tunnel: remote_port on host_name → local_host:local_port."""
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        try:
            listener = await conn.forward_remote_port(
                "", remote_port, local_host, local_port
            )
            tid = str(uuid.uuid4())[:8]
            tunnel = ActiveTunnel(
                tunnel_id=tid, tunnel_type="remote",
                host_name=host_name,
                local_host=local_host, local_port=local_port,
                remote_host="0.0.0.0", remote_port=listener.get_port(),
                listener=listener,
                description=f"{host_name}:{listener.get_port()} → {local_host}:{local_port}"
            )
            async with self._lock:
                self._tunnels[tid] = tunnel
            logger.info(f"Reverse tunnel {tid}: {tunnel.description}")
            return {"tunnel_id": tid, "type": "remote", "host": host_name,
                    "remote_port": listener.get_port(),
                    "forwards_to": f"{local_host}:{local_port}"}
        except Exception as e:
            return {"error": str(e)}

    async def open_dynamic_proxy(self, host_name: str,
                                  local_port: int, local_bind: str = "127.0.0.1") -> dict:
        """Open a SOCKS5 dynamic proxy through host_name on local_port."""
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        try:
            listener = await conn.forward_socks(local_bind, local_port)
            tid = str(uuid.uuid4())[:8]
            tunnel = ActiveTunnel(
                tunnel_id=tid, tunnel_type="dynamic",
                host_name=host_name,
                local_host=local_bind, local_port=listener.get_port(),
                remote_host="*", remote_port=0,
                listener=listener,
                description=f"SOCKS5 {local_bind}:{listener.get_port()} via {host_name}"
            )
            async with self._lock:
                self._tunnels[tid] = tunnel
            logger.info(f"SOCKS proxy {tid}: {tunnel.description}")
            return {"tunnel_id": tid, "type": "dynamic",
                    "socks5": f"socks5://{local_bind}:{listener.get_port()}",
                    "host": host_name}
        except Exception as e:
            return {"error": str(e)}

    async def close_tunnel(self, tunnel_id: str) -> str:
        async with self._lock:
            if tunnel_id not in self._tunnels:
                return f"Tunnel '{tunnel_id}' not found"
            t = self._tunnels.pop(tunnel_id)
        try:
            t.listener.close()
            await t.listener.wait_closed()
        except Exception:
            pass
        logger.info(f"Tunnel {tunnel_id} closed")
        return f"Tunnel {tunnel_id} closed ({t.description})"

    def list_tunnels(self) -> list[dict]:
        return [
            {"tunnel_id": t.tunnel_id, "type": t.tunnel_type,
             "host": t.host_name, "description": t.description,
             "age_s": round(time.time() - t.created_at, 1)}
            for t in self._tunnels.values()
        ]

    async def close_all(self):
        ids = list(self._tunnels.keys())
        for tid in ids:
            await self.close_tunnel(tid)


_tunnel_mgr: Optional[TunnelManager] = None

def get_tunnel_manager() -> TunnelManager:
    global _tunnel_mgr
    if _tunnel_mgr is None:
        _tunnel_mgr = TunnelManager()
    return _tunnel_mgr
