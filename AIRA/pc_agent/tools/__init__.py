from AIRA.pc_agent.tools.filesystem import FilesystemTool
from AIRA.pc_agent.tools.powershell import PowershellTool
from AIRA.pc_agent.tools.applications import ApplicationTool
from AIRA.pc_agent.tools.processes import ProcessTool
from AIRA.pc_agent.tools.system import SystemTool
from AIRA.pc_agent.tools.network import NetworkTool
from AIRA.pc_agent.tools.git_tool import GitTool
from AIRA.pc_agent.tools.screenshot import ScreenshotTool

ALL_TOOLS = [
    FilesystemTool,
    PowershellTool,
    ApplicationTool,
    ProcessTool,
    SystemTool,
    NetworkTool,
    GitTool,
    ScreenshotTool,
]

TOOL_ACTIONS = {
    "filesystem": ["list_directory", "read_file", "write_file", "create_directory",
                    "copy", "move", "delete", "exists", "file_info", "search"],
    "powershell": ["execute"],
    "application": ["open", "list_running", "close"],
    "process": ["list", "get", "stop"],
    "system": ["info"],
    "network": ["info", "ping", "dns_lookup", "connections"],
    "git": ["status", "log", "diff", "branch", "add", "commit", "push", "pull", "checkout"],
    "screenshot": ["capture"],
}


def create_all_tools(config: dict = None) -> dict:
    config = config or {}
    tools = {}
    for tool_cls in ALL_TOOLS:
        instance = tool_cls(config=config.get(tool_cls.name, {}))
        tools[tool_cls.name] = instance
    return tools
