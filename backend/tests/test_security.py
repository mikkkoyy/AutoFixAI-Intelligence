import pytest
from app.security import validate_command

def test_safe_commands_allowed():
    validate_command(["pytest", "-q"])
    validate_command(["python", "-m", "pytest", "-q"])

def test_shell_injection_rejected():
    with pytest.raises(ValueError):
        validate_command(["python", "-c", "print(1)", "&&", "whoami"])

def test_unknown_executable_rejected():
    with pytest.raises(ValueError):
        validate_command(["powershell", "Get-ChildItem"])
