import asyncio
import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AIRA.pc_agent.security import generate_auth_token, verify_token, load_or_create_token
from AIRA.pc_agent.permissions import PermissionSystem, AutonomyMode, PermissionLevel, TOOL_PERMISSIONS
from AIRA.pc_agent.audit import redact_secrets, redact_dict, AuditLogger
from AIRA.pc_agent.command_validator import CommandValidator
from AIRA.pc_agent.agent import PCAgent
from AIRA.pc_agent.tools.filesystem import FilesystemTool
from AIRA.pc_agent.tools.powershell import PowershellTool
from AIRA.pc_agent.tools.system import SystemTool
from AIRA.pc_agent.tools.processes import ProcessTool
from AIRA.pc_agent.tools.network import NetworkTool
from AIRA.pc_agent.tools.git_tool import GitTool
from AIRA.pc_agent.tools.screenshot import ScreenshotTool
from AIRA.pc_agent.tools.applications import ApplicationTool
from AIRA.config import config


def test_security():
    token1 = generate_auth_token()
    token2 = generate_auth_token()
    assert len(token1) == 64
    assert token1 != token2

    assert verify_token(token1, token1) is True
    assert verify_token(token1, token2) is False
    assert verify_token("", token1) is False
    assert verify_token(token1, "") is False
    assert verify_token(None, token1) is False
    print("PASS: security")


def test_permissions():
    ps = PermissionSystem({"mode": "safe", "read": True, "write": True, "execute": False, "destructive": False})
    assert ps.mode == AutonomyMode.SAFE
    assert ps.has_permission("system.info") is True
    assert ps.has_permission("filesystem.read_file") is True
    assert ps.has_permission("filesystem.write_file") is True
    assert ps.has_permission("powershell.execute") is False
    assert ps.has_permission("process.stop") is False
    assert ps.requires_approval("powershell.execute") is True
    assert ps.requires_approval("process.stop") is True
    assert ps.requires_approval("filesystem.read_file") is False
    assert ps.requires_approval("filesystem.write_file") is False
    status = ps.get_status()
    assert status["mode"] == "safe"
    assert status["read"] is True
    assert status["destructive"] is False
    print("PASS: permissions")


def test_permissions_auto_mode():
    ps = PermissionSystem({"mode": "auto", "read": True, "write": True, "execute": True, "destructive": True})
    assert ps.requires_approval("powershell.execute") is False
    assert ps.requires_approval("process.stop") is False
    print("PASS: permissions_auto_mode")


def test_permissions_manual_mode():
    ps = PermissionSystem({"mode": "manual", "read": True, "write": True, "execute": True, "destructive": True})
    assert ps.requires_approval("filesystem.read_file") is True
    assert ps.requires_approval("filesystem.write_file") is True
    print("PASS: permissions_manual_mode")


def test_permissions_blocked_tools():
    ps = PermissionSystem({"blocked_tools": ["screenshot.capture"]})
    assert ps.has_permission("screenshot.capture") is False
    assert ps.has_permission("system.info") is True
    print("PASS: permissions_blocked_tools")


def test_audit_redaction():
    assert "secret_key_abc123" not in redact_secrets('api_key="secret_key_abc123"')
    assert "my_api_token" not in redact_secrets('token: my_api_token')
    assert "hunter2" not in redact_secrets('password: hunter2')
    assert "hello" in redact_secrets("hello world")
    print("PASS: audit_redaction")


def test_audit_dict_redaction():
    data = {"password": "secret123", "token": "abc", "name": "test", "api_key": "key123"}
    redacted = redact_dict(data)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["name"] == "test"
    print("PASS: audit_dict_redaction")


