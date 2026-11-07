#!/usr/bin/env bash
# Runs every CI step (mirrors .github/workflows/ci.yml `check`).
# --all: no docs-only shortcut. --fast: skip pytest (run the relevant tests yourself).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

all="" fast=""
for a in "$@"; do
  case "$a" in
    --all) all=1 ;;
    --fast) fast=1 ;;
    *) echo "usage: scripts/check.sh [--all] [--fast]" >&2; exit 2 ;;
  esac
done

if [ -z "$all" ]; then
  # Any git failure (e.g. no origin/dev) falls through to running everything.
  if changed=$({ git diff --no-renames --name-only "$(git merge-base origin/dev HEAD)" &&
    git ls-files --others --exclude-standard; } 2>/dev/null); then
    if [ -z "$changed" ] || ! printf '%s\n' "$changed" | grep -qvE '^docs/|\.md$'; then
      echo "docs-only change: skipping checks"
      exit 0
    fi
  fi
fi

uv run ruff check
uv run ruff format --check
uv run pyright
uv run lint-imports
[ -n "$fast" ] || uv run pytest -n auto
