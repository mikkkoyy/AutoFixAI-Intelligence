import os
import re

from AIRA.core.logging import get_logger

logger = get_logger("pc_agent")


BLOCKED_PATTERNS = [
    re.compile(r"(?i)\bformat\s+[a-zA-Z]:"),
    re.compile(r"(?i)\bdiskpart\b"),
    re.compile(r"(?i)\bshutdown\s+/[sf]"),
    re.compile(r"(?i)\brestart\s+/[sf]"),
    re.compile(r"(?i)\bRemove-Item\s+.*-Recurse.*C:\\Windows"),
    re.compile(r"(?i)\bRemove-Item\s+.*-Recurse.*C:\\Program\s*Files"),
    re.compile(r"(?i)\bSet-ItemProperty.*HKLM\\SAM"),
    re.compile(r"(?i)\bnet\s+user\b.*password"),
    re.compile(r"(?i)\bnetsh\s+advfirewall\s+set\s+allprofiles\s+state\s+off"),
    re.compile(r"(?i)\bschtasks\s+/Delete"),
    re.compile(r"(?i)\bcd\s+(\.\.\\){3,}"),
    re.compile(r"(?i)\bcertutil\s+-decode"),
    re.compile(r"(?i)\bGet-Credential\b"),
    re.compile(r"(?i)\bGet-Secret\b"),
    re.compile(r"(?i)\bConvertTo-SecureString\b"),
    re.compile(r"(?i)\bImport-PfxCertificate\b"),
    re.compile(r"(?i)\bRegister-ScheduledTask\b"),
    re.compile(r"(?i)\bNew-LocalUser\b"),
    re.compile(r"(?i)\bAdd-LocalGroupMember\b"),
]

DANGEROUS_KEYWORDS = [
    "format",
    "diskpart",
    "cipher /w",
    "takeown",
    "icacls",
    "bcdedit",
    "bootcfg",
    "sfc /scannow",
    "dism /online",
    "Get-WmiObject Win32_Account",
]

PROTECTED_PATHS = [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users\*\AppData\Local\Microsoft\Windows\INetCache",
    r"C:\Users\*\AppData\Local\Microsoft\Windows\Credentials",
]

CONTEXT_PATHS = [
    r"C:\Users\*\Documents",
    r"C:\Users\*\Desktop",
    r"C:\Users\*\Downloads",
    r"C:\Users\*\Pictures",
]


class CommandValidator:

    def __init__(self, blocked_patterns=None, protected_paths=None):
        self.blocked_patterns = blocked_patterns or BLOCKED_PATTERNS
        self.protected_paths = protected_paths or PROTECTED_PATHS

    def validate_command(self, command: str) -> tuple[bool, str]:
        if not command or not command.strip():
            return False, "Empty command"

        cmd_stripped = command.strip()

        for pattern in self.blocked_patterns:
            if pattern.search(cmd_stripped):
                return False, "Blocked dangerous command pattern detected"

        cmd_lower = cmd_stripped.lower()

        for keyword in DANGEROUS_KEYWORDS:
            if keyword.lower() in cmd_lower:
                return False, f"Command contains restricted keyword: {keyword}"

        if re.search(
            r"(?i)\bInvoke-WebRequest\b.*-Uri.*credentials",
            cmd_stripped,
        ):
            return False, "Command contains restricted credential access"

        return True, "Command validated"

    def validate_powershell(self, command: str) -> tuple[bool, str]:
        is_valid, reason = self.validate_command(command)

        if not is_valid:
            return False, reason

        dangerous_ps_patterns = [
            r"(?i)\bInvoke-Expression\b",
            r"(?i)\bIEX\b",
            r"(?i)\bInvoke-Command\b.*-ScriptBlock",
            r"(?i)\bStart-Process\b.*-Verb\s+RunAs",
            r"(?i)\b\[System\.Environment\]::SetEnvironmentVariable\b",
            r"(?i)\bAdd-MpPreference\b",
            r"(?i)\bSet-MpPreference\b",
            r"(?i)\bRemove-MpPreference\b",
            r"(?i)\bGet-Process.*Stop-Process\b",
        ]

        for pattern in dangerous_ps_patterns:
            if re.search(pattern, command):
                return False, "Restricted PowerShell operation detected"

        return True, "PowerShell command validated"

    @staticmethod
    def _normalize_windows_path(path: str) -> str:
        value = os.path.normpath(str(path).strip())
        value = value.replace("/", "\\")
        return os.path.normcase(value)

    @staticmethod
    def _is_within_path(candidate: str, protected: str) -> bool:
        candidate = CommandValidator._normalize_windows_path(candidate)
        protected = CommandValidator._normalize_windows_path(protected)

        try:
            return os.path.commonpath([candidate, protected]) == protected
        except ValueError:
            return False

    def _matches_protected_pattern(self, abs_path: str, protected: str) -> bool:
        protected_norm = self._normalize_windows_path(protected)
        abs_norm = self._normalize_windows_path(abs_path)

        if "*" not in protected_norm:
            return self._is_within_path(abs_norm, protected_norm)

        escaped = re.escape(protected_norm).replace(
            r"\*",
            r"[^\\]+"
        )

        pattern = re.compile(
            r"^" + escaped + r"(?:\\.*)?$",
            re.IGNORECASE,
        )

        return bool(pattern.match(abs_norm))

    def validate_path_access(self, path: str) -> tuple[bool, str]:
        if not path or not str(path).strip():
            return False, "Empty path"

        try:
            raw_path = str(path).strip().replace("/", "\\")

            parts = [
                part
                for part in raw_path.split("\\")
                if part not in ("", ".")
            ]

            if ".." in parts:
                return False, "Path traversal detected"

            normalized = os.path.normpath(raw_path)
            abs_path = os.path.abspath(normalized)

            for protected in self.protected_paths:
                if self._matches_protected_pattern(abs_path, protected):
                    return False, (
                        f"Access to protected path denied: {protected}"
                    )

            return True, "Path access allowed"

        except (OSError, ValueError) as exc:
            logger.warning(
                "Path validation failed for %r: %s",
                path,
                exc,
            )
            return False, f"Invalid path: {exc}"


command_validator = CommandValidator()
