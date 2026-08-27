import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AIRA.autofix import (
    AnalysisError,
    AutoFixConfig,
    AutoFixEngine,
    ErrorMonitor,
    ErrorReport,
    PatchSafetyError,
)
from AIRA.autofix.analyzer import AIAnalyzer
from AIRA.autofix.fixer import (
    apply_patch,
    create_branch,
    current_branch,
    dirty_changes_for_file,
    get_dirty_files,
    head_commit,
    is_forbidden_path,
    is_repo_dirty,
    make_branch_name,
    validate_patch,
)
from AIRA.autofix.monitor import normalize_error
from AIRA.autofix.models import VerificationResult
from AIRA.autofix.rollback import RollbackManager
from AIRA.autofix.verifier import Verifier
from AIRA.intelligence import IntelligenceStore

BUG_CODE = "def add(a, b):\n    return a / b\n"

PATCH = json.dumps(
    {
        "edits": [
            {
                "path": "AIRA/core/bug.py",
                "replace": [{"old": "return a / b", "new": "return a + b"}],
            }
        ]
    }
)

ANALYSIS = json.dumps(
    {
        "root_cause": "ZeroDivisionError caused by dividing by b",
        "confidence": 0.9,
        "affected_files": ["AIRA/core/bug.py"],
        "fix_strategy": "Add b instead of dividing",
        "patch": PATCH,
        "targeted_tests": ["tests/test_bug.py::test_add"],
        "risk": "low",
    }
)


def make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    core = repo / "AIRA" / "core"
    core.mkdir(parents=True, exist_ok=True)
    tests = repo / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (core / "bug.py").write_text(BUG_CODE, encoding="utf-8")
    (tests / "test_bug.py").write_text("import pytest\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


class FakeAI:
    def __init__(self, response=ANALYSIS):
        self.response = response
        self.default_model = "fake-model"
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self.response


class PassingVerifier:
    def verify(self, test_name):
        return VerificationResult(
            targeted_test=test_name,
            targeted_passed=True,
            full_suite_passed=True,
            stdout="1 passed",
            stderr="",
            duration=0.1,
        )


class FailingVerifier:
    def verify(self, test_name):
        return VerificationResult(
            targeted_test=test_name,
            targeted_passed=False,
            full_suite_passed=False,
            stdout="",
            stderr="AssertionError: boom",
            duration=0.1,
        )


# ---- 1. ErrorReport creation ----


def test_error_report_creation():
    report = ErrorReport(error_type="ValueError", message="boom")
    assert report.error_type == "ValueError"
    assert report.message == "boom"
    assert report.timestamp
    data = report.to_dict()
    assert data["error_type"] == "ValueError"
    assert data["repository_path"] is None
    assert "ValueError" in report.error_signature


# ---- 2. traceback parsing ----


def test_traceback_parsing():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "D:/proj/tests/test_core.py", line 42, in test_models\n'
        "    assert result == expected\n"
        '  File "D:/proj/AIRA/core/models.py", line 31, in timestamp_now\n'
        "    return datetime.now(timezone.utc).isoformat()\n"
        "ValueError: invalid value for timezone\n"
    )
    report = normalize_error(traceback_text=tb)
    assert report.error_type == "ValueError"
    assert "invalid value" in report.message
    assert report.source_file
    assert report.source_line == 31
    assert report.test_name and report.test_name.endswith("::test_models")


def test_pytest_failure_node_parsing():
    report = normalize_error(pytest_failure="FAILED tests/test_core.py::test_models")
    assert report.test_name == "tests/test_core.py::test_models"
    assert report.error_type == "Error"


def test_log_line_parsing():
    report = normalize_error(log_line="[ERROR] aira.ai: ValueError: bad arg", command="pytest")
    assert report.error_type == "ValueError"
    assert report.command == "pytest"


def test_exception_normalization():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        report = normalize_error(exc=exc)
    assert report.error_type == "RuntimeError"
    assert "kaboom" in report.message
    assert "RuntimeError" in report.traceback


# ---- 3. secret redaction ----


