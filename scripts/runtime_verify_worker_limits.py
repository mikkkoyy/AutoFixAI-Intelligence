"""Runtime verification: Worker Limit Failover scenarios.

Runs 11 end-to-end scenarios that exercise the full pipeline with
deterministic worker doubles (never real CLIs).  Every scenario is fully
self-contained and leaves no side-effects.

Usage:
    .venv\\Scripts\\python scripts\\runtime_verify_worker_limits.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_root = str(Path(__file__).resolve().parents[1] / "frontend")
if _root not in sys.path:
    sys.path.insert(0, _root)

from app.agents.autofix_task import AutoFixTask, COMPLETED, FAILED
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.github_knowledge import (
    KnowledgeItem,
    cleanup_local_knowledge,
    list_pending_knowledge,
    pop_pending_knowledge,
    save_pending_knowledge,
)
from app.agents.pipeline import ApprovalPipeline
from app.agents.worker_notifications import EVENT_QUOTA_EXCEEDED, EVENT_TIMEOUT
from app.agents.worker_router import WorkerRouter


class FakeWorker:
    def __init__(self, name, available=True, script=None):
        self.name = name
        self._available = available
        self._script = list(script or [])
        self.calls = []

    def discover(self):
        return BackendInfo(self.name, self._available,
                          f"{self.name}-exe" if self._available else None, "fake")

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "timeout": timeout})
        if not self._script:
            raise AssertionError(f"{self.name} ran out of scripted results")
        item = self._script.pop(0)
        if callable(item):
            return item(prompt, Path(workspace))
        return item


def writing_worker(name):
    def run(prompt, root):
        root = Path(root)
        for fname in dict.fromkeys(re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt)):
            (root / fname).write_text(fname.split(".", 1)[0], encoding="utf-8")
        return CodingResult(backend=name, success=True, output="subtask executed")
    return lambda: FakeWorker(name, available=True, script=[run] * 20)


def unavailable(name):
    return lambda: FakeWorker(name, available=False)


def router_with(opencode, deepseek, copilot, ollama=None, on_notification=None, env=None):
    factories = {
        "opencode": opencode,
        "deepseek": deepseek,
        "copilot": copilot,
        "ollama": ollama or unavailable("ollama"),
    }
    return WorkerRouter(
        worker_factories=factories,
        env=env or os.environ,
        on_notification=on_notification,
    )


def run_pipeline(pipeline):
    finished = []
    pipeline.pipeline_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    pipeline.run()
    return finished


passed = 0
failed = 0
total = 11


def scenario(num, name):
    def decorator(fn):
        def wrapper():
            global passed, failed
            try:
                fn()
                passed += 1
                print(f"  PASS  [{num:2d}/{total}] {name}")
            except Exception as exc:
                failed += 1
                print(f"  FAIL  [{num:2d}/{total}] {name}: {exc}")
        return wrapper
    return decorator


THREE_FILES = (
    "Create three files: alpha.txt, beta.txt, gamma.txt. "
    "Put the exact filename inside each file. Then verify all three files."
)


@scenario(1, "QUOTA_EXCEEDED classification and notification")
def scenario_1():
    with tempfile.TemporaryDirectory(prefix="rl1_") as tmp:
        tmp_path = Path(tmp)
        notifications = []
        ds = FakeWorker("deepseek", available=True)
        ds._script = [
            lambda p, r: CodingResult(backend="deepseek", success=False, started=True,
                                       error="HTTP 429: rate limit exceeded")
        ] * 4
        co = writing_worker("copilot")()
        router = router_with(unavailable("opencode"), lambda: ds, lambda: co)
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        pipeline.worker_notification.connect(lambda n: notifications.append(n))
        finished = run_pipeline(pipeline)
        assert finished[0][0] is True, f"Pipeline failed: {finished}"
        events = [e["event_type"] for e in notifications]
        assert EVENT_QUOTA_EXCEEDED in events, "QUOTA_EXCEEDED notification not emitted"


@scenario(2, "TIMEOUT classification and fallback")
def scenario_2():
    with tempfile.TemporaryDirectory(prefix="rl2_") as tmp:
        tmp_path = Path(tmp)
        oc = FakeWorker("opencode", available=True)
        oc._script = [
            lambda p, r: CodingResult(backend="opencode", success=False, timed_out=True,
                                       error="opencode exceeded timeout")
        ] * 4
        ds = writing_worker("deepseek")()
        router = router_with(lambda: oc, lambda: ds, unavailable("copilot"))
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        finished = run_pipeline(pipeline)
        assert finished[0][0] is True
        assert pipeline.autofix_task.verified is True


@scenario(3, "AUTOFIX_WORKER_TIMEOUT env config passed to workers")
def scenario_3():
    with tempfile.TemporaryDirectory(prefix="rl3_") as tmp:
        tmp_path = Path(tmp)
        captured = []
        env = {**os.environ, "AUTOFIX_WORKER_TIMEOUT": "42"}

        def capture_run(prompt, root):
            captured.append(True)
            return CodingResult(backend="opencode", success=True, output="ok")

        oc = FakeWorker("opencode", available=True, script=[capture_run])
        router = router_with(lambda: oc, unavailable("deepseek"), unavailable("copilot"), env=env)
        router.execute("p", str(tmp_path))
        assert len(captured) == 1
        assert oc.calls[0]["timeout"] == 42


@scenario(4, "QUOTA_EXCEEDED marks worker down persistently")
def scenario_4():
    with tempfile.TemporaryDirectory(prefix="rl4_") as tmp:
        tmp_path = Path(tmp)
        ds = FakeWorker("deepseek", available=True)
        ds._script = [
            lambda p, r: CodingResult(backend="deepseek", success=False, started=True,
                                       error="insufficient quota for model")
        ] * 4
        router = router_with(unavailable("opencode"), lambda: ds, unavailable("copilot"))
        task = AutoFixTask.create(tmp_path, "task")
        router.execute("p", str(tmp_path), task=task, subtask_id="s1")
        record = router.discover_workers()["deepseek"]
        assert record.available is False
        assert record.configured is False


@scenario(5, "Ollama discoverable but not tried by default")
def scenario_5():
    with tempfile.TemporaryDirectory(prefix="rl5_") as tmp:
        tmp_path = Path(tmp)
        ollama = FakeWorker("ollama", available=True)
        router = router_with(unavailable("opencode"), unavailable("deepseek"), unavailable("copilot"))
        router._factories["ollama"] = lambda: ollama
        records = router.discover_workers()
        assert "ollama" in records
        assert records["ollama"].available is True
        result = router.execute("p", str(tmp_path))
        assert result.success is False
        assert ollama.calls == []


@scenario(6, "Ollama reachable via custom priority")
def scenario_6():
    with tempfile.TemporaryDirectory(prefix="rl6_") as tmp:
        tmp_path = Path(tmp)
        ollama = FakeWorker("ollama", available=True)
        ollama._script = [
            lambda p, r: CodingResult(backend="ollama", success=True, output="done")
        ]
        router = router_with(unavailable("opencode"), unavailable("deepseek"), unavailable("copilot"))
        router._factories["ollama"] = lambda: ollama
        router.priority = ("ollama",)
        result = router.execute("p", str(tmp_path))
        assert result.success is True
        assert result.worker_name == "ollama"


@scenario(7, "Mixed auth + quota + success scenario")
def scenario_7():
    with tempfile.TemporaryDirectory(prefix="rl7_") as tmp:
        tmp_path = Path(tmp)
        oc = FakeWorker("opencode", available=True)
        oc._script = [
            lambda p, r: CodingResult(backend="opencode", success=False, started=True,
                                       error="unauthorized 401")
        ] * 4
        ds = FakeWorker("deepseek", available=True)
        ds._script = [
            lambda p, r: CodingResult(backend="deepseek", success=False, started=True,
                                       error="HTTP 429: rate limit exceeded")
        ] * 4
        co = writing_worker("copilot")()
        router = router_with(lambda: oc, lambda: ds, lambda: co)
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        finished = run_pipeline(pipeline)
        assert finished[0][0] is True
        task = pipeline.autofix_task
        workers_used = {e["worker"] for e in task.worker_history if e["status"] == "completed"}
        assert "copilot" in workers_used


@scenario(8, "Notification security: quota never leaks secrets")
def scenario_8():
    with tempfile.TemporaryDirectory(prefix="rl8_") as tmp:
        tmp_path = Path(tmp)
        SECRET = "sk-super-secret-value-9999"
        notifications = []
        ds = FakeWorker("deepseek", available=True)
        ds._script = [
            lambda p, r: CodingResult(backend="deepseek", success=False, started=True,
                                       error=f"Bearer {SECRET} rate limit exceeded")
        ] * 4
        router = router_with(unavailable("opencode"), lambda: ds, unavailable("copilot"),
                             on_notification=lambda n: notifications.append(n))
        router.execute("p", str(tmp_path))
        blob = json.dumps([n.to_dict() for n in notifications])
        assert SECRET not in blob, "Secret leaked in notification"


@scenario(9, "Pending knowledge save when GitHub unavailable")
def scenario_9():
    with tempfile.TemporaryDirectory(prefix="rl9_") as tmp:
        tmp_path = Path(tmp)
        cleanup_local_knowledge(str(tmp_path))
        item = KnowledgeItem(
            category="lessons", title="Test lesson", body="- Test body",
            source="test", confidence=0.8,
        )
        result = save_pending_knowledge(item, workspace=str(tmp_path))
        assert result["ok"] is True
        pending = list_pending_knowledge(str(tmp_path))
        assert len(pending) == 1
        assert pending[0]["item"]["title"] == "Test lesson"
        cleanup_local_knowledge(str(tmp_path))


@scenario(10, "Pop pending knowledge clears files")
def scenario_10():
    with tempfile.TemporaryDirectory(prefix="rl10_") as tmp:
        tmp_path = Path(tmp)
        cleanup_local_knowledge(str(tmp_path))
        item = KnowledgeItem(
            category="patterns", title="Pop test", body="- Body",
            confidence=0.7,
        )
        save_pending_knowledge(item, workspace=str(tmp_path))
        assert len(list_pending_knowledge(str(tmp_path))) == 1
        popped = pop_pending_knowledge(str(tmp_path))
        assert len(popped) == 1
        assert len(list_pending_knowledge(str(tmp_path))) == 0
        cleanup_local_knowledge(str(tmp_path))


@scenario(11, "End-to-end: all workers fail with quota -> honest failure")
def scenario_11():
    with tempfile.TemporaryDirectory(prefix="rl11_") as tmp:
        tmp_path = Path(tmp)
        def quota_run(prompt, root):
            return CodingResult(backend="x", success=False, error="credits exhausted")
        router = router_with(
            lambda: FakeWorker("opencode", available=True, script=[quota_run] * 4),
            lambda: FakeWorker("deepseek", available=True, script=[quota_run] * 4),
            lambda: FakeWorker("copilot", available=True, script=[quota_run] * 4),
        )
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        finished = run_pipeline(pipeline)
        assert finished[0][0] is False
        task = pipeline.autofix_task
        assert task.status != COMPLETED
        quota_statuses = [e for e in task.worker_history if e["status"] == "quota_exceeded"]
        assert len(quota_statuses) == 3


ALL_SCENARIOS = [
    scenario_1, scenario_2, scenario_3, scenario_4, scenario_5,
    scenario_6, scenario_7, scenario_8, scenario_9, scenario_10,
    scenario_11,
]

if __name__ == "__main__":
    print(f"\nRuntime verification: Worker Limit Failover ({total} scenarios)")
    print("=" * 60)
    passed = 0
    failed = 0
    for fn in ALL_SCENARIOS:
        fn()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {total}")
    sys.exit(0 if failed == 0 else 1)
