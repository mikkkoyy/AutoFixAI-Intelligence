import asyncio
from pathlib import Path
from typing import Optional
from AIRA.core.memory_db import memory_db
from AIRA.core.logging import get_logger
from AIRA.core.models import generate_id, timestamp_now

logger = get_logger("git")

INTELLIGENCE_REPO = "https://github.com/mikkkoyy/AutoFixAI-Intelligence.git"
INTELLIGENCE_DIR = Path(__file__).parent.parent.parent / "intelligence_repo"


class IntelligenceSync:
    def __init__(self, repo_url: str = None):
        self.repo_url = repo_url or INTELLIGENCE_REPO
        self.repo_dir = INTELLIGENCE_DIR

    async def _run_git(self, *args) -> tuple[int, str, str]:
        cmd = "git " + " ".join(args)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.repo_dir) if self.repo_dir.exists() else None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    async def clone_repo(self) -> bool:
        try:
            if self.repo_dir.exists():
                logger.info("Intelligence repo already exists, pulling latest")
                return await self.pull()
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            rc, out, err = await self._run_git("clone", self.repo_url, str(self.repo_dir))
            if rc != 0:
                logger.error(f"Clone failed: {err}")
                return False
            logger.info("Intelligence repo cloned successfully")
            return True
        except Exception as e:
            logger.error(f"Clone error: {e}")
            return False

    async def pull(self) -> bool:
        try:
            rc, out, err = await self._run_git("pull")
            if rc != 0:
                logger.error(f"Pull failed: {err}")
                return False
            logger.info("Intelligence repo updated")
            return True
        except Exception as e:
            logger.error(f"Pull error: {e}")
            return False

    async def push_local_intelligence(self, message: str = None) -> dict:
        if not self.repo_dir.exists():
            cloned = await self.clone_repo()
            if not cloned:
                return {"success": False, "error": "Failed to clone intelligence repo"}

        try:
            await self._run_git("add", ".")

            rc, status_out, _ = await self._run_git("status", "--porcelain")
            if not status_out.strip():
                return {"success": True, "message": "No changes to commit"}

            commit_msg = message or f"AIRA Intelligence Update - {timestamp_now()}"
            rc, out, err = await self._run_git("commit", "-m", commit_msg)
            if rc != 0:
                return {"success": False, "error": f"Commit failed: {err}"}

            rc, out, err = await self._run_git("push")
            if rc != 0:
                return {"success": False, "error": f"Push failed: {err}"}

            rc, hash_out, _ = await self._run_git("rev-parse", "HEAD")
            commit_hash = hash_out.strip()

            await memory_db.record_sync("push", "success", commit_hash, commit_msg)
            logger.info(f"Intelligence pushed: {commit_hash}")

            return {"success": True, "commit_hash": commit_hash, "message": commit_msg}
        except Exception as e:
            logger.error(f"Push error: {e}")
            await memory_db.record_sync("push", "failed", message=str(e))
            return {"success": False, "error": str(e)}

    async def sync_knowledge_to_repo(self) -> dict:
        try:
            knowledge = await memory_db.search_knowledge(state="verified", limit=100)
            if not knowledge:
                return {"success": True, "message": "No verified knowledge to sync"}

            if not self.repo_dir.exists():
                await self.clone_repo()

            knowledge_dir = self.repo_dir / "knowledge"
            knowledge_dir.mkdir(exist_ok=True)

            for k in knowledge:
                import json
                file_path = knowledge_dir / f"{k['id'][:8]}.json"
                file_path.write_text(json.dumps({
                    "title": k["title"],
                    "category": k["category"],
                    "problem": k.get("problem"),
                    "solution": k["solution"],
                    "tags": json.loads(k["tags"]) if isinstance(k["tags"], str) else k["tags"],
                    "confidence": k["confidence"],
                    "state": k["state"],
                }, indent=2), encoding="utf-8")

            return await self.push_local_intelligence(
                f"AIRA Knowledge Sync - {len(knowledge)} entries"
            )
        except Exception as e:
            logger.error(f"Knowledge sync error: {e}")
            return {"success": False, "error": str(e)}

    async def get_repo_status(self) -> dict:
        if not self.repo_dir.exists():
            return {"exists": False, "cloned": False}
        rc, out, _ = await self._run_git("status", "--porcelain")
        rc2, branch, _ = await self._run_git("branch", "--show-current")
        rc3, log_out, _ = await self._run_git("log", "--oneline", "-5")
        return {
            "exists": True,
            "cloned": True,
            "branch": branch.strip() if rc2 == 0 else "unknown",
            "modified_files": len(out.strip().split("\n")) if out.strip() else 0,
            "recent_commits": log_out.strip().split("\n") if log_out.strip() else [],
        }
