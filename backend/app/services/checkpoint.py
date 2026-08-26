from pathlib import Path
from datetime import datetime, timezone
import json

class CheckpointService:
    def save(self, job_id: str, workspace: Path, attempts: int, status: str):
        path = workspace / ".autofix_checkpoint.json"
        path.write_text(json.dumps({
            "job_id": job_id,
            "status": status,
            "attempts": attempts,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        return str(path)
