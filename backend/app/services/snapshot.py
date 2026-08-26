from pathlib import Path
import shutil

class SnapshotService:
    def create(self, workspace: Path, destination: Path):
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        shutil.copytree(workspace, destination / "files", dirs_exist_ok=True)

    def rollback(self, workspace: Path, snapshot: Path):
        source = snapshot / "files"
        if not source.exists():
            raise ValueError("Snapshot is invalid.")
        for p in workspace.iterdir():
            if p.name == ".autofix_checkpoint.json":
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        shutil.copytree(source, workspace, dirs_exist_ok=True)
