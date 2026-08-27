"""AI-powered analysis that turns an ErrorReport into a strict, validated proposal."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from AIRA.autofix.models import (
    ANALYSIS_SCHEMA_FIELDS,
    VALID_RISK_LEVELS,
    AutoFixError,
    ErrorReport,
    FixProposal,
)
from AIRA.core.logging import get_logger

logger = get_logger("autofix")

ANALYSIS_SYSTEM_PROMPT = (
    "You are AIRA AutoFix, a safe automated debugging assistant. "
    "You analyze failures and propose fixes. You never execute commands and never "
    "modify files directly; you only return a fix proposal.\n"
    "Return ONLY strict JSON with no markdown fences, no commentary, and no prose. "
    "The JSON must match exactly this schema:\n"
    '{"root_cause": string, "confidence": number between 0 and 1, '
    '"affected_files": [string], "fix_strategy": string, '
    '"patch": string, "targeted_tests": [string], "risk": "low"|"medium"|"high"}\n'
    'The "patch" field must be a JSON string describing file edits with this shape: '
    '{"edits": [{"path": "relative/path.py", "replace": [{"old": "...", "new": "..."}]}]}. '
    "Only reference files inside the repository. Never propose patching .env, "
    "credentials, secrets, keys, .git, or .venv files."
)

PATCH_SCHEMA = {"edits": []}


class AnalysisError(AutoFixError):
    """Raised when the AI response cannot be parsed into a valid proposal."""


class AIAnalyzer:
    """Requests strict JSON analysis from an AI provider and validates it."""

    def __init__(self, provider: Any):
        self.provider = provider

    async def analyze(
        self,
        report: ErrorReport,
        relevant_source: str = "",
        relevant_tests: str = "",
        intelligence: str = "",
        repository_context: str = "",
        prior_fixes: str = "",
        test_output: str = "",
    ) -> FixProposal:
        user_prompt = self._build_prompt(
            report=report,
            relevant_source=relevant_source,
            relevant_tests=relevant_tests,
            intelligence=intelligence,
            repository_context=repository_context,
            prior_fixes=prior_fixes,
            test_output=test_output,
        )
        messages = [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            raw = await self.provider.chat(messages, temperature=0.2)
        except Exception as e:
            logger.error(f"Analyzer provider call failed: {e}")
            raise AnalysisError(f"AI analysis failed: {e}")

        return self.parse_proposal(raw)

    @staticmethod
    def _build_prompt(
        report: ErrorReport,
        relevant_source: str,
        relevant_tests: str,
        intelligence: str,
        repository_context: str,
        prior_fixes: str,
        test_output: str,
    ) -> str:
        data = report.to_dict()
        parts = [
            "Analyze the following failure and produce a fix proposal.",
            f"Error type: {data['error_type']}",
            f"Message: {data['message']}",
            f"Source file: {data.get('source_file') or 'unknown'}",
            f"Source line: {data.get('source_line') or 'unknown'}",
            f"Test: {data.get('test_name') or 'unknown'}",
            f"Command: {data.get('command') or 'not provided'}",
        ]
        if report.traceback:
            parts.append("\n--- Traceback ---\n" + report.traceback[:4000])
        if relevant_source:
            parts.append("\n--- Relevant source code ---\n" + relevant_source[:6000])
        if relevant_tests:
            parts.append("\n--- Relevant tests ---\n" + relevant_tests[:4000])
        if intelligence:
            parts.append("\n--- Relevant prior AIRA intelligence ---\n" + intelligence[:4000])
        if repository_context:
            parts.append("\n--- Repository context ---\n" + repository_context[:2000])
        if prior_fixes:
            parts.append("\n--- Previous fixes for similar errors ---\n" + prior_fixes[:4000])
        if test_output:
            parts.append("\n--- Previous attempt test output ---\n" + test_output[:3000])
        return "\n".join(parts)

    @staticmethod
    def parse_proposal(raw: str) -> FixProposal:
        data = AIAnalyzer._extract_json(raw)
        try:
            proposal = AIAnalyzer._validate(data)
        except AnalysisError:
            raise
        except Exception as e:
            raise AnalysisError(f"Invalid analysis response: {e}")
        return proposal

    @staticmethod
    def _extract_json(raw: str) -> dict:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        if start == -1:
            raise AnalysisError("AI response contained no JSON object")
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError as e:
                        raise AnalysisError(f"Malformed JSON in AI response: {e}")
        raise AnalysisError("Unbalanced JSON in AI response")

    @staticmethod
    def _validate(data: dict) -> FixProposal:
        if data is None or not isinstance(data, dict):
            raise AnalysisError("AI analysis was not a JSON object")
        missing = [f for f in ANALYSIS_SCHEMA_FIELDS if f not in data]
        if missing:
            raise AnalysisError(f"Analysis missing required fields: {missing}")

        try:
            confidence = float(data.get("confidence"))
        except (TypeError, ValueError):
            raise AnalysisError("Analysis confidence must be a number")
        if not (0.0 <= confidence <= 1.0):
            raise AnalysisError("Analysis confidence must be between 0 and 1")

        affected_files = data.get("affected_files") or []
        targeted_tests = data.get("targeted_tests") or []
        if not isinstance(affected_files, list) or not all(isinstance(f, str) for f in affected_files):
            raise AnalysisError("Analysis affected_files must be a list of strings")
        if not isinstance(targeted_tests, list) or not all(isinstance(t, str) for t in targeted_tests):
            raise AnalysisError("Analysis targeted_tests must be a list of strings")
        if not isinstance(data.get("root_cause"), str) or not data["root_cause"].strip():
            raise AnalysisError("Analysis root_cause must be a non-empty string")
        if not isinstance(data.get("fix_strategy"), str) or not data["fix_strategy"].strip():
            raise AnalysisError("Analysis fix_strategy must be a non-empty string")
        if not isinstance(data.get("patch"), str) or not data["patch"].strip():
            raise AnalysisError("Analysis patch must be a non-empty string")

        risk = str(data.get("risk")).lower()
        if risk not in VALID_RISK_LEVELS:
            raise AnalysisError(f"Analysis risk must be one of {VALID_RISK_LEVELS}")

        patch = data.get("patch")
        try:
            parsed_patch = json.loads(patch)
            if not isinstance(parsed_patch, dict) or not isinstance(parsed_patch.get("edits"), list):
                raise AnalysisError("Analysis patch must contain an 'edits' array")
        except json.JSONDecodeError as e:
            raise AnalysisError(f"Analysis patch is not valid JSON: {e}")

        return FixProposal(
            root_cause=data["root_cause"],
            confidence=confidence,
            affected_files=affected_files,
            fix_strategy=data["fix_strategy"],
            patch=patch,
            targeted_tests=targeted_tests,
            risk=risk,
        )