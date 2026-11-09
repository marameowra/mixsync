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
        # create then merge in one command: the PR doesn't exist yet when the hook runs
        ("gh pr create --base dev --title x && gh pr merge --auto --merge", "fail", False),
        ("gh pr create --base dev --title x; gh pr merge --auto", "main", True),
        ("gh pr merge --auto && gh pr create --base dev --title x", "fail", True),
        ("gh pr create --base dev --title x && gh pr merge 12 --auto", "fail", True),
        ("gh api repos/x/y/pulls/3/merge -X PUT", "dev", True),
        ("gh api graphql -f query='mutation{mergePullRequest(input:{})}'", "dev", True),
        ("gh pr merge 'unterminated", "dev", True),
        ("gh pr create --title x", "dev", True),
        ("gh pr create --base main", "dev", True),
        ("gh pr create --base=main --fill", "dev", True),
        ("gh pr create -B main", "dev", True),
        ("gh pr create --base dev --title x", "dev", False),
        ("gh pr create --base=dev", "dev", False),
        ("gh pr create -B feat/x", "dev", False),
        ("git tag v1.0.0", "dev", True),
        ("git tag -a v1.0.0 -m 'release'", "dev", True),
        ("git tag -s v2", "dev", True),
        ("git tag -l", "dev", False),
        ("git tag --list 'v*'", "dev", False),
        ("git tag -d v1.0.0", "dev", False),
        ("git tag -a nightly -m 'v1 notes'", "dev", False),
        ("git push --tags", "dev", True),
        ("git push origin --follow-tags", "dev", True),
        ("git push origin v1.0.0", "dev", True),
        ("git push origin HEAD:refs/tags/foo", "dev", True),
        ("git push origin feat/x", "dev", False),
        ("git push -u origin chore/guard-hooks", "dev", False),
        ("gh release create v1.0.0", "dev", True),
        ("gh release edit v1.0.0", "dev", True),
        ("gh release delete v1.0.0", "dev", True),
        ("gh release upload v1.0.0 f.zip", "dev", True),
        ("gh release list", "dev", False),
        ("gh api repos/o/r/actions/runs/1/pending_deployments -X POST", "dev", True),
        ("gh api repos/o/r/pulls/3", "dev", False),
    ],
)
def test_merge_decisions(tmp_path: Path, command: str, base: str, deny: bool) -> None:
    out, _ = run_hook(tmp_path, command, base)
    assert denied(out) is deny


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("gh pr create --fill", "PRs go to dev: pass --base dev (AGENTS.md)."),
        ("git tag v1.0.0", "Releases are human-only"),
        ("gh release create v1", "Releases are human-only"),
        ("gh api x/pending_deployments", "Approving deployments"),
    ],
)
def test_deny_reasons(tmp_path: Path, command: str, reason: str) -> None:
    out, _ = run_hook(tmp_path, command, "dev")
    assert reason in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_repo_and_selector_passed_to_gh_pr_view(tmp_path: Path) -> None:
    _, calls = run_hook(tmp_path, "gh pr merge -R o/r 7 --merge", "dev")
    assert calls.split() == [
        "pr", "view", "7", "--repo", "o/r", "--json", "baseRefName", "-q", ".baseRefName",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 3",
        "git status",
        'echo "line one\nline two"',
        "cat <<'EOF'\ndon't merge this\nEOF",
        "echo 'unterminated",
    ],
)
def test_unrelated_commands_allowed_without_calling_gh(tmp_path: Path, command: str) -> None:
    out, calls = run_hook(tmp_path, command, "main")
    assert out == ""
    assert calls == ""
