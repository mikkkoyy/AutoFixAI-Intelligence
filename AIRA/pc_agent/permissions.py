from enum import Enum
from typing import Optional
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


class PermissionLevel(Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


PERMISSION_HIERARCHY = {
    PermissionLevel.READ: 0,
    PermissionLevel.WRITE: 1,
    PermissionLevel.EXECUTE: 2,
    PermissionLevel.DESTRUCTIVE: 3,
}


class AutonomyMode(Enum):
    MANUAL = "manual"
    SAFE = "safe"
    SEMI_AUTO = "semi_auto"
    AUTO = "auto"


TOOL_PERMISSIONS = {
    "filesystem.list_directory": PermissionLevel.READ,
    "filesystem.read_file": PermissionLevel.READ,
    "filesystem.exists": PermissionLevel.READ,
    "filesystem.file_info": PermissionLevel.READ,
    "filesystem.search": PermissionLevel.READ,
    "filesystem.write_file": PermissionLevel.WRITE,
    "filesystem.create_directory": PermissionLevel.WRITE,
    "filesystem.copy": PermissionLevel.WRITE,
    "filesystem.move": PermissionLevel.WRITE,
    "filesystem.delete": PermissionLevel.DESTRUCTIVE,
    "powershell.execute": PermissionLevel.EXECUTE,
    "application.open": PermissionLevel.EXECUTE,
    "application.list_running": PermissionLevel.READ,
    "application.close": PermissionLevel.EXECUTE,
    "process.list": PermissionLevel.READ,
    "process.get": PermissionLevel.READ,
    "process.stop": PermissionLevel.DESTRUCTIVE,
    "system.info": PermissionLevel.READ,
    "network.info": PermissionLevel.READ,
    "network.ping": PermissionLevel.EXECUTE,
    "network.dns_lookup": PermissionLevel.EXECUTE,
    "git.status": PermissionLevel.READ,
    "git.log": PermissionLevel.READ,
    "git.diff": PermissionLevel.READ,
    "git.branch": PermissionLevel.READ,
    "git.add": PermissionLevel.WRITE,
    "git.commit": PermissionLevel.WRITE,
    "git.push": PermissionLevel.EXECUTE,
    "git.pull": PermissionLevel.EXECUTE,
    "git.checkout": PermissionLevel.WRITE,
    "screenshot.capture": PermissionLevel.READ,
}


class PermissionSystem:
    def __init__(self, config: dict = None):
        config = config or {}
        self.mode = AutonomyMode(config.get("mode", "safe"))
        self.max_level = PermissionLevel(
            config.get("max_permission", "execute")
        )
        self.allowed_tools: Optional[set] = config.get("allowed_tools")
        self.blocked_tools: set = set(config.get("blocked_tools", []))
        self.read_enabled = config.get("read", True)
        self.write_enabled = config.get("write", True)
        self.execute_enabled = config.get("execute", False)
        self.destructive_enabled = config.get("destructive", False)

    def is_tool_allowed(self, tool_name: str) -> bool:
        if tool_name in self.blocked_tools:
            return False
        if self.allowed_tools is not None:
            return tool_name in self.allowed_tools
        return True

    def get_tool_permission(self, tool_name: str) -> PermissionLevel:
        return TOOL_PERMISSIONS.get(tool_name, PermissionLevel.READ)

    def has_permission(self, tool_name: str) -> bool:
        if not self.is_tool_allowed(tool_name):
            return False

        tool_level = self.get_tool_permission(tool_name)

        level_enabled = {
            PermissionLevel.READ: self.read_enabled,
            PermissionLevel.WRITE: self.write_enabled,
            PermissionLevel.EXECUTE: self.execute_enabled,
            PermissionLevel.DESTRUCTIVE: self.destructive_enabled,
        }

        if not level_enabled.get(tool_level, False):
            return False

        if PERMISSION_HIERARCHY[tool_level] > PERMISSION_HIERARCHY[self.max_level]:
            return False

        return True

    def requires_approval(self, tool_name: str) -> bool:
        if self.mode == AutonomyMode.AUTO:
            return False
        if self.mode == AutonomyMode.MANUAL:
            return True

        tool_level = self.get_tool_permission(tool_name)

        if tool_level == PermissionLevel.DESTRUCTIVE:
            return True
        if tool_level == PermissionLevel.EXECUTE and self.mode == AutonomyMode.SAFE:
            return True
        if self.mode == AutonomyMode.SAFE and tool_level in (
            PermissionLevel.EXECUTE, PermissionLevel.DESTRUCTIVE
        ):
            return True
        if self.mode == AutonomyMode.SEMI_AUTO and tool_level == PermissionLevel.DESTRUCTIVE:
            return True

        return False

    def get_status(self) -> dict:
        return {
            "mode": self.mode.value,
            "max_permission": self.max_level.value,
            "read": self.read_enabled,
            "write": self.write_enabled,
            "execute": self.execute_enabled,
            "destructive": self.destructive_enabled,
        }
