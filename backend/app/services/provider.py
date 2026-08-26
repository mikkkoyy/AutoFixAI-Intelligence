from dataclasses import dataclass
from pathlib import Path
import json
import urllib.request

from app.config import API_KEY, BASE_URL, MODEL
from app.services.diagnostics import Diagnosis


@dataclass
class ProviderResult:
    ok: bool
    message: str


class AIProvider:
    name = "abstract"

    def generate(
        self,
        task: str,
        workspace: Path,
    ) -> ProviderResult:
        raise NotImplementedError

    def fix(
        self,
        task: str,
        failure: str,
        workspace: Path,
        diagnosis: Diagnosis | None = None,
    ) -> ProviderResult:
        raise NotImplementedError


class DeterministicProvider(AIProvider):
    """
    Local deterministic provider used for development and integration tests.

    The demo scenario creates a deliberately broken add() implementation,
    then repairs it after a classified test failure.
    """

    name = "deterministic"

    def generate(
        self,
        task: str,
        workspace: Path,
    ) -> ProviderResult:

        task_lower = task.lower()

        demo_request = any(
            keyword in task_lower
            for keyword in (
                "demo",
                "add function",
                "addition",
                "python add",
                "make its tests pass",
            )
        )

        if demo_request:
            (workspace / "demo.py").write_text(
                "def add(a, b):\n"
                "    return a - b\n",
                encoding="utf-8",
            )

            (workspace / "test_demo.py").write_text(
                "from demo import add\n"
                "\n"
                "\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )

            return ProviderResult(
                True,
                "Generated deterministic demo project.",
            )

        return ProviderResult(
            True,
            "No deterministic files were generated.",
        )

    def fix(
        self,
        task: str,
        failure: str,
        workspace: Path,
        diagnosis: Diagnosis | None = None,
    ) -> ProviderResult:

        target = workspace / "demo.py"

        # Never perform a deterministic repair without a diagnosis.
        if diagnosis is None:
            return ProviderResult(
                False,
                "No diagnosis was provided to the deterministic fixer.",
            )

        # Unknown failures must not receive a blind deterministic fix.
        if diagnosis.category == "unknown":
            return ProviderResult(
                False,
                "Failure could not be classified; no deterministic fix applied.",
            )

        if diagnosis.category != "test_failure":
            return ProviderResult(
                False,
                f"No deterministic fix is available for {diagnosis.category}.",
            )

        if not target.exists():
            return ProviderResult(
                False,
                "Deterministic target demo.py was not found.",
            )

        test_files = list(
            workspace.glob("test_*.py")
        )

        if not test_files:
            return ProviderResult(
                False,
                "No deterministic test file was found.",
            )

        source = target.read_text(
            encoding="utf-8",
        )

        if "return a - b" not in source:
            return ProviderResult(
                False,
                "No known deterministic defect was found.",
            )

        target.write_text(
            "def add(a, b):\n"
            "    return a + b\n",
            encoding="utf-8",
        )

        return ProviderResult(
            True,
            "Applied deterministic demo fix.",
        )


class OpenAICompatibleProvider(AIProvider):
    name = "openai-compatible"

    def _call(
        self,
        instruction: str,
    ):

        if (
            not API_KEY
            or not BASE_URL
            or not MODEL
        ):
            return ProviderResult(
                False,
                "AI provider is not configured.",
            )

        payload = json.dumps(
            {
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": instruction,
                    }
                ],
            }
        ).encode()

        request = urllib.request.Request(
            BASE_URL + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=60,
            ) as response:
                data = json.loads(
                    response.read().decode()
                )

            content = data[
                "choices"
            ][0][
                "message"
            ][
                "content"
            ]

            return ProviderResult(
                True,
                content,
            )

        except Exception as exc:
            return ProviderResult(
                False,
                f"Provider request failed: {exc}",
            )

    def generate(
        self,
        task: str,
        workspace: Path,
    ) -> ProviderResult:

        return self._call(
            "You are a coding agent. Analyze this task and return "
            "a concise implementation plan. Do not execute commands. "
            "Task: " + task
        )

    def fix(
        self,
        task: str,
        failure: str,
        workspace: Path,
        diagnosis: Diagnosis | None = None,
    ) -> ProviderResult:

        diagnosis_text = (
            diagnosis.summary
            if diagnosis
            else "No diagnosis available."
        )

        return self._call(
            "You are a debugging agent. Analyze the test failure "
            "and provide a precise fix plan. Do not execute commands.\n\n"
            "Task:\n"
            + task
            + "\n\nDiagnosis:\n"
            + diagnosis_text
            + "\n\nFailure:\n"
            + failure
        )


def build_provider():

    from app.config import PROVIDER

    # "openai" is a first-class alias of the OpenAI-compatible provider.
    # Credentials come from AUTOFIX_* or fall back to OPENAI_API_KEY
    # (see app.config).
    if PROVIDER in {
        "openai",
        "openai-compatible",
    }:
        return OpenAICompatibleProvider()

    return DeterministicProvider()
