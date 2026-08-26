"""Qt thread workers for Chat, knowledge saving and the internal OpenCode worker.

``ChatWorker``            — conversational reply via app.agents.chat_intelligence.
``KnowledgeSaveWorker``   — explicit user-approved GitHub knowledge save.
``OpenCodeChatWorker``    — internal OpenCode execution path, pinned to the
                            OpenCode backend and executed against the active
                            workspace captured at send time.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.agents.task_transport import prepare_task_payload

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OPENCODE_TIMEOUT_SECONDS = int(os.environ.get("AUTOFIX_OPENCODE_TIMEOUT", "600"))


class ChatWorker(QThread):
    """Produces a conversational AI reply without touching the pipeline.

    Since the Chat intelligence upgrade, ``run()`` goes through
    :class:`app.agents.chat_intelligence.ChatEngine`: replies stay natural,
    coding requests become revisable proposals, and approvals are detected —
    but execution still never happens here.  The optional
    ``active_proposal`` attribute (set by the window AFTER construction, so
    test doubles that override __init__ keep working) carries the pending
    proposal dict for revision/approval handling.
    """

    reply_ready = Signal(str)
    reply_failed = Signal(str)
    #: Full ChatResponse payload for the window's state machine.
    structured_ready = Signal(dict)
    #: Emitted when research phase starts/ends for subtle UI status.
    research_status = Signal(str)

    def __init__(self, message: str, workspace: str, history=None, parent=None):
        super().__init__(parent)
        self._message = message
        self._workspace = workspace
        self._history = list(history or [])
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _produce(self):
        from app.agents.chat_intelligence import ChatEngine

        # Emit research status if this message likely needs web research
        try:
            from app.agents.web_research import should_research
            if should_research(self._message):
                self.research_status.emit("researching")
        except Exception:
            pass

        engine = ChatEngine()
        result = engine.handle(
            self._message,
            self._workspace,
            history=self._history,
            active_proposal=getattr(self, "active_proposal", None),
        )

        # Signal research completion
        try:
            self.research_status.emit("complete")
        except Exception:
            pass

        return result

    def run(self):
        try:
            response = self._produce()
        except Exception as exc:
            if not self._cancelled:
                self.reply_failed.emit(str(exc))
            return
        if self._cancelled:
            return
        self.reply_ready.emit(response.content)
        try:
            self.structured_ready.emit(response.to_dict())
        except Exception:
            pass


class KnowledgeSaveWorker(QThread):
    """Saves ONE user-approved knowledge item to the GitHub knowledge repo.

    Runs off the UI thread so a slow/unreachable GitHub never blocks the
    interface.  Emits ``saved(ok, message)`` exactly once with a CLEAN,
    credential-free message — the window only claims success when ``ok`` is
    True (the save operation itself is honest about failures).
    """

    saved = Signal(bool, str)

    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self._payload = dict(payload or {})
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from app.agents.github_knowledge import KnowledgeItem, save_knowledge

        if self._cancelled:
            return
        try:
            item = KnowledgeItem.from_dict(self._payload)
        except Exception as exc:
            self.saved.emit(False, f"Knowledge payload was invalid: {type(exc).__name__}")
            return
        result = save_knowledge(item)
        if self._cancelled:
            return
        self.saved.emit(bool(result.get("ok")), str(result.get("message", "")))


class OpenCodeChatWorker(QThread):
    """Runs one OpenCode request against the active workspace."""

    output_received = Signal(str)
    request_finished = Signal(bool, str)

    def __init__(self, prompt: str, workspace: str, parent=None):
        super().__init__(parent)
        self._prompt = prompt
        self._workspace = workspace
        self._transport = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _discover(self):
        from app.agents.coding_agent import _default_discover_opencode

        return _default_discover_opencode()

    def run(self):
        info = self._discover()
        if not (info.available and info.executable):
            if not self._cancelled:
                self.request_finished.emit(
                    False,
                    f"OpenCode could not be started or reached.\n{info.detail}".strip(),
                )
            return

        env = os.environ.copy()
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        collected: list[str] = []

        # Large requests travel via the workspace task file, never as one
        # giant command-line argument.
        self._transport = prepare_task_payload(self._prompt, self._workspace)
        effective_prompt = self._transport.command_prompt

        try:
            process = subprocess.Popen(
                [info.executable, "run", effective_prompt],
                cwd=str(Path(self._workspace)),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            if not self._cancelled:
                self.request_finished.emit(
                    False, f"OpenCode could not be started: {exc}"
                )
            return

        timed_out = False
        try:
            for line in process.stdout or []:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                collected.append(text)
                if not self._cancelled:
                    self.output_received.emit(text)
            process.wait(timeout=OPENCODE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass

        output = "\n".join(collected)
        if self._cancelled:
            return

        if timed_out:
            self.request_finished.emit(
                False, f"OpenCode exceeded {OPENCODE_TIMEOUT_SECONDS}s timeout."
            )
        elif process.returncode == 0:
            self.request_finished.emit(True, output or "OpenCode completed with no output.")
        else:
            tail = "\n".join(output.splitlines()[-8:]) if output else ""
            self.request_finished.emit(
                False,
                f"OpenCode exited with code {process.returncode}."
                + (f"\n{tail}" if tail else ""),
            )
