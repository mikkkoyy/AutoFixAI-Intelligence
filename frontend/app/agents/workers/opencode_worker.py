from __future__ import annotations

from dataclasses import dataclass

from app.agents.coding_agent import (
    BackendInfo,
    CodingAgentRunner,
    CodingResult,
    _default_discover_opencode,
)


@dataclass
class OpenCodeWorker:
    """Adapter for the internal OpenCode coding backend."""

    runner: CodingAgentRunner | None = None

    def __post_init__(self):
        if self.runner is None:
            self.runner = CodingAgentRunner()

    def discover(self) -> BackendInfo:
        return _default_discover_opencode()

    def is_available(self) -> bool:
        info = self.discover()
        return bool(info.available and info.executable)

    def execute(self, prompt: str, workspace: str, on_output=None, timeout: int | None = None) -> CodingResult:
        return self.runner.execute(prompt, workspace, on_output=on_output, timeout=timeout)
