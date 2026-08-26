import os
import json
from pathlib import Path
from typing import Any, Optional
from enum import Enum
from AIRA.core.models import PermissionLevel, generate_id, timestamp_now
from AIRA.core.logging import get_logger

logger = get_logger("tools")


class ToolResult:
    def __init__(self, success: bool, output: Any = None, error: str = None):
        self.success = success
        self.output = output
        self.error = error

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }


class BaseTool:
    name: str = "base"
    description: str = ""
    permission: PermissionLevel = PermissionLevel.READ

    def __init__(self):
        self.log = get_logger("tools")

    async def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def validate(self, **kwargs) -> bool:
        return True


class FileReadTool(BaseTool):
    name = "file_read"
    description = "Read file contents"
    permission = PermissionLevel.READ

    async def execute(self, path: str, encoding: str = "utf-8") -> ToolResult:
        try:
            p = Path(path)
            if not p.exists():
                return ToolResult(False, error=f"File not found: {path}")
            if p.stat().st_size > 10 * 1024 * 1024:
                return ToolResult(False, error="File too large (>10MB)")
            content = p.read_text(encoding=encoding)
            self.log.info(f"Read file: {path} ({len(content)} chars)")
            return ToolResult(True, content)
        except Exception as e:
            self.log.error(f"File read error: {e}")
            return ToolResult(False, error=str(e))


class FileWriteTool(BaseTool):
    name = "file_write"
    description = "Write content to a file"
    permission = PermissionLevel.WRITE

    PROTECTED_PATHS = [".env", "secrets", "config/secrets"]

    def validate(self, path: str = "", **kwargs) -> bool:
        for pp in self.PROTECTED_PATHS:
            if pp in path.lower():
                self.log.warning(f"Blocked write to protected path: {path}")
                return False
        return True

    async def execute(self, path: str, content: str, encoding: str = "utf-8") -> ToolResult:
        if not self.validate(path=path):
            return ToolResult(False, error="Access denied: protected file")
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding=encoding)
            self.log.info(f"Wrote file: {path} ({len(content)} chars)")
            return ToolResult(True, f"Written {len(content)} chars to {path}")
        except Exception as e:
            self.log.error(f"File write error: {e}")
            return ToolResult(False, error=str(e))


class FileListTool(BaseTool):
    name = "file_list"
    description = "List files in a directory"
    permission = PermissionLevel.READ

    async def execute(self, path: str = ".", pattern: str = "*") -> ToolResult:
        try:
            p = Path(path)
            if not p.exists():
                return ToolResult(False, error=f"Directory not found: {path}")
            files = []
            for item in p.glob(pattern):
                files.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                })
            return ToolResult(True, files)
        except Exception as e:
            return ToolResult(False, error=str(e))


class TerminalTool(BaseTool):
    name = "terminal"
    description = "Execute a terminal command"
    permission = PermissionLevel.EXECUTE

    BLOCKED_COMMANDS = [
        "rm -rf", "format", "del /s", "rmdir /s",
        "rd /s", "cipher", "takeown",
    ]

    def validate(self, command: str = "", **kwargs) -> bool:
        cmd_lower = command.lower().strip()
        for blocked in self.BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                self.log.warning(f"Blocked dangerous command: {command}")
                return False
        return True

    async def execute(self, command: str, timeout: int = 30) -> ToolResult:
        if not self.validate(command=command):
            return ToolResult(False, error="Command blocked for safety")
        try:
            import asyncio
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            self.log.info(f"Executed: {command[:100]} (rc={proc.returncode})")
            if proc.returncode != 0:
                return ToolResult(False, output=output or None, error=error or f"Exit code {proc.returncode}")
            return ToolResult(True, output=output or None)
        except asyncio.TimeoutError:
            return ToolResult(False, error=f"Command timed out after {timeout}s")
        except Exception as e:
            self.log.error(f"Terminal error: {e}")
            return ToolResult(False, error=str(e))


class GitTool(BaseTool):
    name = "git"
    description = "Execute git commands"
    permission = PermissionLevel.WRITE

    async def execute(self, command: str, cwd: str = None) -> ToolResult:
        try:
            import asyncio
            full_cmd = f"git {command}"
            proc = await asyncio.create_subprocess_shell(
                full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            self.log.info(f"Git: {command[:100]} (rc={proc.returncode})")
            return ToolResult(
                proc.returncode == 0,
                output=output or None,
                error=error if proc.returncode != 0 else None,
            )
        except Exception as e:
            return ToolResult(False, error=str(e))


class MemoryTool(BaseTool):
    name = "memory"
    description = "Search and manage AIRA memory"
    permission = PermissionLevel.WRITE

    async def execute(self, action: str, **kwargs) -> ToolResult:
        from AIRA.core.memory_db import memory_db
        try:
            if action == "search":
                results = await memory_db.search_long_term(
                    query=kwargs.get("query"),
                    category=kwargs.get("category"),
                    limit=kwargs.get("limit", 10),
                )
                return ToolResult(True, results)
            elif action == "search_knowledge":
                results = await memory_db.search_knowledge(
                    query=kwargs.get("query"),
                    category=kwargs.get("category"),
                    limit=kwargs.get("limit", 10),
                )
                return ToolResult(True, results)
            elif action == "stats":
                stats = await memory_db.get_stats()
                return ToolResult(True, stats)
            elif action == "store":
                result = await memory_db.store_long_term(
                    key=kwargs["key"],
                    category=kwargs.get("category", "general"),
                    content=kwargs["content"],
                    importance=kwargs.get("importance", 0.5),
                )
                return ToolResult(True, result)
            elif action == "store_knowledge":
                result = await memory_db.store_knowledge(
                    title=kwargs["title"],
                    category=kwargs.get("category", "general"),
                    solution=kwargs["solution"],
                    problem=kwargs.get("problem"),
                    tags=kwargs.get("tags", []),
                )
                return ToolResult(True, result)
            else:
                return ToolResult(False, error=f"Unknown action: {action}")
        except Exception as e:
            return ToolResult(False, error=str(e))


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self.tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get(self, name: str) -> Optional[BaseTool]:
        return self.tools.get(name)

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "permission": t.permission.value,
            }
            for t in self.tools.values()
        ]

    def get_tools_for_permission(self, level: PermissionLevel) -> list[BaseTool]:
        hierarchy = {
            PermissionLevel.READ: 0,
            PermissionLevel.WRITE: 1,
            PermissionLevel.EXECUTE: 2,
            PermissionLevel.DESTRUCTIVE: 3,
        }
        max_level = hierarchy[level]
        return [t for t in self.tools.values() if hierarchy[t.permission] <= max_level]


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(FileListTool())
    registry.register(TerminalTool())
    registry.register(GitTool())
    registry.register(MemoryTool())
    return registry
