import subprocess
import asyncio
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")

DEFAULT_ALLOWED = [
    "code.exe", "notepad.exe", "explorer.exe", "cmd.exe",
    "powershell.exe", "python.exe", "python3.exe", "pip.exe",
    "git.exe", "node.exe", "npm.exe", "npx.exe",
    "devenv.exe", "python3.exe", "code-insiders.exe",
]


class ApplicationTool:
    name = "application"
    description = "Controlled application management"
    permission_level = "execute"

    def __init__(self, config: dict = None):
        config = config or {}
        allowed = config.get("allowed", [])
        self.allowed_apps = set(
            a.lower() for a in (allowed if allowed else DEFAULT_ALLOWED)
        )

    def _is_allowed(self, app: str) -> bool:
        app_lower = app.lower()
        return app_lower in self.allowed_apps or app_lower.endswith(".exe") and app_lower in self.allowed_apps

    async def execute(self, action: str, **kwargs) -> dict:
        actions = {
            "open": self._open,
            "list_running": self._list_running,
            "close": self._close,
        }
        handler = actions.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown application action: {action}"}
        return await handler(**kwargs)

    async def _open(self, application: str = "", args: list = None, **kw) -> dict:
        if not self._is_allowed(application):
            return {"success": False, "error": f"Application not in allowlist: {application}"}
        try:
            cmd = [application]
            if args:
                cmd.extend(args)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"Opened application: {application} (PID={proc.pid})")
            return {"success": True, "result": f"Launched {application} (PID={proc.pid})"}
        except FileNotFoundError:
            return {"success": False, "error": f"Executable not found: {application}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _list_running(self, **kw) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                "Get-Process | Select-Object Name,Id,MainWindowTitle | ConvertTo-Json -Compress",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            import json
            try:
                data = json.loads(stdout.decode("utf-8", errors="replace"))
                if isinstance(data, dict):
                    data = [data]
                return {"success": True, "result": data[:100]}
            except json.JSONDecodeError:
                return {"success": True, "result": []}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _close(self, application: str = "", **kw) -> dict:
        if not self._is_allowed(application):
            return {"success": False, "error": f"Application not in allowlist: {application}"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                f"Stop-Process -Name '{application.replace('.exe', '')}' -Force -ErrorAction SilentlyContinue",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            logger.info(f"Closed application: {application}")
            return {"success": True, "result": f"Closed {application}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
