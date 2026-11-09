import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "require_issue.py"


def denied(command: str) -> bool:
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=True,
    )
    return "deny" in res.stdout


@pytest.mark.parametrize(
    ("command", "deny"),
    [
        ('gh pr create --base dev --body "Fixes #12"', False),
        ('gh pr create --base dev -b "summary\n\ncloses #3"', False),
        ('gh pr create --base dev --body="Refs #7"', False),
        ('gh pr create --base dev --body "$(cat <<EOF\nx\n\nResolved: #9\nEOF\n)"', False),
        ("gh pr create --base dev --fill", True),
        ('gh pr create --base dev --body "no issue here, see #4"', True),
        ('gh pr create --base dev --title "Fixes #12"', True),
        ("gh pr create --base dev --body-file -", True),
        ('git commit -m "x" && gh pr create --base dev --body "x"', True),
        ("gh issue create --title x", False),
        ("gh pr list", False),
        ("ls", False),
    ],
)
def test_pr_create_needs_issue(command: str, deny: bool) -> None:
    assert denied(command) is deny


def test_body_file(tmp_path: Path) -> None:
    linked, bare = tmp_path / "a.md", tmp_path / "b.md"
    linked.write_text("Fixes #1\n")
    bare.write_text("nothing\n")
    assert not denied(f"gh pr create --base dev --body-file {linked}")
    assert denied(f"gh pr create --base dev -F {bare}")
    assert denied(f"gh pr create --base dev -F {tmp_path / 'missing.md'}")
