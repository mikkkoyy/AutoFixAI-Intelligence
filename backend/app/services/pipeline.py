from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.db import create_job, update_job, add_event
from app.services.workspace import WorkspaceManager
from app.services.scanner import ProjectScanner
from app.services.snapshot import SnapshotService
from app.services.provider import build_provider
from app.services.runner import TestRunner, RunResult
from app.services.diagnostics import DiagnosticService
from app.services.checkpoint import CheckpointService
from app.schemas import JobRequest


@dataclass
class PipelineResult:
    """
    Result returned by PipelineService.

    The object exposes the richer pipeline result expected by the
    service-level tests while remaining backward-compatible with
    the original API route which unpacked:

        job_id, workspace = pipeline.run(...)
    """

    job_id: str
    workspace: Path
    status: str
    test: RunResult
    attempts: int
    checkpoint: str | None
    message: str

    def __iter__(self):
        """
        Backward compatibility for existing callers that unpack
        PipelineResult as:

            job_id, workspace = result
        """
        yield self.job_id
        yield self.workspace


class PipelineService:
    def __init__(self):
        self.workspaces = WorkspaceManager()
        self.scanner = ProjectScanner()
        self.snapshots = SnapshotService()
        self.provider = build_provider()
        self.runner = TestRunner()
        self.diagnostics = DiagnosticService()
        self.checkpoints = CheckpointService()

    def run(self, request: JobRequest) -> PipelineResult:
        job_id = uuid4().hex[:12]

        workspace = self.workspaces.create(request.workspace)

        # If an older/internal caller does not provide task,
        # use the job name as the fallback task.
        task = request.task.strip() if request.task else request.name

        create_job(
            job_id,
            request.name,
            task,
            str(workspace),
        )

        seq = 0

        def event(name: str, message: str):
            nonlocal seq
            seq += 1
            add_event(job_id, seq, name, message)

        attempts = 0
        checkpoint = None

        # Default result in case execution fails before the first test.
        last_test = RunResult(
            passed=False,
            return_code=-1,
            stdout="",
            stderr="Pipeline did not reach test execution.",
        )

        try:
            update_job(
                job_id,
                "running",
                0,
                "Pipeline started.",
            )

            # PLAN
            event(
                "plan",
                f"Task accepted: {task}",
            )

            # SCAN
            scan_result = self.scanner.scan(workspace)
            event(
                "scan",
                str(scan_result),
            )

            # GENERATE
            generated = self.provider.generate(
                task,
                workspace,
            )

            event(
                "generate",
                generated.message,
            )

            # Create rollback snapshot after generation.
            snapshot = workspace.parent / f".snapshot-{job_id}"

            self.snapshots.create(
                workspace,
                snapshot,
            )

            max_attempts = request.effective_max_attempts

            while True:
                # TEST
                test_number = attempts + 1

                event(
                    "test",
                    f"Running test attempt {test_number}.",
                )

                last_test = self.runner.run(
                    workspace,
                    request.command,
                )

                # SUCCESS
                if last_test.passed:
                    checkpoint = self.checkpoints.save(
                        job_id,
                        workspace,
                        attempts,
                        "verified",
                    )

                    event(
                        "verify",
                        "Tests passed.",
                    )

                    event(
                        "checkpoint",
                        checkpoint,
                    )

                    message = "Verified: tests passed."

                    update_job(
                        job_id,
                        "verified",
                        attempts,
                        message,
                    )

                    return PipelineResult(
                        job_id=job_id,
                        workspace=workspace,
                        status="verified",
                        test=last_test,
                        attempts=attempts,
                        checkpoint=checkpoint,
                        message=message,
                    )

                # FAILURE: maximum fixes already used
                if attempts >= max_attempts:
                    event(
                        "verify",
                        "Maximum fix attempts reached.",
                    )

                    self.snapshots.rollback(
                        workspace,
                        snapshot,
                    )

                    message = (
                        "Verification failed; "
                        "workspace rolled back."
                    )

                    update_job(
                        job_id,
                        "failed",
                        attempts,
                        message,
                    )

                    return PipelineResult(
                        job_id=job_id,
                        workspace=workspace,
                        status="failed",
                        test=last_test,
                        attempts=attempts,
                        checkpoint=None,
                        message=message,
                    )

                # DIAGNOSE
                diagnosis = self.diagnostics.diagnose(
                    last_test.stdout,
                    last_test.stderr,
                )

                event(
                    "diagnose",
                    diagnosis.summary,
                )

                # One fix attempt has now been consumed.
                attempts += 1

                # FIX
                fixed = self.provider.fix(
                    task,
                    last_test.stdout + "\n" + last_test.stderr,
                    workspace,
                    diagnosis=diagnosis,
                )

                event(
                    "fix",
                    fixed.message,
                )

                # FIX FAILED
                if not fixed.ok:
                    self.snapshots.rollback(
                        workspace,
                        snapshot,
                    )

                    message = fixed.message

                    update_job(
                        job_id,
                        "failed",
                        attempts,
                        message,
                    )

                    return PipelineResult(
                        job_id=job_id,
                        workspace=workspace,
                        status="failed",
                        test=last_test,
                        attempts=attempts,
                        checkpoint=None,
                        message=message,
                    )

                # Loop back to TEST.
                event(
                    "retest",
                    f"Fix applied. Retesting attempt {attempts + 1}.",
                )

        except Exception as exc:
            event(
                "error",
                str(exc),
            )

            update_job(
                job_id,
                "failed",
                attempts,
                f"Pipeline error: {exc}",
            )

            raise
