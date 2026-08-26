from pathlib import Path
from uuid import uuid4
from app.config import WORKSPACES_DIR

class WorkspaceManager:
    def create(self, requested: str | None = None) -> Path:
        if requested:
            path = Path(requested).resolve()
            # Only allow caller-specified workspaces that are not the backend itself.
            if path == Path.cwd().resolve() or Path.cwd().resolve() in path.parents:
                raise ValueError("Requested workspace may not be inside the backend source tree.")
        else:
            path = WORKSPACES_DIR / uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_files(self, workspace: Path):
        return sorted(
            str(p.relative_to(workspace))
            for p in workspace.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
