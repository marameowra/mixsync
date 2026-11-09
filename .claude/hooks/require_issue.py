"""PreToolUse hook: `gh pr create` is denied unless its body links a GitHub issue.

Accepted: a closing keyword (`Fixes #12`, `closes #3`, `Resolved: #7`) or `Refs #12` for
partial work, in `--body`/`-b` or a readable `--body-file`/`-F`. `--fill` and stdin
bodies can't be checked, so they are denied (fail closed).
"""

import json
import re
import sys
from pathlib import Path

from guard_main_merge import deny, segments

REASON = (
    "Every change is tracked in a GitHub issue (AGENTS.md). Find or open one with "
    "`gh issue create`, then put `Fixes #<n>` (or `Refs #<n>` for partial work) in the PR body."
)
ISSUE_REF = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?):? +#\d+", re.I)


def body_text(args: list[str]) -> str:
    out = []
    it = iter(args)
    for tok in it:
        flag, eq, val = tok.partition("=")
        if flag in ("--body", "-b", "--body-file", "-F"):
            val = val if eq else next(it, None) or ""
            if flag in ("--body-file", "-F"):
                try:
                    val = Path(val).read_text() if val != "-" else ""
                except OSError:
                    val = ""
            out.append(val)
    return "\n".join(out)


def main() -> None:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
        if not re.search(r"\bgh\b", command) or "create" not in command:
            return  # cheap prefilter
        segs = segments(command)
    except (ValueError, KeyError, TypeError):
        deny(REASON)
        return
    for seg in segs:
        for i, tok in enumerate(seg):
            rest = seg[i + 1 :]
            if (
                tok == "gh"
                and rest[:2] == ["pr", "create"]
                and not ISSUE_REF.search(body_text(rest[2:]))
            ):
                deny(REASON)


if __name__ == "__main__":
    main()
