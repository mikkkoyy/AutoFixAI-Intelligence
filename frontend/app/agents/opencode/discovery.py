"""OpenCode CLI discovery — detect installed opencode, resolve path, get version."""

import subprocess
import shutil
from dataclasses import dataclass


@dataclass
class OpenCodeInfo:
    """Result of OpenCode discovery."""
    is_installed: bool
    executable_path: str | None
    version: str | None
    error: str | None = None


class OpenCodeDiscovery:
    """Detect the installed OpenCode CLI on the system PATH."""

    def discover(self) -> OpenCodeInfo:
        """Return info about the installed OpenCode CLI."""
        path = shutil.which("opencode")
        if not path:
            return OpenCodeInfo(
                is_installed=False,
                executable_path=None,
                version=None,
                error="opencode not found on PATH",
            )

        version = self._get_version(path)
        return OpenCodeInfo(
            is_installed=True,
            executable_path=path,
            version=version,
        )

    def _get_version(self, executable: str) -> str | None:
        """Run 'opencode --version' and return the first line."""
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (result.stdout or result.stderr).strip()
            return output.splitlines()[0] if output else None
        except (subprocess.TimeoutExpired, OSError):
            return None
