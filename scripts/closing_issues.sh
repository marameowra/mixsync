#!/usr/bin/env bash
# Reads text on stdin, prints the unique issue numbers it closes via GitHub keywords (Fixes #1, ...).
grep -oiE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?):? +#[0-9]+' | grep -oE '[0-9]+$' | sort -un || true
