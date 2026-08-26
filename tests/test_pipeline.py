"""ApprovalPipeline tests — honest success/failure semantics.

The pipeline must never claim success unless the coding agent actually ran
and verification actually passed.
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.coding_agent import CodingResult
from app.agents.pipeline import ApprovalPipeline
from app.builder.project_builder import ProjectBuilder


class FakeRunner:
    """Injectable CodingAgentRunner double."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "workspace": str(workspace)})
        if on_output:
            on_output("fake coding output")
        return self._result


def run_pipeline(pipeline):
    stages = []
    finished = []
    pipeline.stage_finished.connect(lambda label, ok, msg: stages.append((label, ok)))
    pipeline.pipeline_finished.connect(
        lambda success, summary: finished.append((success, summary))
    )
    pipeline.run()
    return stages, finished


def make_workspace_with_passing_tests(tmp_path):
    """A tiny project whose tests genuinely pass under pytest."""
    (tmp_path / "mathlib.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "test_mathlib.py").write_text(
        "from mathlib import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tmp_path


class TestHonestFailure:
    def test_no_coding_agent_means_failure(self, tmp_path):
        runner = FakeRunner(
            CodingResult(
                backend=None,
                success=False,
                error="No coding agent available.",
            )
        )
        pipeline = ApprovalPipeline("do work", str(tmp_path), coding_runner=runner)
        stages, finished = run_pipeline(pipeline)

        labels = [s[0] for s in stages]
        assert "Planner" in labels

        assert len(finished) == 1
        success, summary = finished[0]
        assert success is False
        assert "No coding agent" in summary or "FAILED" in summary

    def test_failing_tests_block_success(self, tmp_path):
        (tmp_path / "test_broken.py").write_text(
            "def test_always_fails():\n    assert False\n", encoding="utf-8"
        )

        runner = FakeRunner(
            CodingResult(backend="opencode", success=True, output="done")
        )
        pipeline = ApprovalPipeline("do work", str(tmp_path), coding_runner=runner)
        stages, finished = run_pipeline(pipeline)

        labels = [s[0] for s in stages]
        stage_map = {}
        for label, ok in stages:
            stage_map[label] = ok

        assert "Tester" in labels
        assert stage_map.get("Tester") is False
        assert "Debugger" in labels  # debugger engaged on failure
        assert stage_map.get("Verification") is False

        success, _summary = finished[0]
        assert success is False


class SubtaskRunner:
    def __init__(self):
        self.calls = []

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "workspace": str(workspace)})
        if on_output:
            on_output(f"executing: {prompt[:80]}")
        root = __import__("pathlib").Path(workspace)
        matches = re.findall(r"[A-Za-z0-9_.-]+\.(?:txt|json|py)", prompt)
        unique_matches = []
        for match in matches:
            if match not in unique_matches:
                unique_matches.append(match)
        for name in unique_matches:
            if name.endswith(".txt"):
                (root / name).write_text(name.split(".", 1)[0], encoding="utf-8")
            elif name.endswith(".json"):
                (root / name).write_text('{"name": "' + name + '"}', encoding="utf-8")
            elif name.endswith(".py"):
                (root / name).write_text(
                    "def run():\n    return '" + name + "'\n",
                    encoding="utf-8",
                )

        if "verify" in prompt.lower() or "verification" in prompt.lower():
            ok = True
            for name in unique_matches:
                path = root / name
                if not path.exists():
                    ok = False
                elif name.endswith(".txt") and name.split(".", 1)[0].lower() not in path.read_text(encoding="utf-8", errors="ignore").lower():
                    ok = False
            return CodingResult(backend="opencode", success=ok, output="verification run", error="verification failed" if not ok else "")
        return CodingResult(backend="opencode", success=True, output="subtask executed")


class TestHonestSuccess:
    def test_verified_project_reports_success(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)

        runner = FakeRunner(
            CodingResult(backend="opencode", success=True, output="edited files")
        )
        pipeline = ApprovalPipeline("add feature", str(workspace), coding_runner=runner)
        stages, finished = run_pipeline(pipeline)

        stage_map = dict(stages)
        assert stage_map.get("Coding") is True
        assert stage_map.get("Tester") is True
        assert stage_map.get("Verification") is True

        assert pipeline.backend_used == "opencode"

        success, summary = finished[0]
        assert success is True
        assert "PASSED" in summary

    def test_pipeline_uses_active_workspace(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = FakeRunner(
            CodingResult(backend="aider", success=True, output="")
        )
        pipeline = ApprovalPipeline("task", str(workspace), coding_runner=runner)
        run_pipeline(pipeline)

        assert runner.calls[0]["workspace"] == str(workspace)

    def test_pipeline_creates_task_decomposition(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = FakeRunner(
            CodingResult(backend="opencode", success=True, output="edited files")
        )
        pipeline = ApprovalPipeline(
            "Create three text files alpha.txt, beta.txt, gamma.txt and verify them.",
            str(workspace),
            coding_runner=runner,
        )
        run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert task is not None
        assert len(task.subtasks) >= 4
        assert task.agent_assignments
        assert task.dependencies

    def test_pipeline_executes_real_subtasks(self, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        runner = SubtaskRunner()
        pipeline = ApprovalPipeline(
            "Create three files: alpha.txt, beta.txt, gamma.txt. Put the exact filename inside each file. Then verify all three files.",
            str(workspace),
            coding_runner=runner,
        )
        stages, finished = run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert task is not None
        assert len(task.subtasks) >= 4
        completed = [subtask for subtask in task.subtasks if subtask.status == "COMPLETED"]
        assert len(completed) >= 4
        assert task.verified is True
        assert task.status == "COMPLETED"
        assert all((workspace / name).exists() for name in ("alpha.txt", "beta.txt", "gamma.txt"))
        assert finished[0][0] is True
        assert any("Subtask" in step for step in task.completed_steps)


class TestStageOrder:
    def test_stages_run_in_required_order(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = FakeRunner(
            CodingResult(backend="opencode", success=True, output="")
        )
        pipeline = ApprovalPipeline("task", str(workspace), coding_runner=runner)
        stages, _finished = run_pipeline(pipeline)

        labels = [s[0] for s in stages]
        # Planner → Coding → Tester → Reviewer → Verification
        # (Debugger only appears when tests fail)
        assert labels[:3] == ["Planner", "Coding", "Tester"]
        assert labels[-2:] == ["Reviewer", "Verification"]
        assert "Debugger" not in labels


class TestBuilderIntegration:
    def test_built_project_verifies(self, tmp_path):
        result = ProjectBuilder().build_python_project(tmp_path, "PipeDemo")
        assert result.success