def test_secret_redaction_from_env():
    os.environ["AUTOFIX_TEST_TOKEN"] = "super-secret-value-12345"
    try:
        tb = (
            "POST https://api.example.com failed\n"
            "Token: super-secret-value-12345\n"
            "ValueError: auth failed\n"
        )
        report = normalize_error(traceback_text=tb)
        assert "super-secret-value-12345" not in report.message
        assert "super-secret-value-12345" not in report.traceback
        assert "[REDACTED]" in report.traceback
    finally:
        os.environ.pop("AUTOFIX_TEST_TOKEN", None)


def test_secret_redaction_patterns():
    tb = 'OpenAI error: "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123"'
    report = normalize_error(traceback_text=tb)
    assert "sk-proj-AbCdEfGhIjK" not in report.traceback
    assert "[REDACTED]" in report.traceback


# ---- 4. malformed AI response ----


async def test_malformed_ai_response_rejected():
    class BadAI:
        async def chat(self, messages, **kwargs):
            return "I think the issue is in the loop. Good luck!"

    analyzer = AIAnalyzer(BadAI())
    report = ErrorReport(error_type="ZeroDivisionError", message="division by zero")
    with pytest.raises(AnalysisError):
        await analyzer.analyze(report)


async def test_partial_ai_response_rejected():
    class PartialAI:
        async def chat(self, messages, **kwargs):
            return json.dumps({"root_cause": "only a root cause"})

    analyzer = AIAnalyzer(PartialAI())
    report = ErrorReport(error_type="ZeroDivisionError", message="division by zero")
    with pytest.raises(AnalysisError) as excinfo:
        await analyzer.analyze(report)
    assert "missing required fields" in str(excinfo.value)


async def test_valid_ai_response_accepted():
    class GoodAI:
        async def chat(self, messages, **kwargs):
            return "./```json\n" + ANALYSIS + "\n```"

    analyzer = AIAnalyzer(GoodAI())
    report = ErrorReport(error_type="ZeroDivisionError", message="division by zero")
    proposal = await analyzer.analyze(report)
    assert proposal.root_cause == "ZeroDivisionError caused by dividing by b"
    assert proposal.risk == "low"
    assert proposal.affected_files == ["AIRA/core/bug.py"]
    assert "AIRA/core/bug.py" in proposal.patch


# ---- 6. unsafe path rejection ----


@pytest.mark.parametrize(
    "bad_path",
    [
        ".env",
        "AIRA/.env",
        "AIRA/../.env",
        "secrets.txt",
        "config/credentials.json",
        "AIRA/keys/id_rsa.key",
        "config/app.pem",
        ".git/config",
        ".venv/lib/x.py",
        "C:/Windows/system32/foo.dll",
        "C:/Users/evil/notes.txt",
    ],
)
def test_unsafe_paths_rejected(bad_path):
    assert is_forbidden_path(bad_path)


def test_safe_repo_paths_allowed():
    assert not is_forbidden_path("AIRA/core/bug.py")
    assert not is_forbidden_path("tests/test_bug.py")


def test_validate_patch_rejects_forbidden_path():
    patch = json.dumps(
        {"edits": [{"path": ".env", "replace": [{"old": "x", "new": "y"}]}]}
    )
    with pytest.raises(PatchSafetyError):
        validate_patch(patch, allowed_paths=["AIRA/", "tests/"])


def test_validate_patch_rejects_outside_allowed():
    patch = json.dumps(
        {"edits": [{"path": "main.py", "replace": [{"old": "x", "new": "y"}]}]}
    )
    with pytest.raises(PatchSafetyError):
        validate_patch(patch, allowed_paths=["AIRA/", "tests/"])


def test_validate_patch_approves_allowed_files(tmp_path):
    repo = make_git_repo(tmp_path)
    approved = validate_patch(PATCH, ["AIRA/", "tests/"], repo)
    assert approved == ["AIRA/core/bug.py"]


# ---- 7. dirty repository detection ----


