from app.services.diagnostics import DiagnosticService


def test_pytest_assertion_is_test_failure():
    d = DiagnosticService().diagnose(
        "E       assert -1 == 5\n"
        "E        + where -1 = add(2, 3)",
        "",
    )
    assert d.category == "test_failure"


def test_import_error_is_not_misclassified():
    d = DiagnosticService().diagnose(
        "E   ModuleNotFoundError: No module named 'missing'",
        "",
    )
    assert d.category == "import_error"


def test_syntax_error_has_priority():
    d = DiagnosticService().diagnose("SyntaxError: invalid syntax", "")
    assert d.category == "syntax_error"
