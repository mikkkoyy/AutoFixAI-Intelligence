from pathlib import Path
import ast


def test_ui_source_parses():
    source = Path("frontend/app/ui/main_window.py").read_text(encoding="utf-8")
    ast.parse(source)
