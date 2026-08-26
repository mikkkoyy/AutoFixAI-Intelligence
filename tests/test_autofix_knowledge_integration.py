"""Shared AI knowledge inside AutoFix planning (Parts 8, 23, 24).

- cloud planning prompts receive the relevant shared-knowledge block
- the priority sentence is embedded so shared guidance can never outrank
  project files, project configuration or project memory
- local planner fallback also receives the guidance block
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents import chat_provider as cp
from app.agents import github_knowledge as gk


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "AUTOFIX_PROVIDER", "AUTOFIX_API_KEY", "AUTOFIX_BASE_URL",
        "AUTOFIX_KNOWLEDGE_REPO", "GITHUB_TOKEN", "AUTOFIX_KNOWLEDGE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


GUIDANCE = (
    "Relevant shared AI knowledge:\n"
    + gk.PRIORITY_RULE
    + "\n\n- [planning] Decomposition strategy: Split by verification boundary."
)


# ── Cloud planning prompt composition ────────────────────────────


class TestAnalyzePrompt:
    def test_prompt_contains_shared_block_after_workspace_context(
        self, monkeypatch, tmp_path
    ):
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        monkeypatch.setattr(gk, "shared_knowledge_block",
                            lambda task, limit=2: GUIDANCE)
        captured = {}

        def fake_call(config, prompt):
            captured["prompt"] = prompt
            return "the plan"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        plan, source = cp.analyze("fix the login bug", str(tmp_path),
                                  env={"OPENAI_API_KEY": "k"})
        assert plan == "the plan" and source == "GPT"
        prompt = captured["prompt"]
        assert "app.py" in prompt                       # workspace context
        assert "Decomposition strategy" in prompt       # shared knowledge
        assert prompt.index("app.py") < prompt.index(
            "Relevant shared AI knowledge"
        ), "project files must come before shared knowledge"
        assert prompt.index("Relevant shared AI knowledge") < prompt.index(
            "\nTask:\n"
        )

    def test_priority_rule_embedded_in_planning_context(
        self, monkeypatch, tmp_path
    ):
        seen = {}

        def capture(config, prompt):
            seen["p"] = prompt
            return "plan"

        monkeypatch.setattr(gk, "shared_knowledge_block",
                            lambda task, limit=2: GUIDANCE)
        monkeypatch.setattr(cp, "call_provider", capture)
        cp.analyze("task", str(tmp_path), env={"OPENAI_API_KEY": "k"})
        assert "(1) current project files" in seen["p"]
        assert ".autofix/memory" in seen["p"]

    def test_no_shared_block_when_retrieval_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gk, "shared_knowledge_block",
                            lambda task, limit=2: "")
        captured = {}

        def fake_call(config, prompt):
            captured["prompt"] = prompt
            return "plan"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        cp.analyze("task", str(tmp_path), env={"OPENAI_API_KEY": "k"})
        assert "shared AI knowledge" not in captured["prompt"].lower()

    def test_retrieval_failure_never_breaks_planning(
        self, monkeypatch, tmp_path
    ):
        def exploding(*args, **kwargs):
            raise RuntimeError("retrieval backend down")

        monkeypatch.setattr(gk, "shared_knowledge_block", exploding)
        monkeypatch.setattr(cp, "call_provider",
                            lambda config, prompt: "plan anyway")
        plan, source = cp.analyze("task", str(tmp_path),
                                  env={"OPENAI_API_KEY": "k"})
        assert plan == "plan anyway"

    def test_all_providers_failing_lists_tried_names(self, monkeypatch,
                                                     tmp_path):
        def failing(config, prompt):
            raise cp.ChatProviderError(f"{config.name} HTTP 401")

        monkeypatch.setattr(cp, "call_provider", failing)
        plan, error = cp.analyze("task", str(tmp_path), env={
            "OPENAI_API_KEY": "a", "DEEPSEEK_API_KEY": "d",
        })
        assert plan is None
        assert "[tried: GPT, DeepSeek]" in error


# ── Local planner fallback keeps the guidance ────────────────────


class TestLocalPlannerGuidance:
    def _run_worker(self, monkeypatch, tmp_path, guidance, qapp):
        from app.agents.pipeline import PlanWorker

        monkeypatch.setattr(gk, "shared_knowledge_block",
                            lambda task, limit=2: guidance)
        # No providers configured → analyze returns (None, "") → local path.
        results = []
        worker = PlanWorker(description="add retry to the worker router",
                            workspace=str(tmp_path))
        worker.plan_ready.connect(lambda text: results.append(text))
        worker.plan_failed.connect(lambda err: results.append(("failed", err)))
        worker.run()
        return results

    def test_local_plan_appends_shared_guidance(self, monkeypatch, tmp_path,
                                                qapp):
        results = self._run_worker(monkeypatch, tmp_path, GUIDANCE, qapp)
        assert results and results[0]
        plan = results[0] if isinstance(results[0], str) else ""
        assert "Decomposition strategy" in plan
        assert "(1) current project files" in plan

    def test_local_plan_works_without_guidance(self, monkeypatch, tmp_path,
                                               qapp):
        results = self._run_worker(monkeypatch, tmp_path, "", qapp)
        assert results and isinstance(results[0], str)
        assert "Decomposition strategy" not in results[0]

    def test_local_plan_survives_guidance_failure(self, monkeypatch, tmp_path,
                                                  qapp):
        """Retrieval blow-ups stay silent — the plan still ships."""
        def exploding(*args, **kwargs):
            raise RuntimeError("retrieval down")

        monkeypatch.setattr(gk, "shared_knowledge_block", exploding)
        results = []
        from app.agents.pipeline import PlanWorker

        worker = PlanWorker(description="another task",
                            workspace=str(tmp_path))
        worker.plan_ready.connect(lambda t: results.append(t))
        worker.plan_failed.connect(lambda e: results.append(("failed", e)))
        worker.run()
        assert results and isinstance(results[0], str)   # plan, not failure
        assert "Decomposition strategy" not in results[0]

# ── Chat context ordering: memory before shared ──────────────────


class TestChatContextPriorityOrdering:
    def test_memory_lines_precede_shared_lines(self, monkeypatch, tmp_path):
        from app.agents import task_memory as tm
        from app.agents.chat_intelligence import build_chat_context

        monkeypatch.setattr(
            tm, "retrieve_relevant",
            lambda workspace, message, limit=3: [{
                "kind": "fix", "title": "Login race fix",
                "content": "Serialize writes around session updates.",
            }],
        )
        monkeypatch.setattr(
            gk, "retrieve_knowledge",
            lambda query, limit=3: [gk.KnowledgeEntry(
                path="ai-knowledge/lessons/serialize-writes.md",
                category="lessons", title="Serialize writes", body="b",
            )],
        )
        ctx = build_chat_context("how do we fix login races?", str(tmp_path))
        lines = ctx.summary_lines()
        memory_pos = next(i for i, l in enumerate(lines)
                          if l.startswith("[memory"))
        shared_pos = next(i for i, l in enumerate(lines)
                          if l.startswith("[shared]"))
        assert memory_pos < shared_pos

    def test_shared_lines_absent_without_hits(self, monkeypatch, tmp_path):
        from app.agents.chat_intelligence import build_chat_context

        monkeypatch.setattr(gk, "retrieve_knowledge",
                            lambda query, limit=3: [])
        ctx = build_chat_context("what is a race condition?", str(tmp_path))
        assert not any(l.startswith("[shared]") for l in ctx.summary_lines())
