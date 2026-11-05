import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "closing_issues.sh"


def closing(body: str) -> list[str]:
    out = subprocess.run([str(SCRIPT)], input=body, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_parses_closing_keywords():
    body = "Fixes #12\ncloses #3, RESOLVED: #7\nFixes #12\nsee #99\nprefixes #5"
    assert closing(body) == ["3", "7", "12"]


def test_no_keywords():
    assert closing("nothing here, ref #4") == []
