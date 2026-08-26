"""Runtime verification — NORMAL execution via the real installed OpenCode.

Runs the complete production path:
    ApprovalPipeline -> WorkerRouter -> REAL OpenCode CLI -> subtask
    verification -> top-level verification

No worker doubles are injected. This is the multi-subtask regression
(spec item 17): create alpha.txt, beta.txt, gamma.txt, then verify all three.

Usage:
    .venv\\Scripts\\python.exe scripts\\runtime_verify_normal.py [workspace]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from app.agents.autofix_task import AutoFixTask, COMPLETED
from app.agents.pipeline import ApprovalPipeline
from app.agents.worker_router import WorkerRouter

REQUEST = (
    "Create three files: alpha.txt, beta.txt, gamma.txt. "
    "Put the exact filename inside each file. Then verify all three files."
)


def main():
    if len(sys.argv) > 1:
        ws = Path(sys.argv[1])
        ws.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        ws = Path(tempfile.mkdtemp(prefix="autofix-normal-"))
        cleanup = True

    print("=" * 70)
    print("RUNTIME TEST 17 — NORMAL EXECUTION (real OpenCode)")
    print(f"workspace: {ws}")

    router = WorkerRouter()  # REAL routing authority — no injection
    status = router.discover_workers()
    for name, rec in status.items():
        print(f"  worker {name:9} available={rec.available} ({rec.detail})")
    if not status.get("opencode") or not status["opencode"].available:
        print("RESULT: OpenCode is not available on this machine — cannot run.")
        return 2

    finished = []
    statuses = []
    pipeline = ApprovalPipeline(REQUEST, str(ws), worker_router=router)
    pipeline.status_changed.connect(statuses.append)
    pipeline.pipeline_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    pipeline.run()

    task = pipeline.autofix_task
    print("-" * 70)
    print(f"task_id        : {task.task_id}")
    print(f"status         : {task.status}")
    print(f"verified       : {task.verified}")
    print(f"backend used   : {pipeline.backend_used}")
    print(f"recovery       : {task.recovery_attempts} attempt(s)")
    for s in task.subtasks:
        print(f"  {s.id} {s.status:10} worker={s.worker} verified={s.verified} :: {s.title}")
    print("worker history :")
    for e in task.worker_history:
        print(f"  - subtask={e.get('subtask')} worker={e['worker']} status={e['status']}"
              + (f" ({e['reason'][:60]})" if e.get("reason") else ""))

    files_ok = {}
    for name in ("alpha.txt", "beta.txt", "gamma.txt"):
        p = ws / name
        files_ok[name] = p.exists() and name.split(".")[0] in p.read_text(encoding="utf-8", errors="ignore")
        print(f"  file {name}: exists={p.exists()} content-ok={files_ok[name]}")

    checks = {
        "signal TRUE": bool(finished and finished[0][0] is True),
        "parent COMPLETED": task.status == COMPLETED,
        "4 subtasks": len(task.subtasks) == 4,
        "4 completed": sum(1 for s in task.subtasks if s.status == COMPLETED) == 4,
        "verification true": task.verified is True,
        "executed by opencode": pipeline.backend_used == "opencode",
        "files created correctly": all(files_ok.values()),
        "no fallback needed": all(
            e["status"] != "unavailable" or e["worker"] != "opencode"
            for e in task.worker_history),
    }
    ok = True
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"RESULT: {'ALL CHECKS PASSED' if ok else 'CHECKS FAILED'}")

    if cleanup:
        shutil.rmtree(ws, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
