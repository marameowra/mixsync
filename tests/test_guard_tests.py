import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "guard_tests.py"

# Built by concatenation so this file never trips the guard itself.
SKIP = "pytest.mark." + "skip"
XFAIL = "pytest.mark." + "xfail"
SKIP_CALL = "pytest." + "skip("


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    return tmp_path


def decide(repo: Path, tool: str, tool_input: dict[str, Any]) -> bool:
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(repo)}),
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(res.stdout) and (
        json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )


@pytest.mark.parametrize(
    ("command", "deny"),
    [
        ("rm tests/test_a.py", True),
        ("rm -rf tests", True),
        ("git rm tests/unit/test_a.py", True),
        ("unlink tests/test_a.py", True),
        ("truncate -s 0 tests/test_a.py", True),
        ("ls && rm ./tests/unit/x.py", True),
        ("echo > tests/test_a.py", True),
        ("echo hi >tests/test_a.py", True),
        ("echo hi >> tests/test_a.py", False),
        ("cat tests/test_a.py", False),
        ("mv tests/test_a.py /tmp/elsewhere.py", False),
        ("rm src/old.py", False),
        ("echo > src/out.txt", False),
        ("uv run pytest tests/", False),
    ],
)
def test_bash(repo: Path, command: str, deny: bool) -> None:
    assert decide(repo, "Bash", {"command": command}) is deny


def test_bash_resolves_relative_to_cwd(repo: Path) -> None:
    sub = repo / "tests" / "unit"
    sub.mkdir()
    assert decide(sub, "Bash", {"command": "rm test_a.py"}) is True
    assert decide(repo / "src", "Bash", {"command": "rm test_a.py"}) is False


def test_write_empty_denied(repo: Path) -> None:
    f = {"file_path": str(repo / "tests" / "test_a.py")}
    assert decide(repo, "Write", {**f, "content": ""}) is True
    assert decide(repo, "Write", {**f, "content": " \n"}) is True
    assert decide(repo, "Write", {**f, "content": "def test_a(): ...\n"}) is False
    other = {"file_path": str(repo / "src" / "x.py"), "content": ""}
    assert decide(repo, "Write", other) is False


def test_edit_adding_marker_denied(repo: Path) -> None:
    f = str(repo / "tests" / "test_a.py")
    add = {"file_path": f, "old_string": "def t(): ...", "new_string": f"@{SKIP}\ndef t(): ..."}
    assert decide(repo, "Edit", add) is True
    assert decide(repo, "Edit", {**add, "new_string": "def t(): pass"}) is False
    assert decide(repo, "Edit", {**add, "new_string": f"{XFAIL} {SKIP_CALL})"}) is True
    other = {**add, "file_path": str(repo / "src" / "x.py")}
    assert decide(repo, "Edit", other) is False


def test_edit_keeping_existing_marker_allowed(repo: Path) -> None:
    f = str(repo / "tests" / "test_a.py")
    keep = {"file_path": f, "old_string": f"@{SKIP}\nA", "new_string": f"@{SKIP}\nB"}
    assert decide(repo, "Edit", keep) is False
    drop = {**keep, "new_string": "B"}
    assert decide(repo, "Edit", drop) is False


def test_multiedit_sums_markers(repo: Path) -> None:
    f = str(repo / "tests" / "test_a.py")
    swap = {"old_string": f"@{SKIP}", "new_string": "x"}
    add = {"old_string": "y", "new_string": f"@{SKIP}"}
    assert decide(repo, "MultiEdit", {"file_path": f, "edits": [swap, add]}) is False
    assert decide(repo, "MultiEdit", {"file_path": f, "edits": [add, add]}) is True


def test_write_compares_with_file_on_disk(repo: Path) -> None:
    f = repo / "tests" / "test_a.py"
    f.write_text(f"@{SKIP}\ndef t(): ...\n")
    same = {"file_path": str(f), "content": f"@{SKIP}\ndef t(): pass\n"}
    assert decide(repo, "Write", same) is False
    more = {"file_path": str(f), "content": f"@{SKIP}\n@{SKIP}\ndef t(): pass\n"}
    assert decide(repo, "Write", more) is True
    new = {"file_path": str(repo / "tests" / "test_b.py"), "content": f"@{SKIP}\n"}
    assert decide(repo, "Write", new) is True


def test_unparseable_input_denied() -> None:
    res = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, check=True
    )
    assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_deny_reason(repo: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "rm tests/a.py"}, "cwd": str(repo)}
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    reason = json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == (
        "Tests may not be deleted, emptied, or skipped without asking the user first (AGENTS.md)."
    )
