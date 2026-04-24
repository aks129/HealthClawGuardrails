#!/usr/bin/env bash
# scripts/seed_openclaw_workspaces.sh
#
# OpenClaw gotcha: when you create agents via `openclaw agents add ...`,
# the CLI scaffolds IDENTITY.md/SOUL.md/USER.md but NOT BOOTSTRAP.md or
# MEMORY.md. On first conversation turn the agent notices the missing
# files, blocks on "bootstrap incomplete", and refuses to handle user
# commands (including /dashboard).
#
# This script idempotently seeds the missing files for every agent
# workspace on the Mac mini. Safe to re-run; skips files that exist.
#
# Run from the laptop:
#   bash scripts/seed_openclaw_workspaces.sh

set -euo pipefail
SSH_USER="${SSH_USER:-coopeydoop}"
SSH_HOST="${SSH_HOST:-192.168.5.121}"
REMOTE="${SSH_USER}@${SSH_HOST}"

ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" true \
  || { echo "ERROR: cannot SSH to $REMOTE" >&2; exit 1; }

ssh "$REMOTE" 'bash -s' <<'REMOTE'
set +e
TODAY=$(date +%Y-%m-%d)
YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d 'yesterday' +%Y-%m-%d)

bootstrap_content() {
cat <<'EOF'
# BOOTSTRAP — already complete

This workspace is fully provisioned. **Bootstrap is DONE.** Do not ask
the user to create any more files; do not treat missing optional files
as a blocker.

## Required reading order (already satisfied)

1. `IDENTITY.md` — who I am
2. `SOUL.md` — values + defaults
3. `USER.md` — facts about the person I serve
4. `AGENTS.md` — capabilities + slash-command dispatch table
5. `MEMORY.md` — index of daily notes (optional)

## What to do next

On the very next user message, skip any bootstrap checklist and just
**respond to the request**. If the user sent `/dashboard`, `/health`,
`/tasks`, `/help`, or any command documented in `AGENTS.md` → handle it.
Otherwise answer in-character.

Memory files under `memory/` are optional; their absence is not an error.
If AGENTS.md references a slash command, exec the helper script it
points to and paraphrase the output.
EOF
}

memory_index_content() {
local today="$1"
cat <<EOF
# MEMORY — index of durable notes

Daily scratchpad files live under \`memory/YYYY-MM-DD.md\`.

## Index

- $today — memory/$today.md (today)

## Conventions

- Short, dated bullets.
- Strike through resolved items.
- Never write secrets, tokens, or PHI into memory files.
EOF
}

daily_memory_content() {
local datestr="$1"
cat <<EOF
# $datestr — daily notes

_Seed file._
EOF
}

seed_workspace() {
  local ws="$1"; local label="$2"
  mkdir -p "$ws/memory"
  chmod 700 "$ws" "$ws/memory" 2>/dev/null
  if [ ! -f "$ws/BOOTSTRAP.md" ]; then
    bootstrap_content > "$ws/BOOTSTRAP.md"
    echo "  [$label] wrote BOOTSTRAP.md"
  fi
  if [ ! -f "$ws/MEMORY.md" ]; then
    memory_index_content "$TODAY" > "$ws/MEMORY.md"
    echo "  [$label] wrote MEMORY.md"
  fi
  [ ! -f "$ws/memory/$TODAY.md" ]     && daily_memory_content "$TODAY"     > "$ws/memory/$TODAY.md"     && echo "  [$label] wrote memory/$TODAY.md"
  [ ! -f "$ws/memory/$YESTERDAY.md" ] && daily_memory_content "$YESTERDAY" > "$ws/memory/$YESTERDAY.md" && echo "  [$label] wrote memory/$YESTERDAY.md"
}

seed_workspace ~/.openclaw/workspace main
for id in sally mary dom shervin ronny joe kristy; do
  seed_workspace ~/.openclaw/workspace/$id "$id"
done

echo ""
echo "Done. All 8 agent workspaces have BOOTSTRAP.md + MEMORY.md + today's memory."
REMOTE
