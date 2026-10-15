"""PreToolUse hook: agents may not merge PRs into main (AGENTS.md)."""

import json
import re
import shlex
import subprocess
import sys

REASON = "Merging into main is human-only (AGENTS.md). Leave the PR for the user."
# Merge flags that consume the next token, so it is not mistaken for the selector.
VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-t", "--subject", "-F", "--body-file",
               "-A", "--author-email", "--match-head-commit"}  # fmt: skip
API_MERGE = re.compile(r"pulls/\d+/merge|mergePullRequest")


def deny() -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON,
    }}))  # fmt: skip
    sys.exit(0)


def segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for line in command.splitlines():
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        seg: list[str] = []
        for tok in lex:
            if re.fullmatch(r"[;&|()<>]+", tok):  # operator token: new segment
                out.append(seg)
                seg = []
            else:
                seg.append(tok)
        out.append(seg)
    return out


def merge_target(args: list[str]) -> tuple[str | None, str | None]:
    selector = repo = None
    it = iter(args)
    for tok in it:
        if tok in ("-R", "--repo"):
            repo = next(it, None)
        elif tok.startswith("--repo="):
            repo = tok.split("=", 1)[1]
        elif tok in VALUE_FLAGS:
            next(it, None)
        elif not tok.startswith("-") and selector is None:
            selector = tok
    return selector, repo


def base_is_main(selector: str | None, repo: str | None) -> bool:
    cmd = ["gh", "pr", "view", *([selector] if selector else [])]
    cmd += ["--repo", repo] if repo else []
    cmd += ["--json", "baseRefName", "-q", ".baseRefName"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return True  # fail closed
    return res.returncode != 0 or res.stdout.strip() == "main"


def main() -> None:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
        segs = segments(command)
    except (ValueError, KeyError, TypeError):
        deny()  # unparseable: fail closed
        return
    for seg in segs:
        for i, tok in enumerate(seg):
            if tok != "gh":
                continue
            rest = seg[i + 1 :]
            if rest[:2] == ["pr", "merge"] and base_is_main(*merge_target(rest[2:])):
                deny()
            if rest[:1] == ["api"] and any(API_MERGE.search(t) for t in rest):
                deny()


main()
