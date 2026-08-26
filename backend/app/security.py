from pathlib import Path

ALLOWED_COMMANDS = {"pytest", "python"}
FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", "<", "`"}

def validate_command(command: list[str]):
    if not command:
        raise ValueError("Command cannot be empty.")
    executable = Path(command[0]).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable not in ALLOWED_COMMANDS:
        raise ValueError(f"Executable '{executable}' is not allowed.")
    if any(token in command for token in FORBIDDEN_TOKENS):
        raise ValueError("Shell chaining/redirection is not allowed.")
