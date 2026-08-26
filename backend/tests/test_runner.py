import sys
from app.services.runner import TestRunner


def test_runner_invalidates_stale_python_bytecode(tmp_path):
    (tmp_path / "demo.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "check_demo.py").write_text(
        "from demo import add\n"
        "assert add(2, 3) == EXPECTED\n",
        encoding="utf-8",
    )

    runner = TestRunner()

    first = runner.run(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import demo; assert demo.add(2, 3) == 5",
        ],
    )
    assert first.passed is False

    # Same-size rewrite: this is the Windows stale-.pyc failure mode.
    (tmp_path / "demo.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    second = runner.run(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import demo; assert demo.add(2, 3) == 5",
        ],
    )
    assert second.passed is True
