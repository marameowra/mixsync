"""PreToolUse hook: Bash commands AGENTS.md reserves for humans are denied.

Denied (fail closed on unparseable gh/git commands):
- merging a PR into main (`gh pr merge`, merge via `gh api`);
- `gh pr create` with a base of main or no `--base` (gh defaults to main);
- creating release tags (`git tag v*`, `git push --tags/--follow-tags/<v* or refs/tags/*>`);
- `gh release create|edit|delete|upload`;
- approving deployments (`gh api` on `pending_deployments`).
"""

import json
import re
import shlex
import subprocess
import sys

REASON = "Merging into main is human-only (AGENTS.md). Leave the PR for the user."
PR_BASE_REASON = "PRs go to dev: pass --base dev (AGENTS.md)."
RELEASE_REASON = "Releases are human-only: no release tags or GitHub releases (AGENTS.md)."
DEPLOY_REASON = "Approving deployments is human-only (AGENTS.md)."
# Merge flags that consume the next token, so it is not mistaken for the selector.
VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-t", "--subject", "-F", "--body-file",
               "-A", "--author-email", "--match-head-commit"}  # fmt: skip
TAG_VALUE_FLAGS = {"-m", "--message", "-F", "--file", "-u", "--local-user"}
TAG_REFSPEC = re.compile(r"refs/tags/|^\+?v\d|:v\d")
RELEASE_VERBS = {"create", "edit", "delete", "upload"}
API_MERGE = re.compile(r"pulls/\d+/merge|mergePullRequest")


def deny(reason: str = REASON) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))  # fmt: skip
    sys.exit(0)


def segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    # Unquoted newlines are whitespace; a stray token can only skew the selector (fails closed).
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
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


def pr_create_base(args: list[str]) -> str | None:
    it = iter(args)
    for tok in it:
        if tok in ("--base", "-B"):
            return next(it, None)
        if tok.startswith("--base="):
            return tok.split("=", 1)[1]
    return None


def creates_tag(args: list[str]) -> bool:
    if {"-l", "--list", "-d", "--delete"} & set(args):
        return False
    it = iter(args)
    for tok in it:
        if tok in TAG_VALUE_FLAGS:
            next(it, None)
        elif re.match(r"v\d", tok):
            return True
    return False


def pushes_tag(args: list[str]) -> bool:
    return any(
        t == "--tags" or t.startswith("--follow-tags") or TAG_REFSPEC.search(t) for t in args
    )


def main() -> None:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
        if not re.search(r"\b(gh|git)\b", command) or not re.search(
            r"merge|create|tag|push|release|pending_deployments", command
        ):
            return  # cheap prefilter: can't be a guarded command, skip parsing
        segs = segments(command)
    except (ValueError, KeyError, TypeError):
        deny()  # bad input or unparseable gh/git command: fail closed
        return
    for seg in segs:
        for i, tok in enumerate(seg):
            rest = seg[i + 1 :]
            if tok == "git":
                if "tag" in rest and creates_tag(rest[rest.index("tag") + 1 :]):
                    deny(RELEASE_REASON)
                if "push" in rest and pushes_tag(rest[rest.index("push") + 1 :]):
                    deny(RELEASE_REASON)
            if tok != "gh":
                continue
            if rest[:2] == ["pr", "merge"] and base_is_main(*merge_target(rest[2:])):
                deny()
            if rest[:2] == ["pr", "create"] and pr_create_base(rest[2:]) in (None, "main"):
                deny(PR_BASE_REASON)
            if rest[:1] == ["release"] and rest[1:2] and rest[1] in RELEASE_VERBS:
                deny(RELEASE_REASON)
            if rest[:1] == ["api"] and any(API_MERGE.search(t) for t in rest):
                deny()
            if rest[:1] == ["api"] and any("pending_deployments" in t for t in rest):
                deny(DEPLOY_REASON)


if __name__ == "__main__":
    main()
