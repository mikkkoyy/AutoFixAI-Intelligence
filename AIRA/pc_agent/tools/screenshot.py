import asyncio
import tempfile
from pathlib import Path
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


class ScreenshotTool:
    name = "screenshot"
    description = "Desktop screenshot capture"
    permission_level = "read"

    def __init__(self, config: dict = None):
        config = config or {}
        self.temp_dir = Path(config.get("temp_dir", tempfile.gettempdir())) / "aira_screenshots"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def execute(self, action: str = "capture", **kwargs) -> dict:
        if action == "capture":
            return await self._capture(**kwargs)
        return {"success": False, "error": f"Unknown screenshot action: {action}"}

    async def _capture(self, **kw) -> dict:
        try:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height); "
                "$graphics = [System.Drawing.Graphics]::FromImage($bitmap); "
                "$graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size); "
                f"$path = '{self.temp_dir}\\screenshot_$(Get-Date -Format 'yyyyMMdd_HHmmss').png'; "
                "$bitmap.Save($path); "
                "$graphics.Dispose(); $bitmap.Dispose(); "
                "$path"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            path = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0 or not path:
                err = stderr.decode("utf-8", errors="replace").strip()
                return {"success": False, "error": f"Screenshot failed: {err[:200]}"}

            p = Path(path)
            if p.exists():
                return {
                    "success": True,
                    "result": {
                        "path": str(p),
                        "size_bytes": p.stat().st_size,
                        "warning": "Screenshot may contain sensitive information on screen.",
                    },
                }
            return {"success": False, "error": "Screenshot file not created"}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def cleanup(self):
        try:
            for f in self.temp_dir.glob("screenshot_*.png"):
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass
