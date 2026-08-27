"""Command line entry point for AIRA AutoFix.

Usage:
    python -m AIRA.autofix --error "ValueError: boom"
    python -m AIRA.autofix --traceback "<traceback text>"
    python -m AIRA.autofix --test tests/test_core.py::test_models --mode safe
    python -m AIRA.autofix --pytest "FAILED tests/test_core.py::test_models"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m AIRA.autofix",
        description="AIRA AutoFix v1 - AI-assisted safe bug fixing",
    )
    parser.add_argument("--error", help="An error message to analyze")
    parser.add_argument("--traceback", help="A raw traceback string to analyze")
    parser.add_argument("--pytest", help="A pytest failure line to analyze")
    parser.add_argument("--log", help="An application log ERROR/CRITICAL line to analyze")
    parser.add_argument(
        "--test",
        help="Targeted test to run in safe mode, e.g. tests/test_core.py::test_models",
    )
    parser.add_argument(
        "--mode",
        choices=["suggest", "safe"],
        default=None,
        help="AutoFix mode (default: from configuration, usually 'suggest')",
    )
    parser.add_argument("--command", help="Original failing command (for context)")
    parser.add_argument("--source", help="Path to a file with relevant source code context")
    parser.add_argument(
        "--repo",
        default=str(PROJECT_ROOT),
        help="Repository root (defaults to the project root)",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON result")
    return parser


def _read_file(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return ""


async def _run(args) -> int:
    from AIRA.autofix import AutoFixEngine, ErrorMonitor, normalize_error

    monitor = ErrorMonitor(repository_path=args.repo)

    if args.traceback:
        report = monitor.normalize_traceback(args.traceback, test_name=args.test)
    elif args.pytest:
        report = monitor.normalize_pytest(args.pytest, test_name=args.test)
    elif args.log:
        report = monitor.normalize_log(args.log, command=args.command)
    elif args.error:
        report = normalize_error(
            traceback_text=f"{args.error}",
            test_name=args.test,
            command=args.command,
            repository_path=args.repo,
        )
    else:
        report = normalize_error(
            test_name=args.test,
            command=args.command,
            repository_path=args.repo,
        )

    from AIRA.config import config

    config.load()

    engine = AutoFixEngine(config=config, repo_root=args.repo)
    outcome = await engine.run(
        report,
        mode=args.mode,
        test_name=args.test,
        relevant_source=_read_file(args.source),
    )

    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, default=str))
        return 0 if outcome.success else 1

    print("=" * 72)
    print("AIRA AutoFix result")
    print("=" * 72)
    print(f"mode       : {outcome.mode}")
    print(f"success    : {outcome.success}")
    if outcome.proposal:
        print(f"risk       : {outcome.proposal.risk}")
        print(f"confidence : {outcome.proposal.confidence}")
        print(f"root cause : {outcome.proposal.root_cause}")
        print(f"strategy   : {outcome.proposal.fix_strategy}")
        print(f"files      : {', '.join(outcome.proposal.affected_files) or '-'}")
        print(f"tests      : {', '.join(outcome.proposal.targeted_tests) or '-'}")
    if outcome.verification:
        v = outcome.verification
        print(f"target test: {v.targeted_test} passed={v.targeted_passed}")
        print(f"full suite : passed={v.full_suite_passed}")
        if v.stderr:
            print("--- stderr ---")
            print(v.stderr[-1500:])
    if outcome.commit:
        print(f"commit     : {outcome.commit}")
    if outcome.branch:
        print(f"branch     : {outcome.branch}")
    if outcome.error:
        print(f"error      : {outcome.error}")
    if not outcome.success:
        print("\nNo fix was kept. For safe mode, use --test <node-id> --mode safe.")
    return 0 if outcome.success else 1


def main() -> int:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())