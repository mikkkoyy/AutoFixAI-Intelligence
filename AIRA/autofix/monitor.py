"""Normalize failures into structured ErrorReports."""

from __future__ import annotations

import os
import re
import traceback
from pathlib import Path
from typing import Optional, Union

from AIRA.autofix.models import ErrorReport

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

SENSITIVE_VALUE_CACHE_LIMIT = 200


class SecretRedactor:
    """Redacts credentials and secret values from extracted text."""

    _PATTERNS = [
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        re.compile(r"sk-[A-Za-z0-9_-]{16,}", re.IGNORECASE),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}", re.IGNORECASE),
        re.compile(r"(?i)api[_-]?key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']"),
        re.compile(r"(?i)password[\"']?\s*[:=]\s*\S+"),
        re.compile(r"(?i)secret[\"']?\s*[:=]\s*\S+"),
        re.compile(r"(?i)token[\"']?\s*[:=]\s*\S+"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ]

    _SENSITIVE_ENV_KEYS = {
        "API_KEY",
        "SECRET",
        "TOKEN",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "PRIVATE_KEY",
        "AUTH",
    }

    def __init__(self):
        self._env_values = self._collect_env_values()

    @classmethod
    def _collect_env_values(cls) -> list[str]:
        values = []
        for key, value in os.environ.items():
            upper = key.upper()
            if any(marker in upper for marker in cls._SENSITIVE_ENV_KEYS):
                if value and len(value) >= 6:
                    values.append(value)
        return values[:SENSITIVE_VALUE_CACHE_LIMIT]

    def redact(self, text: str) -> str:
        if not text:
            return text
        result = text
        for value in self._env_values:
            if value in result:
                result = result.replace(value, "[REDACTED]")
        for pattern in self._PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result

    def clean_report(self, report: ErrorReport) -> ErrorReport:
        report.message = self.redact(report.message)
        report.traceback = self.redact(report.traceback)
        return report


_FRAME_RE = re.compile(
    r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([^\n]+))?'
)


