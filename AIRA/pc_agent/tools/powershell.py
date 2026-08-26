import asyncio
from AIRA.core.logging import get_logger
from AIRA.pc_agent.command_validator import command_validator

logger = get_logger("pc_agent")


class PowershellTool:
    name = "powershell"
    description = "Controlled PowerShell command execution"
    permission_level = "execute"

    def __init__(self, config: dict = None):
        config = config or {}
        self.timeout = config.get("timeout", 30)
        self.shell = config.get("shell", "powershell")

    async def execute(self, command: str = "", timeout: int = None, **kwargs) -> dict:
        timeout = timeout or self.timeout

        is_valid, reason = command_validator.validate_powershell(command)
        if not is_valid:
            return {"success": False, "error": f"Command blocked: {reason}"}

        try:
            cmd_parts = [self.shell, "-NoProfile", "-NonInteractive", "-Command", command]
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return {"success": False, "error": f"Command timed out after {timeout}s"}

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            result = {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": stdout[:50000] if stdout else "",
                "stderr": stderr[:10000] if stderr else "",
            }

            logger.info(f"PowerShell executed: {command[:100]} (rc={proc.returncode}, {timeout}s)")
            return result

        except FileNotFoundError:
            return {"success": False, "error": "PowerShell not found. Ensure 'powershell' is in PATH."}
        except Exception as e:
            return {"success": False, "error": f"Execution error: {str(e)}"}
