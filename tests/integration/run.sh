#!/usr/bin/env bash
# Runs the compose integration suite: builds the images, starts the stack, runs pytest in it,
# then removes the stack and its throwaway volumes. Extra arguments go to pytest (e.g. -k happy).
set -euo pipefail
cd "$(dirname "$0")"

compose=(docker compose -f compose.yml)
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color --tail=200 mixsync-worker mixsync-web fakes navidrome || true
  fi
  # only this project's containers and its named volumes (config, downloads, data): test data
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "$status"
}
trap cleanup EXIT

"${compose[@]}" build
"${compose[@]}" run --rm runner "$@"
