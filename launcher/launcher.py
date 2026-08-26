"""AutoFix AI Studio Launcher

Starts the FastAPI backend, waits for its health check, then launches
the PySide6 frontend.  Designed to run as a plain script *or* as a
PyInstaller-bundled .exe on Windows.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import shutil
import collections

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_project_root() -> Path:
    """Return the project root directory.

    When frozen by PyInstaller the entry point lives inside
    ``<dist>/_internal/`` (or beside the exe), so we walk up until we
    find the marker file ``backend/app/main.py``.  When running as a
    normal script the root is simply the parent of this file's directory.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — start from the exe's directory
        candidate = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parent.parent

    # Walk up looking for the backend marker
    for _ in range(10):
        if (candidate / "backend" / "app" / "main.py").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    # Fallback: assume cwd or script parent is the root
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resolve_project_root()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_LOG_FILE = LOG_DIR / "launcher.log"

_handlers: list[logging.Handler] = [
    logging.FileHandler(_LOG_FILE, encoding="utf-8"),
]
if sys.stdout is not None:
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("launcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Default venv paths (may be absent). Prefer project venvs but fall back to
# a discovered system Python (py, python) when possible.
BACKEND_PY = PROJECT_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
FRONTEND_PY = PROJECT_ROOT / "frontend" / ".venv" / "Scripts" / "python.exe"
ROOT_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _find_python_executable(candidates: list[Path] | None = None) -> str | None:
    """Return a path to a Python executable. Search given candidate paths,
    then fall back to common system launchers ('py', 'python', 'python3').
    Returns the string suitable to pass as argv[0] to subprocess.
    """
    candidates = list(candidates or [])
    for p in candidates:
        try:
            if isinstance(p, (str,)):
                path = Path(p)
            else:
                path = Path(p)
            # Support both Windows and Unix venv layouts
            if path.exists() and os.access(path, os.X_OK):
                return str(path)
        except Exception:
            continue

    # Common fallback names on Windows (py) and others (python)
    for name in ("py", "python.exe", "python3.exe", "python", "python3"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def _resolve_runtime_pythons():
    """Resolve BACKEND_PY and FRONTEND_PY to usable executables where possible.

    Updates the global BACKEND_PY, FRONTEND_PY and ROOT_PY variables (strings).
    Returns a dict with resolved paths (or None) for diagnostics.
    """
    global BACKEND_PY, FRONTEND_PY, ROOT_PY

    backend_candidates = [
        PROJECT_ROOT / "backend" / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / "backend" / ".venv" / "bin" / "python",
    ]
    frontend_candidates = [
        PROJECT_ROOT / "frontend" / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / "frontend" / ".venv" / "bin" / "python",
    ]
    root_candidates = [
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "bin" / "python",
    ]

    resolved_backend = _find_python_executable(backend_candidates)
    resolved_frontend = _find_python_executable(frontend_candidates)
    resolved_root = _find_python_executable(root_candidates)

    # If project-specific interpreters are missing, try system-wide interpreter
    # for both backend and frontend if necessary.
    if not resolved_backend:
        resolved_backend = _find_python_executable()
    if not resolved_frontend:
        resolved_frontend = _find_python_executable()
    if not resolved_root:
        resolved_root = resolved_backend or resolved_frontend

    BACKEND_PY = resolved_backend
    FRONTEND_PY = resolved_frontend
    ROOT_PY = resolved_root

    return {
        "backend": resolved_backend,
        "frontend": resolved_frontend,
        "root": resolved_root,
    }

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
HEALTH_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}/health"

HEALTH_TIMEOUT = 60  # seconds to wait for backend to become healthy
POLL_INTERVAL = 0.5  # seconds between health checks

# ---------------------------------------------------------------------------
# Mutex (single instance)
# ---------------------------------------------------------------------------

class _Mutex:
    """Best-effort single-instance guard using a lock file.

    On Windows we cannot rely on POSIX file locks, so we use a lock-file
    with a PID check.  If the PID is stale the lock is forcibly released.
    """

    def __init__(self, name: str = "autofix-studio") -> None:
        self._path = LOG_DIR / f"{name}.lock"
        self._owned = False

    def acquire(self) -> bool:
        if self._path.exists():
            try:
                old_pid = int(self._path.read_text().strip())
            except (ValueError, OSError):
                old_pid = None
            if old_pid is not None:
                # Check if the process is still alive
                try:
                    # os.kill with signal 0 just checks existence
                    os.kill(old_pid, 0)
                except OSError:
                    # Stale lock — remove it
                    log.info("Removing stale lock file (PID %s dead)", old_pid)
                else:
                    log.warning(
                        "Another instance is already running (PID %s). "
                        "Exiting.",
                        old_pid,
                    )
                    return False

        try:
            self._path.write_text(str(os.getpid()), encoding="utf-8")
            self._owned = True
            return True
        except OSError as exc:
            log.warning("Could not create lock file: %s", exc)
            return True  # proceed anyway — non-critical

    def release(self) -> None:
        if self._owned and self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Splash screen (optional lightweight PySide6 splash)
# ---------------------------------------------------------------------------

def _show_splash(message: str = "Starting AutoFix AI Studio…") -> object:
    """Show a minimal splash window.  Returns the QApplication instance.

    Returns *None* (without blocking) when PySide6 is unavailable.
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (
            QApplication,
            QSplashScreen,
            QLabel,
            QWidget,
            QVBoxLayout,
        )
    except ImportError:
        return None

    app = QApplication.instance() or QApplication(sys.argv)

    # Build a simple frame instead of raw QSplashScreen for more control
    splash = QWidget()
    splash.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
    )
    splash.setFixedSize(420, 140)
    splash.setStyleSheet(
        "background: #0f1117; color: #e6eaf0; border: 1px solid #262d3a;"
    )
    layout = QVBoxLayout(splash)
    layout.setContentsMargins(24, 20, 24, 20)

    title = QLabel("AutoFix AI Studio")
    title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
    title.setStyleSheet("color: #3b82f6; background: transparent;")
    layout.addWidget(title)

    status = QLabel(message)
    status.setFont(QFont("Segoe UI", 10))
    status.setStyleSheet("color: #8993a5; background: transparent;")
    layout.addWidget(status)

    splash.move(
        app.primaryScreen().geometry().center() - splash.rect().center()
    )
    splash.show()
    app.processEvents()
    return app, splash, status


def _show_startup_error(title: str, reason: str, details: str = "") -> None:
    """Display a visible startup error — either a GUI dialog (if available)
    or a console message that waits for user acknowledgement.
    """
    message = f"{title}\n\nStartup failed.\n\nReason:\n{reason}\n\n{details}\n"
    log.error("Startup error: %s\n%s", reason, details)

    # Try a GUI dialog first if PySide6 is available
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        dlg = QMessageBox()
        dlg.setWindowTitle("AutoFix AI Studio Launcher")
        dlg.setText("Startup failed.")
        dlg.setInformativeText(reason)
        if details:
            dlg.setDetailedText(details)
        dlg.setIcon(QMessageBox.Icon.Critical)
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()
        return
    except Exception:
        # Fall back to console output
        try:
            print(message)
            input("Press Enter to close...")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

class _Process:
    """Thin wrapper around a subprocess with logging and recent-output buffer.

    The buffer is used to surface useful diagnostics when a child exits
    unexpectedly during startup.
    """

    def __init__(self, label: str, cmd: list[str], cwd: Path) -> None:
        self.label = label
        self.cmd = cmd
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._output_lines: collections.deque[str] = collections.deque(maxlen=400)

    def start(self) -> None:
        log.info("[%s] Starting: %s", self.label, " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except OSError as exc:
            log.exception("[%s] Failed to start: %s", self.label, exc)
            raise

        self._thread = threading.Thread(
            target=self._drain, daemon=True, name=f"{self.label}-drain"
        )
        self._thread.start()

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    line = str(raw)
                if line:
                    self._output_lines.append(line)
                    log.info("[%s] %s", self.label, line)
        except Exception:
            log.exception("[%s] Output drain failed", self.label)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def terminate(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return
        log.info("[%s] Terminating (PID %s)", self.label, self.proc.pid)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("[%s] Force-killing", self.label)
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                log.exception("[%s] Force-kill failed", self.label)
        except OSError as exc:
            log.warning("[%s] terminate error: %s", self.label, exc)

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    def recent_output(self, lines: int = 40) -> str:
        return "\n".join(list(self._output_lines)[-lines:])


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _wait_for_backend(backend_proc: _Process | None) -> bool:
    """Poll the backend health endpoint until it responds or we time out.

    If backend_proc is provided, include its recent output in diagnostic logs
    when the health check fails.
    """
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + HEALTH_TIMEOUT

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(HEALTH_URL)
            with urllib.request.urlopen(req, timeout=2) as resp:
                body = json.loads(resp.read().decode())
                if body.get("status") == "ok":
                    log.info("Backend health OK — %s", body)
                    return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            # Keep trying until deadline
            log.debug("Health check attempt failed: %s", exc)
        time.sleep(POLL_INTERVAL)

    # Timed out — collect backend output for diagnostics
    recent = ""
    rc = None
    if backend_proc is not None:
        recent = backend_proc.recent_output(80)
        rc = backend_proc.returncode

    log.error("Backend did not become healthy within %ss", HEALTH_TIMEOUT)
    if recent:
        log.error("Backend recent output:\n%s", recent)
    if rc is not None:
        log.error("Backend exit code: %s", rc)
    return False


def _port_is_listening(port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        return sock.connect_ex((BACKEND_HOST, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _show_shutdown_notification() -> None:
    """Show the frontend-close shutdown notice and wait for user confirmation.

    The launcher must not kill the backend immediately on the first close event;
    the user must click OK, after which the launcher waits for the backend to exit
    gracefully and only then shuts down the launcher.
    """
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        dlg = QMessageBox(QMessageBox.Icon.Information, "AutoFix AI Studio", "")
        dlg.setText("AutoFix is closing.")
        dlg.setInformativeText("Waiting for server to be closed.\nClick OK to exit.")
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.setModal(True)
        dlg.exec()
        return
    except Exception:
        log.info("Shutdown notice: AutoFix is closing. Waiting for server to be closed. Click OK to exit.")
        try:
            input("AutoFix is closing. Waiting for server to be closed. Click OK to exit.\nPress Enter to continue...")
        except Exception:
            pass


def _wait_for_backend_shutdown(backend_proc: _Process | None, timeout: float = 30.0) -> bool:
    """Wait for the server to exit after the user confirms shutdown."""
    if backend_proc is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not backend_proc.alive:
            return True
        time.sleep(0.5)
    return not backend_proc.alive


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def _validate_venvs() -> bool:
    """Ensure the required Python executables exist, with fallbacks.

    This updates BACKEND_PY/FRONTEND_PY/ROOT_PY to resolved executables when
    possible. Returns True when both backend and frontend executables were
    resolved; otherwise returns False and leaves the globals set to whatever
    was discovered.
    """
    resolved = _resolve_runtime_pythons()
    ok = True
    if not resolved.get("backend"):
        log.error("Backend Python not found — checked project venv and system PATH.")
        ok = False
    if not resolved.get("frontend"):
        log.error("Frontend Python not found — checked project venv and system PATH.")
        ok = False

    log.info("Resolved Python executables: backend=%s frontend=%s root=%s",
             resolved.get("backend"), resolved.get("frontend"), resolved.get("root"))
    return ok


def main() -> int:
    log.info("=" * 60)
    log.info("AutoFix AI Studio Launcher starting")
    log.info("Project root: %s", PROJECT_ROOT)
    log.info("Backend venv: %s", BACKEND_PY)
    log.info("Frontend venv: %s", FRONTEND_PY)

    # Single instance guard
    mutex = _Mutex()
    if not mutex.acquire():
        return 1

    # Validate venvs
    if not _validate_venvs():
        details = (
            f"Project root: {PROJECT_ROOT}\n"
            f"Backend candidate: {BACKEND_PY}\n"
            f"Frontend candidate: {FRONTEND_PY}\n"
        )
        _show_startup_error("AutoFix AI Studio Launcher", "Required Python interpreters not found.", details)
        mutex.release()
        return 1

    # Optional splash
    splash_info = _show_splash("Starting backend…")
    if splash_info is not None:
        app, splash_widget, splash_label = splash_info

    # Check that the backend port is available before attempting to start.
    def _port_in_use(port: int) -> tuple[bool, str]:
        import socket, subprocess, sys

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((BACKEND_HOST, port))
            sock.close()
            return False, ""
        except OSError:
            # Try to find the owning PID (Windows: netstat -ano)
            try:
                if sys.platform == "win32":
                    out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
                    lines = [l for l in out.splitlines() if f":{port} " in l or f":{port}\r" in l or l.strip().endswith(f":{port}")]
                    for l in lines:
                        # last token is PID
                        parts = l.split()
                        pid = parts[-1]
                        # Get process name
                        try:
                            pname = subprocess.check_output(["tasklist", "/fi", f"PID eq {pid}"], text=True, stderr=subprocess.DEVNULL)
                        except Exception:
                            pname = ""
                        return True, f"Port {port} appears in use by PID {pid}.\nNetstat line: {l}\n{pname}"
            except Exception:
                pass
            return True, f"Port {port} is in use (could not identify owner)."

    in_use, owner = _port_in_use(BACKEND_PORT)
    if in_use:
        details = (
            f"Port {BACKEND_PORT} is already in use on {BACKEND_HOST}.\n\n{owner}\n\n"
            f"Project root: {PROJECT_ROOT}\nBackend candidate: {BACKEND_PY}\nFrontend candidate: {FRONTEND_PY}"
        )
        _show_startup_error("AutoFix AI Studio Launcher", "Backend port already in use.", details)
        mutex.release()
        return 1

    # --- Start backend ---
    backend = _Process(
        label="backend",
        cmd=[
            str(BACKEND_PY),
            "-m", "uvicorn",
            "app.main:app",
            "--host", BACKEND_HOST,
            "--port", str(BACKEND_PORT),
        ],
        cwd=PROJECT_ROOT / "backend",
    )
    try:
        backend.start()
    except Exception as exc:
        details = f"Failed to start backend process: {exc}\n\nRecent output:\n{getattr(backend, 'recent_output', lambda n: '')(80)}"
        _show_startup_error("AutoFix AI Studio Launcher", "Backend failed to start.", details)
        mutex.release()
        return 1

    if splash_info is not None:
        splash_label.setText("Waiting for backend…")
        app.processEvents()

    # Wait for backend health
    if not _wait_for_backend(backend):
        log.error("Shutting down — backend failed to start.")
        details = backend.recent_output(200)
        _show_startup_error("AutoFix AI Studio Launcher", "Backend failed health check.", details)
        backend.terminate()
        if splash_info is not None:
            splash_widget.close()
        mutex.release()
        return 1

    if splash_info is not None:
        splash_label.setText("Starting frontend…")
        app.processEvents()

    # --- Start frontend ---
    frontend = _Process(
        label="frontend",
        cmd=[str(FRONTEND_PY), "-m", "app.main"],
        cwd=PROJECT_ROOT / "frontend",
    )
    frontend.start()

    if splash_info is not None:
        splash_widget.close()

    log.info("All processes started. Monitoring…")

    # --- Monitor loop ---
    try:
        while True:
            time.sleep(1)

            if not backend.alive:
                rc = backend.returncode
                details = backend.recent_output(200)
                log.error("Backend exited unexpectedly (code %s)", rc)
                _show_startup_error(
                    "AutoFix AI Studio Launcher",
                    "Backend process exited unexpectedly.",
                    f"Exit code: {rc}\n\nRecent backend output:\n{details}",
                )
                frontend.terminate()
                break

            if not frontend.alive:
                rc = frontend.returncode
                details = frontend.recent_output(200)
                log.info("Frontend exited (code %s). Showing shutdown notice and waiting for backend shutdown.", rc)
                try:
                    _show_shutdown_notification()
                except Exception:
                    log.exception("Shutdown dialog failed")

                # Never kill the backend in the middle of the close dialog; wait
                # for a graceful exit. If the backend does not terminate on its own,
                # then use the safe launcher cleanup path.
                if backend.alive:
                    log.info("Waiting for backend shutdown to complete.")
                    if not _wait_for_backend_shutdown(backend, timeout=30.0):
                        log.warning(
                            "Backend did not exit gracefully within timeout; forcing shutdown."
                        )
                        backend.terminate()
                    else:
                        log.info("Backend exited cleanly after frontend close.")

                if _port_is_listening(BACKEND_PORT):
                    log.warning("Backend port %s still listening after shutdown; diagnosing port state.", BACKEND_PORT)
                break

    except KeyboardInterrupt:
        log.info("Interrupted — shutting down…")

    if backend.alive:
        backend.terminate()
    if frontend.alive:
        frontend.terminate()
    mutex.release()

    log.info("Launcher exiting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
