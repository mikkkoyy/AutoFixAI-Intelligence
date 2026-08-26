import ast
from pathlib import Path


class ProjectVerifier:
    """Deterministic verification layer for generated projects."""

    def verify_python_files(self, project: Path) -> list[str]:
        errors = []
        for path in project.rglob("*.py"):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def verify_structure(self, project: Path) -> list[str]:
        required = [project / "src", project / "tests"]
        return [f"Missing: {path}" for path in required if not path.exists()]

    def verify(self, project: Path) -> list[str]:
        return self.verify_structure(project) + self.verify_python_files(project)
