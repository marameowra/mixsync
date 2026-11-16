"""Stop hook for the implementer subagent: block finishing while `scripts/check.sh --fast` fails.

--fast skips pytest: the full suite runs once before pushing (/ship), not per implementer.
"""

import json
import subprocess
import sys
from pathlib import Path

data = json.load(sys.stdin)
if data.get("stop_hook_active"):  # already blocked once: let the agent report the failure
    print(json.dumps({"continue": True}))
    sys.exit(0)

cwd = data.get("cwd") or "."
root = subprocess.run(
    ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True
)
top = Path(root.stdout.strip())
if root.returncode or not (top / "scripts/check.sh").is_file():
    print(json.dumps({"continue": True}))
    sys.exit(0)  # not in a repo, or no check script: nothing to check
run = subprocess.run(["scripts/check.sh", "--fast"], cwd=top, capture_output=True, text=True)
if run.returncode:
    tail = "\n".join((run.stdout + run.stderr).splitlines()[-60:])
    print(f"CI checks failed; fix them before finishing:\n{tail}", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"continue": True}))
