"""Runtime verification: AI Intelligence Framework.

Exercises the full intelligence lifecycle with real file I/O (no mocks):
    1.  Discover intelligence entries across multiple layers
    2.  Validate entries (valid, invalid, secret-rejected)
    3.  Propose, approve, store, retrieve
    4.  Search and relevance-based retrieval
    5.  Context building for AI prompts
    6.  Separation: project memory never mixed with intelligence
    7.  GitHub sync (unconfigured gracefully fails)
    8.  Chat integration (intelligence context populated)
    9.  Pipeline integration (intelligence guidance populated)
    10. Deprecation and audit
    11. Pending queue lifecycle
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend")))

from app.agents.intelligence_store import (
    INTELLIGENCE_LAYERS,
    IntelligenceEntry,
    IntelligenceStorage,
    PendingQueue,
    STATUS_APPROVED,
    STATUS_DEPRECATED,
    STATUS_DISCOVERED,
    STATUS_PROPOSED,
    STATUS_PUBLISHED,
)
from app.agents.intelligence_validator import (
    validate_entry,
    validate_for_proposal,
    validate_for_publication,
)
from app.agents.intelligence_manager import IntelligenceManager, layers_for_intent
from app.agents.intelligence_sync import IntelligenceSync, SyncConfig, SyncResult

PASSED = 0
FAILED = 0


def _check(label: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        msg = f"  FAIL  {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)


def main():
    global PASSED, FAILED
    tmp = tempfile.mkdtemp(prefix="autofix_intel_verify_")
    try:
        _scenario_1_discover_and_validate(tmp)
        _scenario_2_propose_approve_store(tmp)
        _scenario_3_secret_rejection(tmp)
        _scenario_4_search_and_retrieval(tmp)
        _scenario_5_context_building(tmp)
        _scenario_6_separation(tmp)
        _scenario_7_sync_unconfigured(tmp)
        _scenario_8_chat_integration(tmp)
        _scenario_9_pipeline_integration(tmp)
        _scenario_10_deprecation_and_audit(tmp)
        _scenario_11_pending_queue_lifecycle(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*60}")
    print(f"Results: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
    if FAILED:
        print("STATUS: FAIL")
        sys.exit(1)
    else:
        print("STATUS: PASS")
        sys.exit(0)


# -----------------------------------------------------------------------
# Scenario 1: Discover and validate intelligence entries
# -----------------------------------------------------------------------

def _scenario_1_discover_and_validate(tmp):
    print("\n[Scenario 1] Discover and validate intelligence entries")
    mgr = IntelligenceManager(tmp)

    # Valid entry
    p = mgr.discover(
        title="Python Error Handling Pattern",
        layer="coding",
        content="Use try/except with specific exception types rather than bare except clauses.",
        category="patterns",
        tags=("python", "error-handling", "best-practice"),
        source="Chat conversation",
    )
    _check("Entry created", p.entry.id != "")
    _check("Status is DISCOVERED", p.entry.status == STATUS_DISCOVERED)
    _check("Validation passes", p.validation_report.ok)

    # Invalid entry (missing title)
    p2 = mgr.discover(title="", layer="coding", content="Some content.")
    _check("Missing title detected", not p2.validation_report.ok)

    # Invalid layer
    p3 = mgr.discover(title="Bad Layer", layer="nonexistent", content="Content here.")
    _check("Invalid layer detected", not p3.validation_report.ok)


# -----------------------------------------------------------------------
# Scenario 2: Propose, approve, store, retrieve
# -----------------------------------------------------------------------

def _scenario_2_propose_approve_store(tmp):
    print("\n[Scenario 2] Propose, approve, store, retrieve")
    mgr = IntelligenceManager(tmp)

    p = mgr.discover(
        title="Async Error Recovery",
        layer="recovery",
        content="When an async task fails, implement exponential backoff with jitter for retries.",
        category="resilience",
        tags=("async", "retry", "backoff"),
    )
    result = mgr.propose(p)
    _check("Propose succeeds", result["ok"])

    pending = mgr.pending_queue.list_pending()
    _check("Entry in pending queue", len(pending) == 1)

    entry_id = pending[0].id
    result = mgr.approve(entry_id)
    _check("Approve succeeds", result["ok"])

    stored = mgr.load(entry_id)
    _check("Entry stored", stored is not None)
    _check("Status is APPROVED", stored.status == STATUS_APPROVED)

    results = mgr.retrieve_relevant("async retry backoff", "recovery")
    _check("Relevant retrieval finds entry", len(results) >= 1)


# -----------------------------------------------------------------------
# Scenario 3: Secret rejection
# -----------------------------------------------------------------------

def _scenario_3_secret_rejection(tmp):
    print("\n[Scenario 3] Secret rejection")
    mgr = IntelligenceManager(tmp)

    p = mgr.discover(
        title="API Configuration",
        layer="tools",
        content="API key: sk-1234567890abcdef1234567890abcdef",
    )
    result = mgr.propose(p)
    _check("Secret content rejected", not result["ok"])
    _check("Error mentions secret/credential",
           any("secret" in e.lower() or "credential" in e.lower() for e in result.get("errors", [])))


# -----------------------------------------------------------------------
# Scenario 4: Search and relevance-based retrieval
# -----------------------------------------------------------------------

def _scenario_4_search_and_retrieval(tmp):
    print("\n[Scenario 4] Search and relevance-based retrieval")
    mgr = IntelligenceManager(tmp)

    mgr.store(IntelligenceEntry(
        title="Python Debugging Strategy",
        layer="reasoning",
        category="debugging",
        summary="Systematic approach to debugging Python applications.",
        content="Use pdb or breakpoints with targeted test cases to isolate the bug.",
        tags=("python", "debugging", "strategy"),
    ))
    mgr.store(IntelligenceEntry(
        title="CSS Grid Layout Patterns",
        layer="coding",
        category="layouts",
        summary="Responsive grid layouts using CSS Grid.",
        content="Use grid-template-columns with repeat and auto-fit for responsive layouts.",
        tags=("css", "grid", "layout", "responsive"),
    ))

    results = mgr.search("python debugging")
    _check("Search finds python debugging", len(results) >= 1 and "Python" in results[0].title)

    results = mgr.retrieve_relevant("python debugging", "debugging")
    _check("Relevant retrieval filters by intent", len(results) >= 1)

    results = mgr.retrieve_relevant("css grid layout", "coding_request")
    _check("Coding request finds CSS entry", len(results) >= 1)


# -----------------------------------------------------------------------
# Scenario 5: Context building
# -----------------------------------------------------------------------

def _scenario_5_context_building(tmp):
    print("\n[Scenario 5] Context building for AI prompts")
    mgr = IntelligenceManager(tmp)

    mgr.store(IntelligenceEntry(
        title="Test-Driven Development",
        layer="verification",
        category="practices",
        summary="Write tests before implementation to ensure correctness.",
        content="TDD cycle: write failing test, implement minimal code, refactor.",
        tags=("tdd", "testing", "verification"),
    ))

    ctx = mgr.build_intelligence_context("test-driven development", "coding_request")
    _check("Context is non-empty", len(ctx) > 0)
    _check("Context contains layer tag", "VERIFICATION" in ctx)
    _check("Context contains title", "Test-Driven Development" in ctx)

    ctx_empty = mgr.build_intelligence_context("quantum computing", "question")
    _check("Empty context for unmatched query", ctx_empty == "")


# -----------------------------------------------------------------------
# Scenario 6: Separation — project memory never mixed
# -----------------------------------------------------------------------

def _scenario_6_separation(tmp):
    print("\n[Scenario 6] Separation from project memory")
    mgr = IntelligenceManager(tmp)
    mgr.store(IntelligenceEntry(
        title="Intel Entry",
        layer="coding",
        category="test",
        summary="Test entry.",
        content="This is test intelligence content.",
        tags=("test",),
    ))

    intel_dir = os.path.join(tmp, ".autofix", "intelligence")
    memory_dir = os.path.join(tmp, ".autofix", "memory")

    _check("Intelligence directory exists", os.path.exists(intel_dir))
    _check("Project memory directory does NOT exist", not os.path.exists(memory_dir))


# -----------------------------------------------------------------------
# Scenario 7: GitHub sync (unconfigured)
# -----------------------------------------------------------------------

def _scenario_7_sync_unconfigured(tmp):
    print("\n[Scenario 7] GitHub sync (unconfigured)")
    storage = IntelligenceStorage(tmp)
    sync = IntelligenceSync(storage, SyncConfig())

    _check("Sync not configured", not sync.is_configured())

    result = sync.push_approved()
    _check("Push fails gracefully", not result.ok)

    result = sync.pull_remote()
    _check("Pull fails gracefully", not result.ok)

    audit = sync.audit()
    _check("Audit reports unconfigured", not audit["configured"])


# -----------------------------------------------------------------------
# Scenario 8: Chat integration — intelligence context populated
# -----------------------------------------------------------------------

def _scenario_8_chat_integration(tmp):
    print("\n[Scenario 8] Chat integration")
    from app.agents.chat_intelligence import _load_intelligence_context

    mgr = IntelligenceManager(tmp)
    mgr.store(IntelligenceEntry(
        title="Python Debugging Strategy",
        layer="reasoning",
        category="debugging",
        summary="Systematic debugging for Python.",
        content="Use targeted breakpoints and test cases to isolate bugs.",
        tags=("python", "debugging"),
    ))

    ctx = _load_intelligence_context("python debugging", tmp)
    _check("Chat intelligence context is populated", len(ctx) > 0)
    _check("Context mentions intelligence", "reasoning" in ctx.lower() or "python" in ctx.lower())


# -----------------------------------------------------------------------
# Scenario 9: Pipeline integration — intelligence guidance
# -----------------------------------------------------------------------

def _scenario_9_pipeline_integration(tmp):
    print("\n[Scenario 9] Pipeline integration")
    mgr = IntelligenceManager(tmp)
    mgr.store(IntelligenceEntry(
        title="Async Error Recovery Pattern",
        layer="recovery",
        category="resilience",
        summary="Exponential backoff for async retries.",
        content="When async tasks fail, use exponential backoff with jitter.",
        tags=("async", "retry", "backoff"),
    ))

    intel = mgr.build_intelligence_context("async error recovery", max_chars=1200)
    _check("Pipeline intelligence guidance available", len(intel) > 0)
    _check("Guidance contains layer tag", "RECOVERY" in intel)


# -----------------------------------------------------------------------
# Scenario 10: Deprecation and audit
# -----------------------------------------------------------------------

def _scenario_10_deprecation_and_audit(tmp):
    print("\n[Scenario 10] Deprecation and audit")
    mgr = IntelligenceManager(tmp)

    e1 = IntelligenceEntry(
        title="Old Pattern", layer="coding", category="legacy",
        summary="An old pattern.", content="Deprecated content.",
        tags=("old",), status=STATUS_APPROVED,
    )
    e2 = IntelligenceEntry(
        title="New Pattern", layer="coding", category="modern",
        summary="A new pattern.", content="Current best practice.",
        tags=("new",), status=STATUS_APPROVED,
    )
    mgr.store(e1)
    mgr.store(e2)

    result = mgr.deprecate(e1.id)
    _check("Deprecate succeeds", result["ok"])
    loaded = mgr.load(e1.id)
    _check("Entry is DEPRECATED", loaded.status == STATUS_DEPRECATED)

    stats = mgr.stats()
    _check("Stats reports correct total", stats["total"] == 2)

    audit = mgr.audit()
    _check("Audit has storage info", "storage" in audit)
    _check("Audit has sync info", "sync" in audit)


# -----------------------------------------------------------------------
# Scenario 11: Pending queue lifecycle
# -----------------------------------------------------------------------

def _scenario_11_pending_queue_lifecycle(tmp):
    print("\n[Scenario 11] Pending queue lifecycle")
    mgr = IntelligenceManager(tmp)

    p1 = mgr.discover(title="Pending One", layer="coding", content="Content one.")
    p2 = mgr.discover(title="Pending Two", layer="reasoning", content="Content two.")
    mgr.propose(p1)
    mgr.propose(p2)

    _check("Two entries pending", mgr.pending_queue.count() == 2)

    pending = mgr.pending_queue.list_pending()
    first_id = pending[0].id
    result = mgr.reject(first_id)
    _check("Reject succeeds", result["ok"])
    _check("One entry remaining", mgr.pending_queue.count() == 1)

    remaining = mgr.pending_queue.list_pending()
    result = mgr.approve(remaining[0].id)
    _check("Approve remaining succeeds", result["ok"])
    _check("Pending queue empty", mgr.pending_queue.count() == 0)

    stored = mgr.list_entries(status=STATUS_APPROVED)
    _check("Approved entry stored", len(stored) >= 1)


if __name__ == "__main__":
    main()
