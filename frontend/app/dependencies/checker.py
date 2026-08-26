from dataclasses import dataclass
import importlib.util
import shutil
import subprocess
import sys

@dataclass(frozen=True)
class DependencySpec:
    name: str
    command: str | None = None
    module: str | None = None
    pip_package: str | None = None
    required: bool = False

@dataclass
class DependencyStatus:
    spec: DependencySpec
    installed: bool
    version: str = ""
    detail: str = ""

class DependencyChecker:
    SPECS = [
        DependencySpec("Python", "python", required=True),
        DependencySpec("pip", "pip", required=True),
        DependencySpec("Git", "git"),
        DependencySpec("PySide6", module="PySide6", pip_package="PySide6", required=True),
        DependencySpec("pytest", module="pytest", pip_package="pytest", required=True),
        DependencySpec("PyInstaller", "pyinstaller", module="PyInstaller", pip_package="pyinstaller"),
        DependencySpec("Node.js", "node"),
        DependencySpec("npm", "npm"),
        DependencySpec("Ollama", "ollama"),
        DependencySpec("CMake", "cmake"),
    ]

    def _version(self, command: str) -> str:
        try:
            result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=4)
            text = (result.stdout or result.stderr).strip().splitlines()
            return text[0] if text else "Installed"
        except (OSError, subprocess.SubprocessError):
            return "Installed"

    def check_one(self, spec: DependencySpec) -> DependencyStatus:
        if spec.command and shutil.which(spec.command):
            return DependencyStatus(spec, True, self._version(spec.command), "Command available")
        if spec.module and importlib.util.find_spec(spec.module):
            return DependencyStatus(spec, True, "Installed", "Python module available")
        return DependencyStatus(spec, False, "", "Not found")

    def check_all(self) -> list[DependencyStatus]:
        return [self.check_one(spec) for spec in self.SPECS]

    def install_python_package(self, spec: DependencySpec) -> tuple[bool, str]:
        if not spec.pip_package:
            return False, f"{spec.name} requires a system installer; AutoFix will not download an unverified installer."
        command = [sys.executable, "-m", "pip", "install", spec.pip_package]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if result.returncode == 0:
            return True, f"{spec.name} installed successfully."
        error = (result.stderr or result.stdout).strip().splitlines()
        return False, error[-1] if error else f"pip exited with code {result.returncode}"
