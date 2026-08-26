import asyncio
from pathlib import Path
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")

DANGEROUS_GIT_OPS = [
    "reset --hard", "clean -fd", "clean -fda", "force push",
    "push --force", "push -f", "checkout -- .", "restore .",
]


class GitTool:
    name = "git"
    description = "Controlled Git operations"
    permission_level = "read"

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def execute(self, action: str, **kwargs) -> dict:
        actions = {
            "status": self._status,
            "log": self._log,
            "diff": self._diff,
            "branch": self._branch,
            "add": self._add,
            "commit": self._commit,
            "push": self._push,
            "pull": self._pull,
            "checkout": self._checkout,
        }
        handler = actions.get(action)
        if not handler:
            return {"success": False, "error": f"Unknown git action: {action}"}
        return await handler(**kwargs)

    async def _run_git(self, *args, cwd: str = None, timeout: int = 30) -> dict:
        cmd = ["git"] + list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": out[:50000],
                "stderr": err[:10000],
            }
        except FileNotFoundError:
            return {"success": False, "error": "Git not found. Ensure git.exe is in PATH."}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Git command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _status(self, cwd: str = None, **kw) -> dict:
        return await self._run_git("status", "--porcelain", cwd=cwd)

    async def _log(self, count: int = 10, cwd: str = None, **kw) -> dict:
        count = min(count, 50)
        return await self._run_git("log", f"--oneline", f"-{count}", cwd=cwd)

    async def _diff(self, cwd: str = None, **kw) -> dict:
        return await self._run_git("diff", cwd=cwd)

    async def _branch(self, cwd: str = None, **kw) -> dict:
        return await self._run_git("branch", "-a", cwd=cwd)

    async def _add(self, files: str = ".", cwd: str = None, **kw) -> dict:
        return await self._run_git("add", files, cwd=cwd, timeout=15)

    async def _commit(self, message: str = "", cwd: str = None, **kw) -> dict:
        if not message:
            return {"success": False, "error": "Commit message is required"}
        return await self._run_git("commit", "-m", message, cwd=cwd, timeout=15)

    async def _push(self, remote: str = "origin", branch: str = "", cwd: str = None, **kw) -> dict:
        args = ["push", remote]
        if branch:
            args.append(branch)
        return await self._run_git(*args, cwd=cwd, timeout=60)

    async def _pull(self, remote: str = "origin", branch: str = "", cwd: str = None, **kw) -> dict:
        args = ["pull", remote]
        if branch:
            args.append(branch)
        return await self._run_git(*args, cwd=cwd, timeout=60)

    async def _checkout(self, branch: str = "", cwd: str = None, **kw) -> dict:
        if not branch:
            return {"success": False, "error": "Branch name is required"}
        return await self._run_git("checkout", branch, cwd=cwd, timeout=15)

    def requires_approval(self, action: str, **kwargs) -> bool:
        if action == "push":
            return True
        if action == "commit":
            return True
        if action == "checkout":
            return True
        raw_args = " ".join(f"{k}={v}" for k, v in kwargs.items())
        for dangerous in DANGEROUS_GIT_OPS:
            if dangerous in raw_args:
                return True
        return False

