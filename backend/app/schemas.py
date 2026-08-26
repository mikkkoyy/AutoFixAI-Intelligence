from typing import Literal
from pydantic import BaseModel, Field


JobStatus = Literal["queued", "running", "verified", "failed"]


class JobRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    # Backward-compatible:
    # API requests may provide task, while older internal callers may omit it.
    task: str = Field(default="", max_length=10000)

    workspace: str | None = None

    # Current API name.
    max_attempts: int = Field(default=3, ge=0, le=10)

    # Backward-compatible name used by the original pipeline tests.
    max_fix_attempts: int | None = Field(default=None, ge=0, le=10)

    command: list[str] = Field(
        default_factory=lambda: ["pytest", "-q"]
    )

    @property
    def effective_max_attempts(self) -> int:
        """
        Return the configured maximum number of fix attempts.

        max_fix_attempts is retained for backward compatibility.
        max_attempts remains the preferred API field.
        """
        if self.max_fix_attempts is not None:
            return self.max_fix_attempts
        return self.max_attempts


class Event(BaseModel):
    sequence: int
    event: str
    message: str


class JobResponse(BaseModel):
    job_id: str
    name: str
    status: JobStatus
    attempts: int
    message: str


class JobDetail(JobResponse):
    task: str
    workspace: str
    events: list[Event]