def test_dirty_untracked_file_detected(tmp_path):
    repo = make_git_repo(tmp_path)
    assert is_repo_dirty(repo) is False
    assert get_dirty_files(repo) == []
    (repo / "scratch.txt").write_text("new", encoding="utf-8")
    assert is_repo_dirty(repo) is True
    assert "scratch.txt" in get_dirty_files(repo)


def test_dirty_tracked_file_detected(tmp_path):
    repo = make_git_repo(tmp_path)
    target = repo / "AIRA" / "core" / "bug.py"
    target.write_text(BUG_CODE + "x = 1\n", encoding="utf-8")
    assert dirty_changes_for_file(repo, "AIRA/core/bug.py") is True


def test_clean_file_not_dirty(tmp_path):
    repo = make_git_repo(tmp_path)
    assert dirty_changes_for_file(repo, "AIRA/core/bug.py") is False


# ---- 8. patch validation / application ----


def test_apply_patch_applies_edits(tmp_path):
    repo = make_git_repo(tmp_path)
    changed = apply_patch(PATCH, repo, ["AIRA/", "tests/"], raise_on_dirty=True)
    assert changed and changed[0].name == "bug.py"
    content = (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8")
    assert "return a + b" in content


def test_apply_patch_refuses_dirty_target(tmp_path):
    repo = make_git_repo(tmp_path)
    target = repo / "AIRA" / "core" / "bug.py"
    target.write_text(BUG_CODE + "user edit\n", encoding="utf-8")
    with pytest.raises(PatchSafetyError, match="pre-existing"):
        apply_patch(PATCH, repo, ["AIRA/", "tests/"], raise_on_dirty=True)


# ---- 9. targeted test execution ----


def test_verify_runs_targeted_then_full_suite(tmp_path):
    calls = []

    def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "1 passed", "")

    verifier = Verifier(repo_root=tmp_path, run_command=fake_run)
    result = verifier.verify("tests/test_core.py::test_models")
    assert result.targeted_test == "tests/test_core.py::test_models"
    assert result.targeted_passed is True
    assert result.full_suite_passed is True
    assert result.success is True
    assert calls[0][3] == "tests/test_core.py::test_models"
    assert calls[1][3] == "tests"


# ---- 10. full pytest execution ----


def test_verify_full_suite_failure(tmp_path):
    def fake_run(cmd, cwd, timeout):
        if cmd[3] == "tests":
            return subprocess.CompletedProcess(cmd, 1, "", "2 failed, 10 passed")
        return subprocess.CompletedProcess(cmd, 0, "1 passed", "")

    verifier = Verifier(repo_root=tmp_path, run_command=fake_run)
    result = verifier.verify("tests/test_core.py::test_models")
    assert result.targeted_passed is True
    assert result.full_suite_passed is False
    assert result.success is False
    assert "2 failed" in result.stderr


def test_verify_no_targeted_test_fails_safe(tmp_path):
    def fake_run(cmd, cwd, timeout):
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    verifier = Verifier(repo_root=tmp_path, run_command=fake_run)
    result = verifier.verify(None)
    assert result.targeted_passed is False
    assert result.success is False


# ---- 11. rollback ----


def test_rollback_restores_branch_and_files(tmp_path):
    repo = make_git_repo(tmp_path)
    original_branch = current_branch(repo)
    branch = make_branch_name("autofix")
    create_branch(repo, branch)
    target = repo / "AIRA" / "core" / "bug.py"
    target.write_text("modified by autofix\n" + BUG_CODE, encoding="utf-8")

    rollback = RollbackManager(repo)
    rollback.rollback(branch, original_branch, changed_files=[target])

    assert current_branch(repo) == original_branch
    assert "modified by autofix" not in target.read_text(encoding="utf-8")
    proc = subprocess.run(["git", "branch", "--list"], cwd=repo, capture_output=True, text=True)
    assert branch not in proc.stdout


