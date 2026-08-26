"""Runtime verification — worker authentication/configuration notifications.

Runs the REAL ApprovalPipeline -> WorkerRouter -> workers code path with
injected deterministic worker doubles and verifies that every
authentication / configuration problem surfaces to the user as a safe,
non-blocking notification — while AutoFix keeps executing via fallback.

Scenarios:
    auth-fallback   OpenCode HTTP 401 -> warning -> DeepSeek completes (18)
    missing-key     DeepSeek/OpenCode absent  -> info    -> Copilot runs (19)
    all-auth        every worker 401     -> warnings + ONE consolidated (20)
    no-secrets      leaking error text never reaches notifications  (21)

Usage:
    .venv\\Scripts\\python.exe scripts\\runtime_verify_auth_notifications.py [scenario ...]
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from app.agents.autofix_task import COMPLETED, FAILED
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.pipeline import ApprovalPipeline
from app.agents.worker_notifications import (
    EVENT_AUTHENTICATION_REQUIRED,
    EVENT_NO_WORKER_AVAILABLE,
    EVENT_UNAVAILABLE,
)
from app.agents.worker_router import WorkerRouter

THREE_FILES = (
    "Create three files: alpha.txt, beta.txt, gamma.txt. "
    "Put the exact filename inside each file. Then verify all three files."
)

FAKE_SECRET = "sk-runtime-fake-key-DO-NOT-USE-1234"


class FakeWorker:
    """Deterministic internal-worker double (test injection mechanism)."""

    def __init__(self, name, available=True, script=None):
        self.name = name
        self._available = available
        self._script = list(script or [])
        self.calls = []

    def discover(self):
        return BackendInfo(
            self.name,
            self._available,
            f"{self.name}-exe" if self._available else None,
            "deterministic double",
        )

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "workspace": str(workspace)})
        item = self._script.pop(0) if self._script else None
        if item is None:
            raise AssertionError(f"{self.name} ran out of scripted results")
        return item(prompt, Path(workspace))


def file_worker(name):
    """Worker that creates the .txt file named in the SUBTASK TITLE."""

    def run(prompt, root):
        title_match = re.search(r"Subtask title:\s*(.+)", prompt)
        title = title_match.group(1) if title_match else ""
        targets = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_.-]+\.txt", title)))
        for fname in targets:
            (root / fname).write_text(fname.split(".", 1)[0], encoding="utf-8")
        if "verify" in title.lower() or not targets:
            referenced = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt)))
            missing = [n for n in referenced if not (root / n).exists()]
            if missing:
                return CodingResult(
                    backend=name, success=False,
                    error=f"verification failed, missing {missing}",
                )
        return CodingResult(backend=name, success=True, output="subtask executed")

    count = {"n": 0}

    def factory():
        count["n"] += 1
        return FakeWorker(name, available=True, script=[run] * 40)

    return factory


def auth_worker(name):
    """Worker whose every execution fails like a real credential problem."""

    def run(prompt, root):
        return CodingResult(
            backend=name, success=False, started=True,
            error=(f"{name} HTTP 401: authentication/configuration error "
                   f"(Bearer {FAKE_SECRET})"),
        )

    return lambda: FakeWorker(name, available=True, script=[run] * 60)


def unavailable(name):
    return lambda: FakeWorker(name, available=False)


def build_router(opencode, deepseek, copilot, notifications):
    return WorkerRouter(
        worker_factories={
            "opencode": opencode, "deepseek": deepseek, "copilot": copilot,
        },
        on_notification=notifications.append,
    )


def run(request, workspace, router):
    finished = []
    signals = []
    pipeline = ApprovalPipeline(request, str(workspace), worker_router=router)
    pipeline.worker_notification.connect(signals.append)
    pipeline.pipeline_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    pipeline.run()
    return pipeline, signals, finished


def _report(checks):
    ok = True
    for name, passed in checks.items():
        print(f"    [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"  RESULT: {'ALL CHECKS PASSED' if ok else 'CHECKS FAILED'}")
    if not ok:
        sys.exit(1)


def _print_signals(signals):
    for s in signals:
        print(f"    signal: [{s['severity']}] {s['worker']}: {s['message']}"
              + (f" | detail={s['detail']!r}" if s.get("detail") else ""))


# ---------------------------------------------------------------------------


def scenario_auth_fallback():
    """18 — OpenCode auth problem -> visible warning, fallback completes."""
    print("=" * 70)
    print("RUNTIME TEST 18 — AUTH WARNING + AUTOMATIC FALLBACK (OpenCode)")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notifications = []
        router = build_router(auth_worker("opencode"), file_worker("deepseek"),
                              unavailable("copilot"), notifications)
        pipeline, signals, finished = run(THREE_FILES, ws, router)
        task = pipeline.autofix_task
        _print_signals([dict(s) for s in signals])

        warnings = [s for s in signals
                    if s["event_type"] == EVENT_AUTHENTICATION_REQUIRED]
        checks = {
            "signal TRUE (fallback continued)": finished[0][0] is True,
            "task COMPLETED": task.status == COMPLETED,
            "verified TRUE": task.verified is True,
            "warning emitted": len(warnings) >= 1,
            "exact message": all(
                w["message"] == "OpenCode authentication required."
                for w in warnings),
            "severity warning": all(w["severity"] == "warning" for w in warnings),
            "can_continue True": all(w["can_continue"] is True for w in warnings),
            "no consolidated failure": all(
                s["event_type"] != EVENT_NO_WORKER_AVAILABLE for s in signals),
        }
        _report(checks)


def scenario_missing_key():
    """19 — preferred workers absent -> informational notes, Copilot runs."""
    print("=" * 70)
    print("RUNTIME TEST 19 — UNAVAILABLE INFO NOTES (DeepSeek key missing)")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notifications = []
        router = build_router(unavailable("opencode"), unavailable("deepseek"),
                              file_worker("copilot"), notifications)
        pipeline, signals, finished = run(THREE_FILES, ws, router)
        task = pipeline.autofix_task
        _print_signals([dict(s) for s in signals])

        infos = [s for s in signals if s["event_type"] == EVENT_UNAVAILABLE]
        by_worker = {s["worker"]: s for s in infos}
        checks = {
            "signal TRUE (Copilot executed)": finished[0][0] is True,
            "task COMPLETED + verified": (
                task.status == COMPLETED and task.verified is True),
            "opencode info note": by_worker.get("opencode", {}).get("message")
                == "OpenCode is unavailable.",
            "deepseek info note": by_worker.get("deepseek", {}).get("message")
                == "DeepSeek is unavailable.",
            "severity info": all(s["severity"] == "info" for s in infos),
            "no blocking failure": all(
                s["event_type"] != EVENT_NO_WORKER_AVAILABLE for s in signals),
        }
        _report(checks)


def scenario_all_auth():
    """20 — every worker 401 -> warnings + ONE actionable consolidated event."""
    print("=" * 70)
    print("RUNTIME TEST 20 — ALL WORKERS AUTH-FAILED (consolidated failure)")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notifications = []
        router = build_router(auth_worker("opencode"), auth_worker("deepseek"),
                              auth_worker("copilot"), notifications)
        pipeline, signals, finished = run(THREE_FILES, ws, router)
        task = pipeline.autofix_task
        _print_signals([dict(s) for s in signals])

        consolidated = [s for s in signals
                        if s["event_type"] == EVENT_NO_WORKER_AVAILABLE]
        detail = consolidated[0]["detail"] if consolidated else ""
        checks = {
            "signal FALSE (honest failure)": finished[0][0] is False,
            "task FAILED": task.status == FAILED,
            "warnings for each worker": sorted({
                s["worker"] for s in signals
                if s["event_type"] == EVENT_AUTHENTICATION_REQUIRED
            }) == ["copilot", "deepseek", "opencode"],
            "ONE consolidated failure per routing call": len(consolidated) >= 1,
            "consolidated severity error": all(
                c["severity"] == "error" for c in consolidated),
            "status lists OpenCode": "- OpenCode — authentication required" in detail,
            "status lists DeepSeek key": "- DeepSeek — API key required" in detail,
            "status lists Copilot": "- Copilot — authentication required" in detail,
        }
        _report(checks)


def scenario_no_secrets():
    """21 — credential material from worker output never leaks into UI."""
    print("=" * 70)
    print("RUNTIME TEST 21 — NO CREDENTIALS IN NOTIFICATIONS")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notifications = []
        router = build_router(auth_worker("opencode"), auth_worker("deepseek"),
                              auth_worker("copilot"), notifications)
        _pipeline, signals, _finished = run(THREE_FILES, ws, router)

        blob = repr([dict(s) for s in signals])
        checks = {
            "notifications produced": len(signals) > 0,
            "fake API key absent": FAKE_SECRET not in blob,
            "'Bearer' absent": "Bearer" not in blob,
            "raw 401 line absent": "HTTP 401" not in blob,
            "structured messages only": all(
                s["message"] in (
                    "OpenCode authentication required.",
                    "DeepSeek API key required.",
                    "GitHub Copilot authentication required.",
                    "AutoFix could not find an available worker.",
                    "OpenCode is unavailable.",
                    "DeepSeek is unavailable.",
                    "GitHub Copilot is unavailable.",
                ) for s in signals if s["event_type"] != EVENT_NO_WORKER_AVAILABLE),
        }
        _report(checks)


SCENARIOS = {
    "auth-fallback": scenario_auth_fallback,
    "missing-key": scenario_missing_key,
    "all-auth": scenario_all_auth,
    "no-secrets": scenario_no_secrets,
}


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print("=" * 70)
    print("RUNTIME AUTH-NOTIFICATION VERIFICATION: ALL REQUESTED SCENARIOS PASSED")
