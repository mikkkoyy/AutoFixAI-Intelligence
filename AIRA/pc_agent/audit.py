import re
import json
from pathlib import Path
from datetime import datetime
from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")

AUDIT_LOG = Path(__file__).parent.parent / "logs" / "pc_agent.log"

SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|token|password|secret|credential|auth)["\s:=]+\S+'),
    re.compile(r'(?i)(Bearer\s+\S+)'),
    re.compile(r'(?i)(x-api-key["\s:=]+\S+)'),
    re.compile(r'(?i)(Authorization["\s:=]+\S+)'),
]

SENSITIVE_FIELDS = {
    "password", "token", "api_key", "secret", "credential",
    "authorization", "bearer", "access_token", "refresh_token",
}


def redact_secrets(text: str) -> str:
    if not text:
        return text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_dict(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    redacted = {}
    for key, value in data.items():
        if key.lower() in SENSITIVE_FIELDS:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, str):
            redacted[key] = redact_secrets(value)
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value)
        else:
            redacted[key] = value
    return redacted


class AuditLogger:
    def __init__(self):
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, event_type: str, tool: str = None, arguments: dict = None,
                  result: dict = None, agent_request: bool = False,
                  approved: bool = None, error: str = None):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        entry = {
            "timestamp": timestamp,
            "event_type": event_type,
        }

        if tool:
            entry["tool"] = tool
        if arguments:
            entry["arguments"] = redact_dict(arguments)
        if result:
            entry["result_summary"] = {
                "success": result.get("success"),
                "error": result.get("error"),
            }
        if agent_request:
            entry["agent_request"] = True
        if approved is not None:
            entry["approved"] = approved
        if error:
            entry["error"] = redact_secrets(error)

        log_line = json.dumps(entry, default=str)
        logger.info(f"[AUDIT] {log_line}")

        try:
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(f"{log_line}\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    def log_user_request(self, tool: str, arguments: dict):
        self.log_event("USER_REQUEST", tool=tool, arguments=arguments, agent_request=True)

    def log_tool_execution(self, tool: str, arguments: dict):
        self.log_event("TOOL_EXECUTION", tool=tool, arguments=arguments)

    def log_result(self, tool: str, result: dict):
        self.log_event("RESULT", tool=tool, result=result)

    def log_denied(self, tool: str, arguments: dict, reason: str):
        self.log_event("DENIED", tool=tool, arguments=arguments, error=reason)

    def log_approval_request(self, tool: str, arguments: dict):
        self.log_event("APPROVAL_REQUEST", tool=tool, arguments=arguments)

    def log_approval_response(self, tool: str, approved: bool):
        self.log_event("APPROVAL_RESPONSE", tool=tool, approved=approved)


audit_logger = AuditLogger()
