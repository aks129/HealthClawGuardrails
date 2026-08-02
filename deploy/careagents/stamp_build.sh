#!/usr/bin/env bash
# Stamp the CareAgents build marker into <target-dir>/careagents/BUILD_SHA.
#
#   ./deploy/careagents/stamp_build.sh <target-dir>
#
# The one place that knows the marker's format. Both deploy paths call it —
# deploy/careagents/deploy.sh before its rsync, and by hand against the staging
# dir before `railway up` — so the two cannot drift into disagreeing about what
# a marker means. careagents/_build.py is the only reader.
#
# Two lines:
#
#   <short sha, 12 hex, plus -dirty when the tree is not clean>
#   <unix timestamp of that commit>
#
# A dirty tree is stamped as such on purpose: `-dirty` matches no acceptable
# sha in scripts/prod_watch.py, so deploying uncommitted code stays visible
# rather than passing as whatever commit it was built near.
#
# Generated at deploy time, never committed (.gitignore) — a checked-in marker
# goes stale silently, which is the failure it exists to catch (#258).
set -euo pipefail

TARGET="${1:-}"
# From the script's own location, so this works invoked from anywhere and with
# a target outside the repo — the staging-dir case, where a relative path or a
# bare `git` would resolve to the wrong tree or to no tree at all.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [ -z "$TARGET" ]; then
  echo "usage: $0 <target-dir>   # the tree about to be deployed" >&2
  exit 2
fi
if [ ! -d "$TARGET/careagents" ]; then
  # Refuse rather than write nothing. A missing marker degrades to "unknown",
  # which is safe for the app but quietly defeats the deploy check — the whole
  # point of stamping.
  echo "stamp_build: no careagents/ under $TARGET — wrong target directory?" >&2
  exit 1
fi

SHA="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
# An `if`, not `a && b`: under `set -e` the latter would abort the deploy on a
# clean tree, which is the common case.
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  SHA="$SHA-dirty"
fi

printf '%s\n%s\n' "$SHA" "$(git -C "$REPO_ROOT" log -1 --format=%ct)" \
  > "$TARGET/careagents/BUILD_SHA"
echo "build $SHA"
