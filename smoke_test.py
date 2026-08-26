"""Offscreen GUI smoke test — exercises the full TODO checklist end-to-end."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend"))

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.ui import main_window as mw
from app.ui.main_window import MainWindow
from app.agents.coding_agent import CodingAgentRunner, CodingResult
from app.agents.pipeline import ApprovalPipeline

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except Exception as exc:
        results.append((name, f"FAIL: {exc}"))


window = MainWindow()
window.show()
app.processEvents()

# 1 ── File menu structure
def _file_menu():
    titles = [a.text() for a in window.menuBar().actions()[0].menu().actions()]
    assert titles == [
        "New File", "Open File...", "Open Folder...", "Save", "Save As...", "Exit"
    ], titles


check("File menu contains Open Folder...", _file_menu)

# 2 ── Ctrl+K, Ctrl+O shortcut
check("Ctrl+K Ctrl+O shortcut registered",
      lambda: (_ for _ in ()).throw(AssertionError("missing"))
      if window.open_folder_shortcut.key().toString().lower() != "ctrl+k, ctrl+o"
      else None)

# 3-4 ── Open Folder propagation
tmp = Path(os.environ["TEMP"]) / "autofix_smoke_ws"
(tmp / "pkg").mkdir(parents=True, exist_ok=True)
(tmp / "pkg" / "mod.py").write_text("value = 42\n", encoding="utf-8")

mw.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(tmp))
window.open_project_folder()
app.processEvents()


def _propagation():
    assert window.active_workspace == str(tmp)
    assert window.project_path == str(tmp)
    assert window.agent_workspace == str(tmp)
    assert window.tree.topLevelItem(0).text(0) == "autofix_smoke_ws"
    assert window.chat_workspace_label.text() == "autofix_smoke_ws"
    assert "autofix_smoke_ws" in window.windowTitle()
    assert window._workspace_path() == tmp


check("Open Folder propagates to explorer/chat/title/agent", _propagation)

# 5 ── Editor opens file from new workspace
window.open_file_path(tmp / "pkg" / "mod.py")
check("Editor opens file from new workspace",
      lambda: None if window.current_editor() else (_ for _ in ()).throw(
          AssertionError("no editor")))

# 6 ── Real detection on this machine
runner = CodingAgentRunner()
backends = runner.detect_backends()


def _detection():
    assert backends["opencode"].available, "opencode should be available here"
    assert not backends["openhands"].available
    assert not backends["continue"].available
    assert not backends["aider"].available
    assert runner.primary_backend().name == "opencode"


check("Real detection: OpenCode primary, others honest-unavailable", _detection)

# 7 ── Panel indicators: circle-only green/red + PRIMARY
window.update_coding_agent_statuses(backends)
app.processEvents()


def _indicators():
    green = mw.MainWindow._STATUS_GREEN
    red = mw.MainWindow._STATUS_RED
    op_label = window.coding_agent_labels["opencode"]
    assert green in op_label.text() and "AVAILABLE" in op_label.text()
    # ONLY the circle is colored — name/state are plain text after </span>.
    colored = op_label.text().split("</span>")[0]
    assert "&#9679;" in colored and "OpenCode" not in colored
    for name in ("openhands", "continue", "aider"):
        lbl = window.coding_agent_labels[name]
        assert red in lbl.text() and "UNAVAILABLE" in lbl.text()
        assert lbl.styleSheet() == ""
    assert window.primary_agent_label.text() == "PRIMARY: OpenCode"
    assert window.primary_agent_label.styleSheet() == ""


check("Coding agent circles green/red, text normal, PRIMARY: OpenCode", _indicators)

# 7b ── AI CHAT AGENTS section with real environment detection
from app.agents.chat_agents import detect_chat_agents

chat_agents = detect_chat_agents()
window.update_chat_agent_statuses(chat_agents)
app.processEvents()


def _chat_indicators():
    right = window.main_split.widget(2)
    for name in ("GPT", "Claude", "DeepSeek"):
        lbl = window.chat_agent_labels[name]
        assert right.isAncestorOf(lbl)
        state = "AVAILABLE" if chat_agents[name].available else "UNAVAILABLE"
        assert state in lbl.text(), (name, lbl.text())
        color = (
            mw.MainWindow._STATUS_GREEN
            if chat_agents[name].available
            else mw.MainWindow._STATUS_RED
        )
        assert color in lbl.text()
        assert lbl.styleSheet() == ""


check("AI CHAT AGENTS rows (GPT/Claude/DeepSeek) circle-only colored", _chat_indicators)

# 7c ── Diagnostic status present and initially WAITING
check("Diagnostic status WAITING in right panel",
      lambda: None if window.diagnostic_status_label.text() == "STATUS: WAITING"
      else (_ for _ in ()).throw(AssertionError(window.diagnostic_status_label.text())))

# 8 ── Fallback chain (simulated)
chain = []
r = CodingAgentRunner(
    discover_opencode=lambda: __import__("app.agents.coding_agent", fromlist=["BackendInfo"]).BackendInfo("opencode", False, None),
    discover_aider=lambda: __import__("app.agents.coding_agent", fromlist=["BackendInfo"]).BackendInfo("aider", True, "C:/fake/aider.exe"),
)
res = r.execute("task", tmp)
check("Fallback: OpenCode unavailable → Aider used",
      lambda: None if res.backend == "aider" else (_ for _ in ()).throw(
          AssertionError(res.backend)))

none_res = CodingAgentRunner(
    discover_opencode=lambda: __import__("app.agents.coding_agent", fromlist=["BackendInfo"]).BackendInfo("opencode", False, None),
    discover_aider=lambda: __import__("app.agents.coding_agent", fromlist=["BackendInfo"]).BackendInfo("aider", False, None),
).execute("task", tmp)
check("No agent → honest failure",
      lambda: None if (none_res.backend is None and not none_res.success)
      else (_ for _ in ()).throw(AssertionError("not honest")))

# 9 ── Chat approval gating
sent = []
window.chat_input.send_requested.connect(sent.append)
window.chat_input.setPlainText("create a python module")
window.chat_input._emit_send()
app.processEvents()


def _approval_gate():
    assert sent == ["create a python module"]
    assert window._pipeline is None, "chat must NOT start pipeline"
    assert not window.approve_button.isVisibleTo(window), "no plan yet"


check("Chat Enter sends message only — no pipeline", _approval_gate)

# Plan arrives → button appears; Enter still never approves
window._last_request = sent[0]
window._on_plan_ready("1. Create module\n2. Verify")
app.processEvents()
check("Plan shows APPROVE & EXECUTE",
      lambda: None if window.approve_button.isVisibleTo(window)
      else (_ for _ in ()).throw(AssertionError("button hidden")))

started = {}
ApprovalPipeline.start = lambda self: started.setdefault("ws", self._workspace)
window.on_approve_plan()
app.processEvents()
check("APPROVE & EXECUTE starts pipeline in active workspace",
      lambda: None if started.get("ws") == str(tmp) else (_ for _ in ()).throw(
          AssertionError(f"workspace={started.get('ws')}")))


def _approve_diagnostics():
    assert window.diagnostic_status_label.text() == "STATUS: APPROVED"
    log = window.diagnostics.toPlainText()
    for expected in ("Plan approved", "Creating job queue..."):
        assert expected in log, expected


check("Approval writes live activity to Diagnostic", _approve_diagnostics)

# Stage events drive diagnostic status + log lines
window._on_stage_started("Planner")
window._on_stage_finished("Planner", True, "plan ready")
window._on_stage_started("Coding")
app.processEvents()


def _stage_activity():
    assert window.diagnostic_status_label.text() == "STATUS: CODING"
    log = window.diagnostics.toPlainText()
    assert "Planner started" in log
    assert "Coding started" in log
    assert "Planner completed" in log


check("Stage events appear live in Diagnostic (PLANNING→CODING)", _stage_activity)

# 10 ── Honest verification failure reporting
window._on_pipeline_finished(False, "Execution FAILED.\nTest suite is still failing.")
check("Verification failure reported honestly in chat",
      lambda: None if "FAILED" in window.conversation.toPlainText()
      else (_ for _ in ()).throw(AssertionError("not reported")))

# 11 ── Terminal launcher uses workspace
class FakePopen:
    last = None
    def __init__(self, cmd, cwd=None, creationflags=0, **kw):
        FakePopen.last = self
        self.cwd = cwd

mw.subprocess.Popen = FakePopen
window.launch_external_terminal("pwsh")
window.launch_external_terminal("cmd")
check("pwsh+cmd launch externally in active workspace",
      lambda: None if FakePopen.last.cwd == str(tmp)
      else (_ for _ in ()).throw(AssertionError(FakePopen.last.cwd)))

window.close()
app.processEvents()

print("\n=== SMOKE TEST RESULTS ===")
failures = 0
for name, status in results:
    safe = name.encode("ascii", "replace").decode()
    print(f"[{status}] {safe}")
    failures += status != "PASS"
print(f"\n{len(results) - failures}/{len(results)} checks passed")
sys.exit(1 if failures else 0)
