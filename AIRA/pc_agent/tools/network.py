import asyncio
import json
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


class NetworkTool:
    name = "network"
    description = "Safe network diagnostics"
    permission_level = "read"

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def execute(self, action: str = "info", **kwargs) -> dict:
        actions = {
            "info": self._network_info,
            "ping": self._ping,
            "dns_lookup": self._dns_lookup,
            "connections": self._connections,
        }
        handler = actions.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown network action: {action}"}
        return await handler(**kwargs)

    async def _network_info(self) -> dict:
        try:
            ps = (
                "Get-NetAdapter | Where-Object Status -eq 'Up' | "
                "Select-Object Name,InterfaceDescription,LinkSpeed,MacAddress | "
                "ConvertTo-Json -Compress"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            try:
                adapters = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(adapters, dict):
                    adapters = [adapters]
            except json.JSONDecodeError:
                adapters = []

            ps2 = (
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -ne '127.0.0.1' | "
                "Select-Object InterfaceAlias,IPAddress,PrefixLength | "
                "ConvertTo-Json -Compress"
            )
            proc2 = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps2,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=10)
            try:
                addresses = json.loads(stdout2.decode("utf-8", errors="replace"))
                if isinstance(addresses, dict):
                    addresses = [addresses]
            except json.JSONDecodeError:
                addresses = []

            return {
                "success": True,
                "result": {
                    "adapters": adapters[:20],
                    "ip_addresses": addresses[:20],
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _ping(self, target: str = "8.8.8.8", count: int = 4, **kw) -> dict:
        if not target or len(target) > 253:
            return {"success": False, "error": "Invalid target"}
        import re
        if re.search(r'[;&|`$]', target):
            return {"success": False, "error": "Invalid characters in target"}
        try:
            cmd = f"Test-Connection -ComputerName '{target}' -Count {min(count, 10)} -ErrorAction Stop | Select-Object Address,ResponseTime | ConvertTo-Json -Compress"
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return {"success": False, "error": f"Ping failed: {err[:200]}"}
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    data = [data]
                return {"success": True, "result": data}
            except json.JSONDecodeError:
                return {"success": True, "result": "Ping completed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _dns_lookup(self, hostname: str = "", **kw) -> dict:
        if not hostname or len(hostname) > 253:
            return {"success": False, "error": "Invalid hostname"}
        import re
        if re.search(r'[;&|`$]', hostname):
            return {"success": False, "error": "Invalid characters in hostname"}
        try:
            cmd = f"Resolve-DnsName -Name '{hostname}' -ErrorAction Stop | Select-Object Name,Type,IPAddress,TTL | ConvertTo-Json -Compress"
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return {"success": False, "error": f"DNS lookup failed: {err[:200]}"}
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    data = [data]
                return {"success": True, "result": data}
            except json.JSONDecodeError:
                return {"success": True, "result": "DNS lookup completed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _connections(self, **kw) -> dict:
        try:
            ps = (
                "Get-NetTCPConnection -State Established | "
                "Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess | "
                "Sort-Object LocalPort | Select-Object -First 50 | ConvertTo-Json -Compress"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    data = [data]
                return {"success": True, "result": data}
            except json.JSONDecodeError:
                return {"success": True, "result": []}
        except Exception as e:
            return {"success": False, "error": str(e)}


