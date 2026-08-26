from pathlib import Path
import sys
from app.services.pipeline import PipelineService
from app.schemas import JobRequest

def test_pipeline_fixes_demo_failure(tmp_path):
    (tmp_path / "demo.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (tmp_path / "test_demo.py").write_text(
        "from demo import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    (tmp_path / ".autofix_demo_fixable").write_text("1", encoding="utf-8")

    result = PipelineService().run(JobRequest(
        name="demo", workspace=str(tmp_path),
        command=[sys.executable, "-m", "pytest", "-q"],
        max_fix_attempts=2,
    ))
    assert result.status == "verified"
    assert result.test.passed is True
    assert result.attempts == 1
    assert result.checkpoint is not None

def test_pipeline_does_not_claim_verification_on_unfixable_failure(tmp_path):
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )
    result = PipelineService().run(JobRequest(
        name="bad", workspace=str(tmp_path),
        command=[sys.executable, "-m", "pytest", "-q"],
        max_fix_attempts=2,
    ))
    assert result.status == "failed"
    assert result.test.passed is False
    assert result.checkpoint is None
