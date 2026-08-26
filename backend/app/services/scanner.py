from pathlib import Path

class ProjectScanner:
    def scan(self, workspace: Path):
        files = []
        for p in workspace.rglob("*"):
            if not p.is_file() or ".git" in p.parts:
                continue
            files.append({
                "path": str(p.relative_to(workspace)),
                "suffix": p.suffix,
                "size": p.stat().st_size,
            })
        return {"file_count": len(files), "files": files[:500]}