def test_engine_safe_mode_rolls_back_on_failure(tmp_path):
    repo = make_git_repo(tmp_path)
    expected_after_rollback = (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8")
    original_branch = current_branch(repo)
    store = IntelligenceStore(tmp_path / "intel")

    async def run_engine():
        engine = AutoFixEngine(
            autofix_config=AutoFixConfig(mode="safe", max_attempts=1, allowed_paths=["AIRA/", "tests/"]),
            provider=FakeAI(),
            store=store,
            repo_root=repo,
            verifier=FailingVerifier(),
        )
        report = ErrorReport(
            error_type="ZeroDivisionError",
            message="division by zero",
            test_name="tests/test_bug.py::test_add",
        )
        return await engine.run(report, mode="safe")

    outcome = asyncio.run(run_engine())
    assert outcome.success is False
    assert outcome.verification is not None
    assert outcome.verification.targeted_passed is False
    assert current_branch(repo) == original_branch
    assert (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8") == expected_after_rollback

    upgrades = list(store.upgrades_dir.glob("*.json"))
    assert len(upgrades) >= 1
    data = json.loads(upgrades[0].read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["failed_test"] == "tests/test_bug.py::test_add"
    assert "boom" in (data.get("test_error") or "") or "boom" in data.get("error", "")


# ---- 12. successful fix recording ----


def test_engine_safe_mode_records_successful_fix(tmp_path):
    repo = make_git_repo(tmp_path)
    store = IntelligenceStore(tmp_path / "intel")

    async def run_engine():
        engine = AutoFixEngine(
            autofix_config=AutoFixConfig(mode="safe", max_attempts=1, allowed_paths=["AIRA/", "tests/"]),
            provider=FakeAI(),
            store=store,
            repo_root=repo,
            verifier=PassingVerifier(),
        )
        report = ErrorReport(
            error_type="ZeroDivisionError",
            message="division by zero",
            test_name="tests/test_bug.py::test_add",
        )
        return await engine.run(report, mode="safe")

    outcome = asyncio.run(run_engine())
    assert outcome.success is True
    assert outcome.commit
    assert outcome.branch and "autofix" in outcome.branch
    content = (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8")
    assert "return a + b" in content

    fixes = store.list_fixes()
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix["verified"] is True
    assert fix["error_type"] == "ZeroDivisionError"
    assert fix["affected_files"] == ["AIRA/core/bug.py"]
    assert fix["tests"] == ["tests/test_bug.py::test_add"]
    assert fix["provider"] == "fakeai"
    assert fix["commit"]
    assert fix["timestamp"]


# ---- 13. failed fix recording via intelligence search ----


def test_successful_fix_is_searchable(tmp_path):
    store = IntelligenceStore(tmp_path / "intel")
    store.save_fix(
        {
            "error_signature": "ZeroDivisionError: division by zero @ AIRA/core/bug.py",
            "root_cause": "Dividing by zero",
            "affected_files": ["AIRA/core/bug.py"],
            "verified": True,
        }
    )
    results = store.search_fixes(error_type="ZeroDivisionError")
    assert len(results) == 1
    assert results[0]["verified"] is True

    results = store.search_fixes(query="division by zero")
    assert len(results) == 1


def test_suggest_mode_does_not_modify_files(tmp_path):
    repo = make_git_repo(tmp_path)
    before = (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8")

    async def run_engine():
        engine = AutoFixEngine(
            autofix_config=AutoFixConfig(mode="suggest", max_attempts=1, allowed_paths=["AIRA/", "tests/"]),
            provider=FakeAI(),
            store=IntelligenceStore(tmp_path / "intel"),
            repo_root=repo,
            verifier=PassingVerifier(),
        )
        report = ErrorReport(
            error_type="ZeroDivisionError",
            message="division by zero",
            test_name="tests/test_bug.py::test_add",
        )
        return await engine.run(report, mode="suggest")

    outcome = asyncio.run(run_engine())
    assert outcome.proposal is not None
    assert outcome.success is False
    assert outcome.verification is None
    assert (repo / "AIRA" / "core" / "bug.py").read_text(encoding="utf-8") == before
    assert not list((tmp_path / "intel" / "fixes").glob("*.json"))