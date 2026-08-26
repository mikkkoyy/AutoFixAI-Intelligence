import asyncio
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")

CRITICAL_PROCESSES = {
    "system", "smss", "csrss", "wininit", "winlogon", "services",
    "lsass", "svchost", "spoolsv", "dwm", "taskhostw", "sihost",
    "explorer", "searchindexer", "searchprotocolhost", "searchfilterhost",
}


class ProcessTool:
    name = "process"
    description = "Process management"
    permission_level = "read"

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def execute(self, action: str, **kwargs) -> dict:
        actions = {
            "list": self._list,
            "get": self._get,
            "stop": self._stop,
        }
        handler = actions.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown process action: {action}"}
        return await handler(**kwargs)

    async def _list(self, filter_name: str = "", **kw) -> dict:
        try:
            cmd = "Get-Process | Select-Object Name,Id,CPU,WorkingSet64 | ConvertTo-Json -Compress"
            if filter_name:
                cmd = f"Get-Process -Name '*{filter_name}*' -ErrorAction SilentlyContinue | Select-Object Name,Id,CPU,WorkingSet64 | ConvertTo-Json -Compress"
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            import json
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    data = [data]
                return {"success": True, "result": data[:200]}
            except json.JSONDecodeError:
                return {"success": True, "result": []}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _get(self, pid: int = 0, name: str = "", **kw) -> dict:
        try:
            if pid:
                cmd = f"Get-Process -Id {pid} | Select-Object Name,Id,CPU,WorkingSet64,StartTime | ConvertTo-Json -Compress"
            elif name:
                cmd = f"Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Select-Object Name,Id,CPU,WorkingSet64,StartTime | ConvertTo-Json -Compress"
            else:
                return {"success": False, "error": "Provide pid or name"}
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            import json
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                return {"success": True, "result": data}
            except json.JSONDecodeError:
                return {"success": False, "error": "Process not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _stop(self, pid: int = 0, name: str = "", **kw) -> dict:
        target = name.lower() if name else ""
        if target in CRITICAL_PROCESSES:
            return {"success": False, "error": f"Cannot stop critical system process: {name}"}
        try:
            if pid:
                cmd = f"Stop-Process -Id {pid} -Force -ErrorAction Stop"
            elif name:
                cmd = f"Stop-Process -Name '{name}' -Force -ErrorAction Stop"
            else:
                return {"success": False, "error": "Provide pid or name"}
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return {"success": False, "error": err or "Failed to stop process"}
            target_desc = f"PID {pid}" if pid else name
            logger.info(f"Stopped process: {target_desc}")
            return {"success": True, "result": f"Stopped process: {target_desc}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