def _parse_traceback_line(trace_text: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    frames = _FRAME_RE.findall(trace_text)
    if not frames:
        return None, None, None
    filepath, line, func = frames[-1]
    return filepath, int(line), func


def _derive_test_name(trace_text: str, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    for filepath, _, func in _FRAME_RE.findall(trace_text):
        if func and _looks_like_test(func):
            try:
                rel = Path(filepath).resolve()
                root = PROJECT_ROOT.resolve()
                rel_path = rel.relative_to(root)
            except Exception:
                rel_path = Path(filepath.replace("\\", "/"))
            return f"{rel_path.as_posix()}::{func}"
    return None


def _looks_like_test(func: str) -> bool:
    return func.startswith("test_") or "test_" in func


def _extract_error_from_trace_text(trace_text: str) -> tuple[str, str]:
    lines = [line for line in trace_text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return "Error", "Unknown error"
    last = lines[-1]
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(?:\([^)]*\))?:\s?(.*)$", last)
    if match and match.group(2):
        return match.group(1), match.group(2)
    head = last.split(": ", 1)
    if len(head) == 2 and head[0] and not head[0].isdigit():
        return head[0].strip(), head[1].strip()
    return "Error", last


_PYTEST_NODE_RE = re.compile(r"^([^\s]+\.py)(::[^:\s]+)$")


class ErrorMonitor:
    """Creates normalized ErrorReport objects from various failure sources."""

    def __init__(self, repository_path: Union[str, Path] = PROJECT_ROOT):
        self.repository_path = Path(repository_path)
        self.redactor = SecretRedactor()

    def normalize_exception(self, exc: BaseException) -> ErrorReport:
        trace_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        return self._build(e_type=type(exc).__name__, trace_text=trace_text)

    def normalize_traceback(
        self,
        trace_text: str,
        error_type: Optional[str] = None,
        message: Optional[str] = None,
        test_name: Optional[str] = None,
    ) -> ErrorReport:
        return self._build(
            trace_text=trace_text,
            e_type=error_type,
            message=message,
            test_name=test_name,
        )

    def normalize_pytest(
        self,
        failure_text: str,
        test_name: Optional[str] = None,
    ) -> ErrorReport:
        trace_text = failure_text
        match = _PYTEST_NODE_RE.match((test_name or "").strip())
        if not test_name and "::" in failure_text:
            first = failure_text.splitlines()[0].strip()
            match_node = re.match(r"^\s*FAILED\s+([^\s]+)\s*-?\s*(.*)$", first)
            if match_node:
                test_name = match_node.group(1)
                remainder = match_node.group(2)
                if remainder:
                    trace_text = f"AssertionError: {remainder}"
        elif match:
            test_name = match.group(1) + match.group(2)
        elif test_name:
            node = _PYTEST_NODE_RE.match(test_name.strip())
            if not node:
                match = re.search(r"FAILED\s+([^\s]+)", test_name)
                if match:
                    test_name = match.group(1)
        return self._build(trace_text=trace_text, test_name=test_name)

    def normalize_log(
        self,
        line: str,
        command: Optional[str] = None,
    ) -> ErrorReport:
        line = line or ""
        match = re.search(r"^\[(ERROR|CRITICAL)\]\s*(.*)$", line)
        if match:
            remainder = match.group(2).strip()
        else:
            remainder = line.strip()
        e_type, message = None, None
        head, sep, tail = remainder.partition(":")
        if sep and tail.strip():
            if "." in head:
                e_type, message = _extract_error_from_trace_text(tail.strip())
            else:
                e_type = head.strip()
                message = tail.strip()
        if not message:
            e_type, message = _extract_error_from_trace_text(remainder)
        return self._build(trace_text=line, e_type=e_type, message=message, command=command)

    def _build(
        self,
        trace_text: str,
        e_type: Optional[str] = None,
        message: Optional[str] = None,
        test_name: Optional[str] = None,
        command: Optional[str] = None,
    ) -> ErrorReport:
        trace_text = trace_text.replace("\r\n", "\n").strip()
        if not message:
            if not e_type or message is None:
                inferred_type, inferred_msg = _extract_error_from_trace_text(trace_text)
                e_type = e_type or inferred_type or "Error"
                message = message if message is not None else inferred_msg
        e_type = e_type or "Error"
        message = message or e_type
        if not trace_text:
            trace_text = f"{e_type}: {message}"

        if test_name and _PYTEST_NODE_RE.match(test_name.strip()):
            node = test_name.strip()
        else:
            node = test_name

        source_file, source_line, _ = _parse_traceback_line(trace_text)
        derived_test = _derive_test_name(trace_text, node)

        report = ErrorReport(
            error_type=e_type,
            message=message,
            traceback=trace_text,
            source_file=source_file,
            source_line=source_line,
            test_name=derived_test,
            command=command,
            repository_path=str(self.repository_path),
        )
        return self.redactor.clean_report(report)


def normalize_error(
    exc: Optional[BaseException] = None,
    traceback_text: Optional[str] = None,
    pytest_failure: Optional[str] = None,
    log_line: Optional[str] = None,
    command: Optional[str] = None,
    test_name: Optional[str] = None,
    repository_path: Optional[Union[str, Path]] = PROJECT_ROOT,
) -> ErrorReport:
    """Convenience factory: normalize a failure into an ErrorReport."""
    monitor = ErrorMonitor(repository_path=repository_path)
    if exc is not None:
        return monitor.normalize_exception(exc)
    if traceback_text is not None:
        return monitor.normalize_traceback(traceback_text, test_name=test_name)
    if pytest_failure is not None:
        return monitor.normalize_pytest(pytest_failure, test_name=test_name)
    if log_line is not None:
        return monitor.normalize_log(log_line, command=command)
    return monitor.normalize_traceback(
        "Error: unknown failure",
        error_type="Error",
        message="unknown failure",
        test_name=test_name,
    )