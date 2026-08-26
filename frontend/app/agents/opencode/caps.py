"""OpenCode CLI capability detection.

``continue_opencode`` must only pass flags the installed binary actually
supports.  This module runs ``<exe> --version`` and ``<exe> run --help`` ONCE
per executable, parses the supported long options and caches the result.
Detection failures are reported honestly — nothing is assumed.
"""

from __future__ import annotations

import re
import subprocess

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CAPS_TIMEOUT_SECONDS = 10

_FLAG_RE = re.compile(r"(?<!\w)(--[a-zA-Z0-9][a-zA-Z0-9-]*)")

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+[^\s]*)")


class OpenCodeCaps:
    """Detected capabilities of one OpenCode executable."""

    def __init__(self, version: str | None, run_flags: frozenset[str], error: str = ""):
        self.version = version
        self.run_flags = run_flags
        self.error = error

    @property
    def available(self) -> bool:
        return self.version is not None

    def supports(self, flag: str) -> bool:
        return flag in self.run_flags

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "version": self.version,
            "run_flags": sorted(self.run_flags),
            "error": self.error or None,
        }


def _run(executable: str, args: list[str]) -> str:
    completed = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CAPS_TIMEOUT_SECONDS,
        creationflags=_CREATE_NO_WINDOW,
    )
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"


def detect_run_capabilities(executable: str, *, runner=None) -> OpenCodeCaps:
    """Probe *executable* for its version and supported ``run`` flags."""
    do_run = runner or _run
    try:
        version_output = do_run(executable, ["--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OpenCodeCaps(None, frozenset(), f"probe failed: {exc}")

    version_match = _VERSION_RE.search(version_output or "")
    version = version_match.group(1) if version_match else None

    try:
        help_output = do_run(executable, ["run", "--help"])
    except (OSError, subprocess.TimeoutExpired):
        help_output = ""

    flags = frozenset(_FLAG_RE.findall(help_output or ""))
    return OpenCodeCaps(version, flags)


_CACHE: dict[str, OpenCodeCaps] = {}


def cached_caps(executable: str, *, refresh: bool = False) -> OpenCodeCaps:
    """Detect once per executable path, then serve from cache."""
    key = str(executable)
    if refresh or key not in _CACHE:
        _CACHE[key] = detect_run_capabilities(key)
    return _CACHE[key]
