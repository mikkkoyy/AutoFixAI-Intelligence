import asyncio
import platform
import json
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


class SystemTool:
    name = "system"
    description = "Safe system information"
    permission_level = "read"

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def execute(self, action: str = "info", **kwargs) -> dict:
        if action == "info":
            return await self._system_info()
        return {"success": False, "error": f"Unknown system action: {action}"}

    async def _system_info(self) -> dict:
        try:
            info = {
                "os": platform.system(),
                "os_version": platform.version(),
                "platform": platform.platform(),
                "architecture": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
                "python_version": platform.python_version(),
            }

            ps_script = (
                "$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
                "$ram = Get-CimInstance Win32_OperatingSystem; "
                "$totalRam = [math]::Round($ram.TotalVisibleMemorySize / 1MB, 2); "
                "$freeRam = [math]::Round($ram.FreePhysicalMemory / 1MB, 2); "
                "$disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID,"
                "@{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}},"
                "@{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,2)}}; "
                "$obj = @{cpu_percent=$cpu;ram_total_gb=$totalRam;ram_free_gb=$freeRam;"
                "disks=($disks | ConvertTo-Json -Compress)}; "
                "$obj | ConvertTo-Json -Compress"
            )

            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            try:
                sys_data = json.loads(stdout.decode("utf-8", errors="replace"))
                info["cpu_percent"] = sys_data.get("cpu_percent")
                info["ram_total_gb"] = sys_data.get("ram_total_gb")
                info["ram_free_gb"] = sys_data.get("ram_free_gb")
                info["disks"] = sys_data.get("disks", [])
            except (json.JSONDecodeError, ValueError):
                pass

            try:
                proc2 = await asyncio.create_subprocess_exec(
                    "git", "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
                info["git_version"] = out2.decode("utf-8", errors="replace").strip()
            except Exception:
                info["git_version"] = "not found"

            info["current_user"] = "unknown"
            try:
                proc3 = await asyncio.create_subprocess_exec(
                    "whoami",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out3, _ = await asyncio.wait_for(proc3.communicate(), timeout=5)
                info["current_user"] = out3.decode("utf-8", errors="replace").strip()
            except Exception:
                pass

            return {"success": True, "result": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

