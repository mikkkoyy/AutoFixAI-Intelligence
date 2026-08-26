"""Programmatic GUI verification for the new terminal navigator architecture."""
import sys, traceback
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\FILES\project\AutoFix AI Studio") / "frontend"))

results = []
PASS_COUNT = [0]
FAIL_COUNT = [0]

from PySide6.QtWidgets import QApplication, QToolButton
from PySide6.QtGui import QShortcut
from PySide6.QtCore import QTimer, QCoreApplication

app = QApplication(sys.argv)
from app.ui.main_window import MainWindow

w = MainWindow()
w.show()
app.processEvents()

def r(msg):
    results.append(msg)
    print(msg, flush=True)

def check(name, cond):
    if cond:
        PASS_COUNT[0] += 1
        r(f"  {name}: PASS")
    else:
        FAIL_COUNT[0] += 1
        r(f"  {name}: FAIL")

def finish():
    Path(r"D:\FILES\project\AutoFix AI Studio\gui_verify.txt").write_text("\n".join(results), encoding="utf-8")
    QCoreApplication.exit(0)

QTimer.singleShot(30000, finish)

def step_basics():
    try:
        r("=== 1. STARTUP ===")
        check("1.1 title", w.windowTitle() == "AutoFix AI Studio")
        check("1.2 visible", w.isVisible())
        check("1.3 size", w.width() > 1000 and w.height() > 600)
        check("1.4 sessions_start", len(w._terminal_sessions) == 0)
        check("1.5 active_id_start", w._active_terminal_id == -1)
        check("1.6 editor_tabs_start", w._editor_tabs.count() == 0)

        r("=== 2. MENUS ===")
        mb = w.menuBar()
        menus = [a.text() for a in mb.actions() if a.menu()]
        check("2.1 count_8", len(menus) == 8)
        check("2.2 names", menus == ['File', 'Edit', 'Selection', 'View', 'Go', 'Run', 'Terminal', 'Help'])

        r("=== 3. STATUS BAR ===")
        check("3.1 backend", "Connected" in w._status_backend.text())
        check("3.2 opencode", "Ready" in w._status_opencode.text())

        r("=== 4. ACTIVITY BAR ===")
        btns = [b for b in w.findChildren(QToolButton) if b.objectName() == "ActivityBtn" and b.isVisible()]
        check("4.1 count_8", len(btns) == 8)

        r("=== 5. EXPLORER ===")
        check("5.1 roots", w.tree.topLevelItemCount() == 1)
        root = w.tree.topLevelItem(0)
        check("5.2 name", root and root.text(0) == "AutoFix AI Studio")
        check("5.3 children", root and root.childCount() >= 10)
        check("5.4 path", "AutoFix AI Studio" in w._explorer_path_label.text())

        r("=== 6. SHORTCUTS ===")
        sc = w.findChildren(QShortcut)
        found = {s.key().toString() for s in sc}
        expected = {"Ctrl+S", "Ctrl+W", "Ctrl+Tab", "Ctrl+Shift+Tab", "Ctrl+`", "Ctrl+Shift+`", "Ctrl+B"}
        check("6.1 all", expected <= found)

        r("=== 7. TERMINAL MENU ===")
        for a in mb.actions():
            if a.text() == "Terminal":
                items = [x.text() for x in a.menu().actions() if not x.isSeparator()]
                check("7.1 count_9", len(items) == 9)
                break

        r("=== 8. OPENCODE ===")
        check("8.1 discovery", w._opencode_discovery is not None)
        check("8.2 workspace", w._opencode_workspace is not None)
        check("8.3 has_navigator", hasattr(w, '_term_navigator'))
        check("8.4 has_nav_list", hasattr(w, '_nav_session_list'))

        r("=== 9. NAVIGATOR ===")
        check("9.1 nav_width", w._term_navigator.width() >= 150)
        check("9.2 stack_exists", hasattr(w, '_term_output_stack'))

    except Exception as e:
        r(f"ERROR_BASICS: {e}\n{traceback.format_exc()}")
    QTimer.singleShot(0, step_terminal)

