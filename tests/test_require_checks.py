import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / ".claude" / "hooks" / "require_checks.py"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def stop(repo: Path, **extra: object) -> subprocess.CompletedProcess[str]:
    # Codex SubagentStop and Claude Stop payloads share these fields
    payload = {"hook_event_name": "SubagentStop", "agent_type": "implementer", "cwd": str(repo)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload | extra),
        capture_output=True,
        text=True,
    )


def write_check(repo: Path, code: int) -> None:
    script = repo / "scripts" / "check.sh"
    script.parent.mkdir()
    script.write_text(f"#!/bin/sh\necho ruff failed\nexit {code}\n")
    script.chmod(0o755)


def allowed(res: subprocess.CompletedProcess[str]) -> bool:
    return res.returncode == 0 and json.loads(res.stdout) == {"continue": True}


def test_failing_check_blocks(repo: Path) -> None:
    write_check(repo, 1)
    res = stop(repo)
    assert res.returncode == 2
    assert "ruff failed" in res.stderr


def test_passing_check_allows(repo: Path) -> None:
    write_check(repo, 0)
    assert allowed(stop(repo))


def test_second_stop_allowed(repo: Path) -> None:
    write_check(repo, 1)
    assert allowed(stop(repo, stop_hook_active=True))


def test_missing_script_allows(repo: Path) -> None:
    assert allowed(stop(repo))


def test_outside_repo_allows(tmp_path: Path) -> None:
    assert allowed(stop(tmp_path))