def test_command_validator():
    cv = CommandValidator()
    valid, _ = cv.validate_powershell("Get-ChildItem")
    assert valid is True
    valid, _ = cv.validate_powershell("Invoke-Expression 'bad'")
    assert valid is False
    valid, _ = cv.validate_powershell("Get-Credential")
    assert valid is False
    valid, _ = cv.validate_powershell("Start-Process powershell -Verb RunAs")
    assert valid is False
    valid, _ = cv.validate_powershell("Add-MpPreference -ExclusionPath C:\\")
    assert valid is False
    valid, _ = cv.validate_powershell("format C:")
    assert valid is False
    valid, _ = cv.validate_powershell("")
    assert valid is False
    valid, _ = cv.validate_command("shutdown /s /t 0")
    assert valid is False
    valid, _ = cv.validate_command("diskpart")
    assert valid is False
    print("PASS: command_validator")


def test_path_traversal():
    cv = CommandValidator()
    valid, _ = cv.validate_path_access("D:\\project\\file.txt")
    assert valid is True
    valid, _ = cv.validate_path_access("C:\\Windows\\System32\\cmd.exe")
    assert valid is False
    print("PASS: path_traversal")


def test_filesystem_tools():
    project_root = Path(__file__).resolve().parents[1]
    tmp = Path(tempfile.gettempdir()) / "aira_test_fs"
    tmp.mkdir(exist_ok=True)
    fs = FilesystemTool({
        "allowed_paths": [str(project_root), str(tmp)]
    })
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(fs.execute("exists", path=str(project_root)))
    assert result["success"] is True
    result = loop.run_until_complete(fs.execute("list_directory", path=str(project_root)))
    assert result["success"] is True
    (tmp / "test.txt").write_text("hello", encoding="utf-8")
    result = loop.run_until_complete(fs.execute("read_file", path=str(tmp / "test.txt")))
    assert result["success"] is True
    assert result["result"] == "hello"
    result = loop.run_until_complete(fs.execute("file_info", path=str(tmp / "test.txt")))
    assert result["success"] is True
    result = loop.run_until_complete(fs.execute("search", path=str(tmp), pattern="*.txt"))
    assert result["success"] is True
    shutil.rmtree(str(tmp), ignore_errors=True)
    loop.close()
    print("PASS: filesystem_tools")


def test_filesystem_write_protection():
    fs = FilesystemTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(fs.execute("write_file", path=".env", content="bad"))
    assert result["success"] is False
    assert "protected" in result["error"].lower() or "blocked" in result["error"].lower()
    result = loop.run_until_complete(fs.execute("write_file", path=".pc_agent/auth_token", content="bad"))
    assert result["success"] is False
    loop.close()
    print("PASS: filesystem_write_protection")


def test_system_tool():
    st = SystemTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(st.execute("info"))
    assert result["success"] is True
    info = result["result"]
    assert info["os"] == "Windows"
    assert "hostname" in info
    assert "python_version" in info
    loop.close()
    print("PASS: system_tool")


def test_process_tool():
    pt = ProcessTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(pt.execute("list"))
    assert result["success"] is True
    assert isinstance(result["result"], list)
    result = loop.run_until_complete(pt.execute("stop", name="svchost"))
    assert result["success"] is False
    assert "critical" in result["error"].lower()
    loop.close()
    print("PASS: process_tool")


def test_network_tool():
    nt = NetworkTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(nt.execute("info"))
    assert result["success"] is True
    result = loop.run_until_complete(nt.execute("ping", target="127.0.0.1", count=1))
    assert result["success"] is True
    loop.close()
    print("PASS: network_tool")


def test_application_tool():
    at = ApplicationTool({"allowed": ["notepad.exe"]})
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(at.execute("list_running"))
    assert result["success"] is True
    result = loop.run_until_complete(at.execute("open", application="malware.exe"))
    assert result["success"] is False
    assert "allowlist" in result["error"].lower() or "not in" in result["error"].lower()
    loop.close()
    print("PASS: application_tool")


def test_git_tool():
    gt = GitTool()
    assert gt.requires_approval("push") is True
    assert gt.requires_approval("commit") is True
    assert gt.requires_approval("status") is False
    assert gt.requires_approval("diff") is False
    print("PASS: git_tool")


def test_screenshot_tool():
    st = ScreenshotTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(st.execute("capture"))
    loop.close()
    assert "success" in result
    print("PASS: screenshot_tool")


