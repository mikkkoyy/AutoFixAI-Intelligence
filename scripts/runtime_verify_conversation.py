"""Runtime verification - conversational Chat intelligence upgrade.

Drives the REAL ChatEngine and the REAL ApprovalPipeline end-to-end with
injected deterministic worker doubles:

    conversation  greeting -> discussion -> proposal -> revision -> APPROVE
                  -> exactly ONE AutoFixTask executed via the existing
                  pipeline (decompose -> subtasks -> verify) -> COMPLETED
    clarify       materially ambiguous request asks ONE question; the
                  answered follow-up produces a concrete proposal
    no-exec       questions/discussion never create tasks or arm approval

Usage:
    .venv\\Scripts\\python.exe scripts\\runtime_verify_conversation.py [scenario ...]
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from app.agents.autofix_task import AutoFixTask, COMPLETED, SKIPPED
from app.agents.chat_intelligence import ChatEngine
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.pipeline import ApprovalPipeline
from app.agents.worker_router import WorkerRouter


class FakeWorker:
    """Deterministic internal-worker double (test injection mechanism)."""

    def __init__(self, name, script):
        self.name = name
        self._script = list(script)
        self.calls = []

    def discover(self):
        return BackendInfo(self.name, True, f"{self.name}-exe", "deterministic double")

    def is_available(self):
        return True

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt[:120], "workspace": str(workspace)})
        item = self._script.pop(0) if self._script else None
        if item is None:
            raise AssertionError(f"{self.name} ran out of scripted results")
        return item(prompt, Path(workspace))


def ok_worker(name):
    def run(prompt, root):
        return CodingResult(
            backend=name, success=True,
            output="Simulated change applied; all checks look good.",
        )

    return lambda: FakeWorker(name, [run] * 80)


def task_files(ws: Path):
    tasks_dir = ws / ".autofix" / "tasks"
    return sorted(tasks_dir.glob("autofix-task-*.json")) if tasks_dir.exists() else []


def _report(checks):
    ok = True
    for name, passed in checks.items():
        print(f"    [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print(f"  RESULT: {'ALL CHECKS PASSED' if ok else 'CHECKS FAILED'}")
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------------------


def scenario_conversation():
    """22 - full conversational arc ending in ONE approved AutoFix task."""
    print("=" * 70)
    print("RUNTIME TEST 22 - CHAT CONVERSATION -> PROPOSAL -> REVISE -> EXECUTE")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        engine = ChatEngine()
        history = []
        active = None

        def send(text):
            nonlocal history, active
            response = engine.handle(
                text, str(ws), history=history, active_proposal=active
            )
            history.append(("user", text))
            history.append(("assistant", response.content[:2000]))
            if response.proposal is not None:
                active = response.proposal.to_dict()
            return response

        # 1) Small talk - never plans anything.
        r1 = send("hello")
        # 2) Discussion question - answered, nothing armed.
        r2 = send("What can you help me with in this project?")
        # 3) Coding discussion - a proposal card, still nothing executed.
        r3 = send("Can we improve AutoFix worker fallback?")
        # 4) Revision - SAME proposal updated.
        r4 = send("Make OpenCode primary and DeepSeek fallback.")
        # 5) Approval - hands off to the existing pipeline.
        r5 = send("Approve.")

        print(f"    turn1 kind={r1.kind}  turn2 kind={r2.kind}")
        print(f"    turn3 kind={r3.kind} status={r3.proposal.status if r3.proposal else None}")
        print(f"    turn4 kind={r4.kind} preference={r4.proposal.worker_preference if r4.proposal else None}")
        print(f"    turn5 kind={r5.kind}")

        files_before_approval = task_files(ws)
        router = WorkerRouter(worker_factories={
            "opencode": ok_worker("opencode"),
            "deepseek": ok_worker("deepseek"),
            "copilot": ok_worker("copilot"),
        })
        # Mirror on_approve_plan exactly: the task record is created from the
        # ORIGINAL chat request, the approved execution prompt rides along.
        from app.agents.chat_intelligence import render_proposal_text

        task_record = AutoFixTask.create(str(ws), r5.original_request)
        task_record.approved_prompt = r5.execution_prompt
        task_record.plan = render_proposal_text(r4.proposal) if r4.proposal else ""
        task_record.save()
        pipeline = ApprovalPipeline(
            r5.execution_prompt, str(ws),
            approved_plan=task_record.plan,
            existing_task=task_record,
            worker_router=router,
        )
        finished = []
        pipeline.pipeline_finished.connect(lambda ok, msg: finished.append((ok, msg)))
        pipeline.run()
        task = pipeline.autofix_task
        files_after = task_files(ws)
        saved = json.loads(files_after[0].read_text(encoding="utf-8")) if files_after else {}

        checks = {
            "greeting stays reply": r1.kind == "reply" and r1.proposal is None,
            "question stays conversational": (
                r2.kind == "reply" and r2.proposal is None
            ),
            "coding discussion becomes proposal": (
                r3.kind == "proposal"
                and r3.proposal.status == "AWAITING APPROVAL"
            ),
            "proposal did NOT execute on its own": files_before_approval == [],
            "revision keeps same proposal object semantics": (
                r4.kind == "revision" and len(r4.proposal.revisions) == 1
            ),
            "revision applied worker priority": (
                r4.proposal.worker_preference.startswith("OpenCode -> DeepSeek")
            ),
            "approval utterance triggers handoff": r5.kind == "approval",
            "exactly ONE AutoFixTask file": len(files_after) == 1,
            "original request preserved verbatim": (
                saved.get("original_request")
                == "Can we improve AutoFix worker fallback?"
            ),
            "approved prompt carries revision": (
                "OpenCode" in (saved.get("approved_prompt") or "")
            ),
            "pipeline signal TRUE": bool(finished) and finished[0][0] is True,
            "task COMPLETED": task.status == COMPLETED,
            "verified TRUE": task.verified is True,
            "subtasks all completed/skipped": all(
                s.status in (COMPLETED, SKIPPED) for s in task.subtasks
            ) if task.subtasks else True,
        }
        _report(checks)


def scenario_clarify():
    """23 - ambiguous request asks exactly one useful question."""
    print("=" * 70)
    print("RUNTIME TEST 23 - CLARIFICATION THEN CONCRETE PROPOSAL")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        engine = ChatEngine()
        history = []

        def send(text):
            response = engine.handle(text, str(ws), history=history)
            history.append(("user", text))
            history.append(("assistant", response.content[:2000]))
            return response

        r1 = send("Connect AutoFix to my API.")
        r2 = send(
            "Connect AutoFix to my REST API at https://api.example.com "
            "using an API key."
        )
        print(f"    turn1 kind={r1.kind}: {r1.content.splitlines()[0]}")
        print(f"    turn2 kind={r2.kind}")

        checks = {
            "ambiguous request asks a question": (
                r1.kind == "clarification" and r1.requires_clarification
                and "?" in r1.content
            ),
            "no task created by clarification": task_files(ws) == [],
            "answered request produces proposal": (
                r2.kind == "proposal"
                and r2.proposal.status == "AWAITING APPROVAL"
            ),
            "proposal references the API target": (
                "api.example.com" in r2.execution_prompt
            ),
        }
        _report(checks)


def scenario_no_exec():
    """24 - pure discussion/questions never create tasks or proposals."""
    print("=" * 70)
    print("RUNTIME TEST 24 - DISCUSSION NEVER EXECUTES")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        engine = ChatEngine()
        messages = [
            "hello there",
            "What is FastAPI?",
            "Why is worker fallback failing?",
            "Should we use SQLite or JSON storage?",
            "How would you add automatic retry?",
        ]
        kinds = []
        for text in messages:
            response = engine.handle(text, str(ws), history=[
                ("user", t) for t in messages[:kinds.__len__()]
            ])
            kinds.append(response.kind)

        print("    kinds: " + ", ".join(kinds))
        plan_idx = 4  # "How would you add automatic retry?" -> PLAN_REQUEST
        checks = {
            "pure discussion stays conversational": all(
                k == "reply" for i, k in enumerate(kinds) if i != plan_idx
            ),
            "planning question yields unexecuted proposal at most": kinds[
                plan_idx
            ] in ("reply", "proposal"),
            "no tasks created by any turn": task_files(ws) == [],
        }
        _report(checks)


SCENARIOS = {
    "conversation": scenario_conversation,
    "clarify": scenario_clarify,
    "no-exec": scenario_no_exec,
}


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print("=" * 70)
    print("RUNTIME CONVERSATION VERIFICATION: ALL REQUESTED SCENARIOS PASSED")
