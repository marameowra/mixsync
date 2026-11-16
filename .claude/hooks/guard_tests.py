"""PreToolUse hook: tests/ may not be deleted, emptied, or skipped (AGENTS.md).

Denies (fail closed on unparseable input):
- Bash: rm/unlink/truncate (incl. `git rm`) on a path under tests/, or a `>` redirect into it,
  unless the rm/unlink is a relocation: every `def test_*` in the file (or, for a file with
  none, its whole content) already exists in another file under tests/;
- Write of empty content to a file under tests/;
- Edit/MultiEdit/Write that increases the count of skip/xfail/only markers in a tests/ file.

Shortcut: `cd tests && rm x` is not caught (paths resolve against the input cwd only).
Shortcut: a test name found elsewhere counts as relocated even if its body changed.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

from guard_main_merge import segments

REASON = "Tests may not be deleted, emptied, or skipped without asking the user first (AGENTS.md)."
MARKERS = ("pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "pytest.skip(",
           "pytest.xfail(", "unittest.skip", ".only(")  # fmt: skip
REDIRECT = re.compile(r"&?>\|?")  # `>` but not `>>`
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)


def deny() -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON,
    }}))  # fmt: skip
    sys.exit(0)


def repo_root(cwd: Path) -> Path:
    return next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), cwd)


def in_tests(raw: str, cwd: Path) -> bool:
    path = (cwd / raw).resolve()
    root = repo_root(cwd)
    rel = path.relative_to(root).parts if path.is_relative_to(root) else ()
    return rel[:1] == ("tests",)


def relocated(targets: list[Path], cwd: Path) -> bool:
    """True if every target is a file whose tests (or content) live on in another tests/ file."""
    if not all(t.is_file() for t in targets):
        return False
    others = [
        f.read_text(errors="replace")
        for f in (repo_root(cwd) / "tests").rglob("*")
        if f.is_file() and f not in targets and "__pycache__" not in f.parts
    ]
    for t in targets:
        text = t.read_text(errors="replace")
        names = set(TEST_DEF.findall(text))
        if names:
            found = {n for o in others for n in TEST_DEF.findall(o)}
            if not names <= found:
                return False
        elif not text.strip() or text not in others:
            return False
    return True


def markers(text: str) -> int:
    return sum(text.count(m) for m in MARKERS)


def check_bash(command: str, cwd: Path) -> None:
    if "tests" not in command and "tests" not in cwd.parts:
        return
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    toks = list(lex)
    for op, target in zip(toks, toks[1:], strict=False):
        if REDIRECT.fullmatch(op) and in_tests(target, cwd):
            deny()
    for seg in segments(command):
        targets = [(cwd / a).resolve() for a in seg if in_tests(a, cwd)]
        if not targets or not {"rm", "unlink", "truncate"} & set(seg):
            continue
        if "truncate" in seg or not relocated(targets, cwd):
            deny()


def check_file(tool: str, inp: dict, cwd: Path) -> None:
    path = inp["file_path"]
    if ("tests" not in path and "tests" not in cwd.parts) or not in_tests(path, cwd):
        return
    if tool == "Write":
        new = inp["content"]
        if not new.strip():
            deny()
        target = cwd / path
        old = target.read_text(errors="replace") if target.is_file() else ""
        added = markers(new) - markers(old)
    elif tool == "Edit":
        added = markers(inp["new_string"]) - markers(inp["old_string"])
    else:
        added = sum(markers(e["new_string"]) - markers(e["old_string"]) for e in inp["edits"])
    if added > 0:
        deny()


def check_patch(patch: str, cwd: Path) -> None:
    """Check Codex's apply_patch input without trying to apply it."""
    if "*** Begin Patch" not in patch or "*** End Patch" not in patch:
        deny()
    current: Path | None = None
    operation = ""
    move_destination: Path | None = None
    hunks: list[tuple[list[str], list[str]]] = []
    old_lines: list[str] = []
    new_lines: list[str] = []

    def finish_hunk() -> None:
        nonlocal old_lines, new_lines
        if old_lines or new_lines:
            hunks.append((old_lines, new_lines))
        old_lines, new_lines = [], []

    def check_file_change(path: Path, deleted: bool = False) -> None:
        incoming_move = (
            move_destination is not None
            and in_tests(str(move_destination), cwd)
            and not in_tests(str(path), cwd)
        )
        if not in_tests(str(path), cwd) and not incoming_move:
            return
        target = (cwd / path).resolve()
        if deleted:
            if not relocated([target], cwd):
                deny()
            return
        if operation == "add":
            before: list[str] = []
            after = [line for _, additions in hunks for line in additions]
        else:
            if not target.is_file():
                deny()
            source_lines = target.read_text(errors="replace").splitlines()
            before = [] if incoming_move else source_lines
            after = source_lines.copy()
            for removed, added in hunks:
                if removed:
                    width = len(removed)
                    start = next(
                        (
                            i
                            for i in range(len(after) - width + 1)
                            if after[i : i + width] == removed
                        ),
                        None,
                    )
                    if start is None:
                        deny()
                    after[start : start + width] = added
                else:
                    after.extend(added)
        if not "\n".join(after).strip():
            deny()
        if markers("\n".join(after)) - markers("\n".join(before)) > 0:
            deny()

    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            finish_hunk()
            if current is not None:
                check_file_change(current)
            hunks = []
            current = Path(line.removeprefix("*** Update File: "))
            operation = "update"
            move_destination = None
        elif line.startswith("*** Delete File: "):
            finish_hunk()
            if current is not None:
                check_file_change(current)
            hunks = []
            operation = "delete"
            move_destination = None
            check_file_change(Path(line.removeprefix("*** Delete File: ")), deleted=True)
            current = None
        elif line.startswith("*** Add File: "):
            finish_hunk()
            if current is not None:
                check_file_change(current)
            hunks = []
            current = Path(line.removeprefix("*** Add File: "))
            operation = "add"
            move_destination = None
        elif line.startswith("*** Move to: ") and current is not None:
            destination = Path(line.removeprefix("*** Move to: "))
            move_destination = destination
            if in_tests(str(current), cwd) and not in_tests(str(destination), cwd):
                deny()
        elif line.startswith("@@"):
            finish_hunk()
        elif current is not None and line.startswith("+"):
            new_lines.append(line[1:])
        elif current is not None and line.startswith("-"):
            old_lines.append(line[1:])
        elif current is not None and line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
    if current is not None:
        finish_hunk()
        check_file_change(current)


def main() -> None:
    try:
        data = json.load(sys.stdin)
        tool, inp = data["tool_name"], data["tool_input"]
        cwd = Path(data.get("cwd") or os.getcwd()).resolve()
        if tool == "Bash":
            check_bash(inp["command"], cwd)
        elif tool == "apply_patch":
            check_patch(inp["command"], cwd)
        elif tool in ("Edit", "MultiEdit", "Write"):
            check_file(tool, inp, cwd)
    except (ValueError, KeyError, TypeError, OSError):
        deny()  # bad input: fail closed


if __name__ == "__main__":
    main()
