from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import time
import uuid


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class AgentRole(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    DEBUGGER = "debugger"
    TESTER = "tester"
    REVIEWER = "reviewer"
    VERIFICATION = "verification"

    OPENCODE = "opencode"

    RECOVERY = "recovery"

class AgentTaskStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentStep:
    role: AgentRole
    description: str
    status: AgentStatus = AgentStatus.IDLE
    result_message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0


@dataclass
class AgentTask:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    description: str = ""
    status: AgentTaskStatus = AgentTaskStatus.IDLE
    steps: list[AgentStep] = field(default_factory=list)
    current_step_index: int = -1
    workspace: str = ""
    result_message: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def current_step(self) -> AgentStep | None:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        completed = sum(
            1 for s in self.steps
            if s.status in (AgentStatus.PASSED, AgentStatus.FAILED)
        )
        return completed / len(self.steps)

    @property
    def is_finished(self) -> bool:
        return self.status in (
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
        )


@dataclass
class AgentResult:
    agent: str
    status: AgentStatus
    message: str


@dataclass
class BuildResult:
    success: bool
    project_path: Path
    files_created: list[Path] = field(default_factory=list)
    message: str = ""