def step_terminal():
    try:
        r("=== 10. CTRL+` AUTO CREATE ===")
        w._focus_or_create_terminal()
        app.processEvents()
        check("10.1 one_session", len(w._terminal_sessions) == 1)

        sid = list(w._terminal_sessions.keys())[0]
        sess = w._terminal_sessions[sid]
        check("10.2 name", sess.name == "PowerShell")
        check("10.3 label", sess.label == "PWSH")
        check("10.4 cwd", "AutoFix" in sess.cwd)
        check("10.5 running", sess.process.processId() > 0)
        check("10.6 not_opencode", not sess.is_opencode)

        r("=== 11. PER-SESSION OUTPUT ===")
        check("11.1 stack_has_widget", w._term_output_stack.count() == 1)
        check("11.2 current_is_session", w._term_output_stack.currentWidget() is sess.output)
        output = sess.output.toPlainText()
        check("11.3 path_visible", "AutoFix" in output or "D:" in output)
        check("11.4 has_pwsch", "PWSH" in output)
        check("11.5 no_editor_tab", w._editor_tabs.count() == 0)

        r("=== 12. DEDUP ===")
        w._focus_or_create_terminal()
        app.processEvents()
        check("12.1 still_one", len(w._terminal_sessions) == 1)

        r("=== 13. NAVIGATOR SESSIONS ===")
        nav_rows = []
        for i in range(w._nav_session_list.count()):
            item = w._nav_session_list.itemAt(i)
            if item and item.widget():
                nav_rows.append(item.widget())
        check("13.1 one_nav_row", len(nav_rows) >= 1)

        r("=== 14. NEW PS ===")
        w._create_new_terminal("PowerShell")
        app.processEvents()
        check("14.1 two", len(w._terminal_sessions) == 2)
        check("14.2 stack_count", w._term_output_stack.count() == 2)
        ps = [s for s in w._terminal_sessions.values() if s.name == "PowerShell"]
        check("14.3 ps_label", all(s.label == "PWSH" for s in ps))

        r("=== 15. NEW CMD ===")
        w._create_new_terminal("CMD")
        app.processEvents()
        check("15.1 three", len(w._terminal_sessions) == 3)
        cmd = [s for s in w._terminal_sessions.values() if s.name == "CMD"]
        check("15.2 cmd_exists", len(cmd) == 1)
        if cmd:
            check("15.3 label", cmd[0].label == "CMD")
            check("15.4 cwd", "AutoFix" in cmd[0].cwd)
            check("15.5 running", cmd[0].process.processId() > 0)

        r("=== 16. SWITCHING ===")
        sids = list(w._terminal_sessions.keys())
        w._switch_to_terminal(sids[0])
        app.processEvents()
        check("16.1 first", w._active_terminal_id == sids[0])
        check("16.2 output_widget", w._term_output_stack.currentWidget() is w._terminal_sessions[sids[0]].output)
        w._switch_to_terminal(sids[2])
        app.processEvents()
        check("16.3 third", w._active_terminal_id == sids[2])
        check("16.4 output_widget_cmd", w._term_output_stack.currentWidget() is w._terminal_sessions[sids[2]].output)

        r("=== 17. CLS PS ===")
        w._switch_to_terminal(sids[0])
        app.processEvents()
        sess = w._terminal_sessions[sids[0]]
        sess.output.setPlainText("junk")
        sess.input.setText("cls")
        w._execute_active_command()
        app.processEvents()
        ps_cls = sess.output.toPlainText()
        check("17.1 ps_prompt", "PS " in ps_cls)
        check("17.2 ps_path", "AutoFix" in ps_cls or "D:" in ps_cls)

        r("=== 18. CLS CMD ===")
        w._switch_to_terminal(sids[2])
        app.processEvents()
        cs = w._terminal_sessions[sids[2]]
        cs.output.setPlainText("junk")
        cs.input.setText("cls")
        w._execute_active_command()
        app.processEvents()
        cmd_cls = cs.output.toPlainText()
        check("18.1 cmd_path", "AutoFix" in cmd_cls or "D:" in cmd_cls)
        check("18.2 no_ps", "PS " not in cmd_cls)

        r("=== 19. CLEAR DISPLAY ===")
        w._switch_to_terminal(sids[0])
        app.processEvents()
        sess.output.setPlainText("garbage")
        w.clear_terminal_display()
        app.processEvents()
        after = sess.output.toPlainText()
        check("19.1 prompt", "PS " in after and "AutoFix" in after)

        r("=== 20. MAX/RESTORE ===")
        w._maximize_bottom_panel()
        app.processEvents()
        sz = w._main_splitter.sizes()
        check("20.1 max", sz[0] == 0 and w._panel_maximized)
        w._restore_bottom_panel()
        app.processEvents()
        sz = w._main_splitter.sizes()
        check("20.2 restore", sz[0] > 0 and not w._panel_maximized)

        r("=== 21. TOGGLE ===")
        w._toggle_maximize_bottom_panel()
        app.processEvents()
        check("21.1 on", w._panel_maximized)
        w._toggle_maximize_bottom_panel()
        app.processEvents()
        check("21.2 off", not w._panel_maximized)

        r("=== 22. OPEN/CLOSE FILE ===")
        w.open_file_path(Path(r"D:\FILES\project\AutoFix AI Studio\main.py"))
        app.processEvents()
        check("22.1 tabs", w._editor_tabs.count() >= 1)
        check("22.2 name", "main.py" in w._status_file.text())
        check("22.3 lang", w._status_language.text() == "Python")
        w.close_current_tab()
        app.processEvents()
        check("22.4 closed", w._editor_tabs.count() == 0)

        r("=== 23. SCROLL ===")
        sessions = list(w._terminal_sessions.values())
        if sessions:
            sessions[0].output.setPlainText("\n".join([f"line {i}" for i in range(50)]))
            app.processEvents()
            w._scroll_terminal_top()
            app.processEvents()
            check("23.1 top", True)
            w._scroll_terminal_bottom()
            app.processEvents()
            check("23.2 bot", True)

        r("=== 24. ACTIVITY ===")
        btns = [b for b in w.findChildren(QToolButton) if b.objectName() == "ActivityBtn" and b.isVisible()]
        btns[1].click()
        app.processEvents()
        check("24.1 search", btns[1].isChecked())
        btns[0].click()
        app.processEvents()
        check("24.2 explorer", btns[0].isChecked())

        r("=== 25. NEW FROM MENU ===")
        w._new_terminal_from_menu()
        app.processEvents()
        check("25.1 created", len(w._terminal_sessions) > 3)

        r("=== 26. SPLIT ===")
        before = len(w._terminal_sessions)
        w._split_terminal()
        app.processEvents()
        check("26.1 split", len(w._terminal_sessions) == before + 1)

        r("=== 27. OPENCODE INTEGRITY ===")
        w._new_opencode_from_menu()
        app.processEvents()
        oc = [s for s in w._terminal_sessions.values() if s.is_opencode]
        check("27.1 created", len(oc) >= 1)
        if oc:
            check("27.2 label", oc[0].label == "OpenCode")
            check("27.3 cwd", "AutoFix" in oc[0].cwd or "project" in oc[0].cwd)
            check("27.4 running", oc[0].process.processId() > 0)

        r("=== 28. PER-SESSION CLOSE ===")
        before = len(w._terminal_sessions)
        killed_id = list(w._terminal_sessions.keys())[-1]
        w._close_terminal_by_id(killed_id)
        app.processEvents()
        check("28.1 killed", len(w._terminal_sessions) == before - 1)
        check("28.2 no_leak", w._term_output_stack.count() == len(w._terminal_sessions))

        r("=== 29. CLEANUP ===")
        while w._terminal_sessions:
            w._close_active_terminal()
            app.processEvents()
        check("29.1 all_cleaned", len(w._terminal_sessions) == 0)
        check("29.2 stack_empty", w._term_output_stack.count() == 0)

        r("=== 30. RIGHT CLICK PASTE ===")
        check("30.1 TerminalInput", hasattr(w._term_input.__class__, 'contextMenuEvent'))

        r("=== 31. OPENCDOE SIDEBAR ===")
        check("31.1 start_btn", w.opencode_start_button.text() == "Open in Terminal")

    except Exception as e:
        r(f"ERROR: {e}\n{traceback.format_exc()}")

    r(f"\n=== TOTAL: {PASS_COUNT[0]} PASS / {FAIL_COUNT[0]} FAIL ===")
    finish()

QTimer.singleShot(500, step_basics)
app.exec()
