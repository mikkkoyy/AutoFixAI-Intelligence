from AIRA.core.logging import get_logger
from AIRA.pc_agent.permissions import PermissionSystem
from AIRA.pc_agent.audit import audit_logger
from AIRA.pc_agent.tools import create_all_tools, TOOL_ACTIONS

logger = get_logger("pc_agent")


class PCAgent:
    def __init__(self, config: dict = None):
        config = config or {}
        self.permissions = PermissionSystem(config.get("permissions", {}))
        self.tools = create_all_tools(config)
        self.approval_queue: dict[str, dict] = {}

    def get_tool(self, tool_name: str):
        return self.tools.get(tool_name)

    def list_tools(self) -> list[dict]:
        result = []
        for name, tool in self.tools.items():
            actions = TOOL_ACTIONS.get(name, [])
            perm_level = self.permissions.get_tool_permission(f"{name}.*")
            allowed = self.permissions.has_permission(f"{name}.*")
            result.append({
                "name": name,
                "description": tool.description,
                "permission": perm_level.value,
                "enabled": allowed,
                "actions": actions,
            })
        return result

    def get_enabled_tools(self) -> list[dict]:
        return [t for t in self.list_tools() if t["enabled"]]

    async def execute_tool(self, full_tool_name: str, arguments: dict, auto_approve: bool = False) -> dict:
        parts = full_tool_name.split(".", 1)
        if len(parts) != 2:
            return {"success": False, "error": "Invalid tool name format. Use: tool.action"}

        tool_name, action = parts

        tool = self.get_tool(tool_name)
        if not tool:
            return {"success": False, "error": f"Tool not found: {tool_name}"}

        if not self.permissions.has_permission(full_tool_name):
            audit_logger.log_denied(full_tool_name, arguments, "Permission denied")
            return {
                "success": False,
                "error": "PERMISSION_DENIED",
                "message": f"Permission denied for {full_tool_name}. Required: {self.permissions.get_tool_permission(full_tool_name).value}",
            }

        if self.permissions.requires_approval(full_tool_name) and not auto_approve:
            approval_id = f"{full_tool_name}_{id(arguments)}"
            self.approval_queue[approval_id] = {
                "tool": full_tool_name,
                "arguments": arguments,
                "status": "pending",
            }
            audit_logger.log_approval_request(full_tool_name, arguments)
            return {
                "success": False,
                "error": "APPROVAL_REQUIRED",
                "message": f"Operation {full_tool_name} requires approval",
                "approval_id": approval_id,
                "tool": full_tool_name,
                "arguments": arguments,
            }

        audit_logger.log_user_request(full_tool_name, arguments)
        audit_logger.log_tool_execution(full_tool_name, arguments)

        try:
            result = await tool.execute(action=action, **arguments)
            audit_logger.log_result(full_tool_name, result)
            return result
        except Exception as e:
            error_result = {"success": False, "error": str(e)}
            audit_logger.log_result(full_tool_name, error_result)
            return error_result

    def approve_operation(self, approval_id: str) -> dict:
        pending = self.approval_queue.get(approval_id)
        if not pending:
            return {"success": False, "error": "Approval not found or expired"}
        pending["status"] = "approved"
        audit_logger.log_approval_response(pending["tool"], True)
        return {"success": True, "message": "Approved", "operation": pending}

    def deny_operation(self, approval_id: str) -> dict:
        pending = self.approval_queue.get(approval_id)
        if not pending:
            return {"success": False, "error": "Approval not found or expired"}
        pending["status"] = "denied"
        audit_logger.log_approval_response(pending["tool"], False)
        self.approval_queue.pop(approval_id, None)
        return {"success": True, "message": "Denied"}

    def get_status(self) -> dict:
        return {
            "agent": "AIRA PC Agent",
            "version": "1.0.0",
            "platform": "Windows",
            "permissions": self.permissions.get_status(),
            "tools_enabled": len(self.get_enabled_tools()),
            "tools_total": len(self.tools),
            "pending_approvals": len([v for v in self.approval_queue.values() if v["status"] == "pending"]),
        }
