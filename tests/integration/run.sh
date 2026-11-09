#!/usr/bin/env bash
# Runs the compose integration suite: builds the images, starts the stack, runs pytest in it,
# then removes the stack and its throwaway volumes. Extra arguments go to pytest (e.g. -k happy).
set -euo pipefail
cd "$(dirname "$0")"

# per-run project and image tags: parallel runs (e.g. two worktrees) never share or remove
# each other's containers, volumes or images
export IT_TAG="$$"
compose=(docker compose -p "mixsync-it-$IT_TAG" -f compose.yml)
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color --tail=200 mixsync-worker mixsync-web fakes navidrome || true
  fi
  # only this project's containers and its named volumes (config, downloads, data): test data
  "${compose[@]}" down --volumes --remove-orphans || true
  docker image rm "mixsync:it-$IT_TAG" "mixsync-it-runner:$IT_TAG" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

"${compose[@]}" build
"${compose[@]}" run --rm runner "$@"
