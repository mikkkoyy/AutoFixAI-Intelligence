from dataclasses import dataclass
import re


@dataclass
class Diagnosis:
    category: str
    summary: str


class DiagnosticService:
    """
    Classifies test execution failures using pytest/Python output.

    This service intentionally does not modify files.
    It only determines what kind of failure occurred.
    """

    def diagnose(
        self,
        stdout: str,
        stderr: str,
    ) -> Diagnosis:

        stdout = stdout or ""
        stderr = stderr or ""

        text = (
            f"{stdout}\n{stderr}"
        )

        normalized = text.lower()

        # ---------------------------------------------------------
        # Python syntax errors
        # ---------------------------------------------------------

        if (
            "syntaxerror:" in normalized
            or "indentationerror:" in normalized
        ):
            return Diagnosis(
                "syntax_error",
                "Python syntax or indentation is invalid.",
            )

        # ---------------------------------------------------------
        # Import errors
        # ---------------------------------------------------------

        if "modulenotfounderror:" in normalized:
            match = re.search(
                r"modulenotfounderror:\s*no module named ['\"]?([^'\"\s]+)",
                normalized,
            )

            if match:
                return Diagnosis(
                    "import_error",
                    f"Python module could not be imported: {match.group(1)}.",
                )

            return Diagnosis(
                "import_error",
                "A required Python module could not be imported.",
            )

        if "importerror:" in normalized:
            return Diagnosis(
                "import_error",
                "A Python import failed.",
            )

        # ---------------------------------------------------------
        # Timeout
        # ---------------------------------------------------------

        if (
            "test execution timed out" in normalized
            or "timed out" in normalized
            or "timeout" in normalized
        ):
            return Diagnosis(
                "timeout",
                "Test execution exceeded the allowed timeout.",
            )

        # ---------------------------------------------------------
        # Pytest assertion failures
        #
        # Examples:
        #
        # E       assert 2 == 5
        # E       AssertionError
        # FAILED test_demo.py::test_add
        # ---------------------------------------------------------

        if (
            "assertionerror" in normalized
            or re.search(
                r"^\s*e\s+assert\s+",
                normalized,
                re.MULTILINE,
            )
            or "assert " in normalized
            or "failed test_" in normalized
            or "failed tests/" in normalized
        ):
            return Diagnosis(
                "test_failure",
                "A test assertion failed.",
            )

        # ---------------------------------------------------------
        # Generic pytest failure
        # ---------------------------------------------------------

        if (
            "failed" in normalized
            and (
                "pytest" in normalized
                or "test session" in normalized
            )
        ):
            return Diagnosis(
                "test_failure",
                "The test suite reported a failure.",
            )

        # ---------------------------------------------------------
        # Collection errors
        # ---------------------------------------------------------

        if (
            "error collecting" in normalized
            or "collectionerror" in normalized
        ):
            return Diagnosis(
                "collection_error",
                "Pytest could not collect one or more tests.",
            )

        # ---------------------------------------------------------
        # Generic Python runtime errors
        # ---------------------------------------------------------

        runtime_errors = (
            "typeerror:",
            "valueerror:",
            "nameerror:",
            "attributeerror:",
            "indexerror:",
            "keyerror:",
            "zerodivisionerror:",
        )

        for error_name in runtime_errors:
            if error_name in normalized:
                return Diagnosis(
                    "runtime_error",
                    f"Python runtime error detected: {error_name[:-1]}.",
                )

        # ---------------------------------------------------------
        # Unknown
        # ---------------------------------------------------------

        return Diagnosis(
            "unknown",
            "Failure was not classified.",
        )
