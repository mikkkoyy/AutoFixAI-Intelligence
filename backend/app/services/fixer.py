from pathlib import Path
from app.services.diagnostics import Diagnosis

class FixService:
    # Safe deterministic fixer used for v0.1 verification.
    # AI-generated fixes will later be connected through a controlled adapter.

    def apply(self, workspace: Path, diagnosis: Diagnosis) -> bool:
        marker = workspace / ".autofix_demo_fixable"
        target = workspace / "demo.py"

        if diagnosis.category == "test_failure" and marker.exists() and target.exists():
            target.write_text(
                "def add(a, b):\n"
                "    return a + b\n",
                encoding="utf-8",
            )
            marker.unlink()
            return True
        return False