def test_powershell_tool():
    ps = PowershellTool({"timeout": 10})
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(ps.execute(command="Write-Output 'hello'"))
    assert result["success"] is True
    assert "hello" in result["stdout"]
    assert result["exit_code"] == 0
    result = loop.run_until_complete(ps.execute(command="Get-ChildItem C:\\Windows -ErrorAction SilentlyContinue | Select-Object -First 3"))
    assert result["success"] is True
    loop.close()
    print("PASS: powershell_tool")


def test_powershell_blocked():
    ps = PowershellTool()
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(ps.execute(command="Invoke-Expression 'bad code'"))
    assert result["success"] is False
    assert "blocked" in result["error"].lower()
    result = loop.run_until_complete(ps.execute(command="format C:"))
    assert result["success"] is False
    loop.close()
    print("PASS: powershell_blocked")


def test_agent_integration():
    config.load()
    pc_config = config.get("pc_agent", {})
    agent = PCAgent(pc_config)
    status = agent.get_status()
    assert status["agent"] == "AIRA PC Agent"
    assert status["version"] == "1.0.0"
    assert status["platform"] == "Windows"
    tools = agent.list_tools()
    assert len(tools) > 0
    names = [t["name"] for t in tools]
    assert "filesystem" in names
    assert "powershell" in names
    assert "system" in names
    assert "git" in names
    assert "screenshot" in names
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(agent.execute_tool("system.info", {}))
    assert result["success"] is True
    result = loop.run_until_complete(agent.execute_tool("nonexistent.action", {}))
    assert result["success"] is False
    assert "not found" in result["error"].lower()
    loop.close()
    print("PASS: agent_integration")


def test_agent_approval():
    pc_config = {"permissions": {"mode": "manual", "read": True, "write": True, "execute": True, "destructive": True}}
    agent = PCAgent(pc_config)
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(agent.execute_tool("powershell.execute", {"command": "echo test"}))
    assert result.get("error") == "APPROVAL_REQUIRED"
    approval_id = result.get("approval_id")
    assert approval_id
    result = agent.approve_operation(approval_id)
    assert result["success"] is True
    result = loop.run_until_complete(agent.execute_tool("powershell.execute", {"command": "echo test"}, auto_approve=True))
    assert result["success"] is True
    loop.close()
    print("PASS: agent_approval")


def test_secret_not_in_logs():
    token = generate_auth_token()
    from AIRA.pc_agent.audit import redact_secrets
    redacted = redact_secrets(f"token={token}")
    assert token not in redacted
    print("PASS: secret_not_in_logs")


def test_tool_permission_mapping():
    for tool_name, expected_level in TOOL_PERMISSIONS.items():
        assert isinstance(expected_level, PermissionLevel)
    assert TOOL_PERMISSIONS["system.info"] == PermissionLevel.READ
    assert TOOL_PERMISSIONS["filesystem.write_file"] == PermissionLevel.WRITE
    assert TOOL_PERMISSIONS["powershell.execute"] == PermissionLevel.EXECUTE
    assert TOOL_PERMISSIONS["process.stop"] == PermissionLevel.DESTRUCTIVE
    print("PASS: tool_permission_mapping")


import shutil


def main():
    test_security()
    test_permissions()
    test_permissions_auto_mode()
    test_permissions_manual_mode()
    test_permissions_blocked_tools()
    test_audit_redaction()
    test_audit_dict_redaction()
    test_command_validator()
    test_path_traversal()
    test_filesystem_tools()
    test_filesystem_write_protection()
    test_system_tool()
    test_process_tool()
    test_network_tool()
    test_application_tool()
    test_git_tool()
    test_screenshot_tool()
    test_powershell_tool()
    test_powershell_blocked()
    test_agent_integration()
    test_agent_approval()
    test_secret_not_in_logs()
    test_tool_permission_mapping()
    print("\n=== ALL PC AGENT TESTS PASSED ===")


if __name__ == "__main__":
    main()

