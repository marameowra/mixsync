import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "guard_main_merge.py"

FAKE_GH = """#!/bin/sh
echo "$@" >> "$GH_CALLS"
[ "$GH_BASE" = fail ] && exit 1
echo "$GH_BASE"
"""


def run_hook(tmp_path: Path, command: str, base: str) -> tuple[str, str]:
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "GH_BASE": base,
        "GH_CALLS": str(calls),
    }
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return res.stdout, calls.read_text() if calls.exists() else ""


def denied(out: str) -> bool:
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny" if out else False


@pytest.mark.parametrize(
    ("command", "base", "deny"),
    [
        ("gh pr merge 12 --auto --merge", "main", True),
        ("gh pr merge 12 --auto --merge", "dev", False),
        ("git push && gh pr merge 12 --auto --merge", "main", True),
        ("gh pr merge --merge", "main", True),
        ("gh pr merge 12", "fail", True),
        ("gh api repos/x/y/pulls/3/merge -X PUT", "dev", True),
        ("gh api graphql -f query='mutation{mergePullRequest(input:{})}'", "dev", True),
        ("gh pr merge 'unterminated", "dev", True),
    ],
)
def test_merge_decisions(tmp_path: Path, command: str, base: str, deny: bool) -> None:
    out, _ = run_hook(tmp_path, command, base)
    assert denied(out) is deny


def test_repo_and_selector_passed_to_gh_pr_view(tmp_path: Path) -> None:
    _, calls = run_hook(tmp_path, "gh pr merge -R o/r 7 --merge", "dev")
    assert calls.split() == [
        "pr", "view", "7", "--repo", "o/r", "--json", "baseRefName", "-q", ".baseRefName",
    ]  # fmt: skip


@pytest.mark.parametrize("command", ["gh pr view 3", "git status"])
def test_unrelated_commands_allowed_without_calling_gh(tmp_path: Path, command: str) -> None:
    out, calls = run_hook(tmp_path, command, "main")
    assert out == ""
    assert calls == ""
