#!/usr/bin/env bash
# hermes/install.sh
#
# One-shot Hermes wiring for the HealthClaw stack.
#
#   1. Verifies Hermes is installed and the ~/.hermes dir exists.
#   2. Copies all HealthClaw skills into ~/.hermes/skills/healthclaw/
#      so Hermes can read, fork, and improve them over time.
#   3. Installs the HealthClaw SOUL persona at ~/.hermes/personas/healthclaw.md.
#   4. Merges hermes/mcp.json into ~/.hermes/config.json under mcp_servers
#      (existing entries preserved; HealthClaw entries overwritten so a re-run
#      is always idempotent).
#
# Usage:
#   ./hermes/install.sh                     # install everything
#   ./hermes/install.sh --skills-only       # just refresh skills
#   ./hermes/install.sh --dry-run           # print actions, do nothing
#
# After install, in any Hermes session:
#   /persona healthclaw
#   /skill list                # confirms healthclaw skills are loaded
#   "show me my conditions"    # SOUL takes over

set -euo pipefail

DRY_RUN=0
SKILLS_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skills-only) SKILLS_ONLY=1 ;;
    -h|--help)
      sed -n '2,21p' "$0"
      exit 0 ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 64 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    eval "$@"
  fi
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

echo "→ HealthClaw → Hermes installer"
echo "  repo:    $REPO_ROOT"
echo "  hermes:  $HERMES_HOME"

if ! command -v hermes >/dev/null 2>&1; then
  echo "✘ 'hermes' CLI not found on PATH."
  echo "  Install it first: see https://github.com/nousresearch/hermes-agent#installation"
  echo "  (this script keeps the install idempotent — re-run after you finish.)"
  exit 1
fi

if [ ! -d "$HERMES_HOME" ]; then
  echo "  creating $HERMES_HOME"
  run "mkdir -p '$HERMES_HOME/skills' '$HERMES_HOME/personas'"
fi

# ─── Step 1: skills ────────────────────────────────────────────────────────
DEST_SKILLS="$HERMES_HOME/skills/healthclaw"
echo "→ copying skills/ → $DEST_SKILLS"
run "mkdir -p '$DEST_SKILLS'"
run "rsync -a --delete '$REPO_ROOT/skills/' '$DEST_SKILLS/'"

# ─── Step 2: persona ───────────────────────────────────────────────────────
if [ "$SKILLS_ONLY" = "0" ]; then
  DEST_PERSONA="$HERMES_HOME/personas/healthclaw.md"
  echo "→ installing persona → $DEST_PERSONA"
  run "cp '$REPO_ROOT/hermes/SOUL.md' '$DEST_PERSONA'"

  # ─── Step 3: MCP server config ───────────────────────────────────────────
  CONFIG="$HERMES_HOME/config.json"
  echo "→ wiring MCP server → $CONFIG"
  if [ ! -f "$CONFIG" ]; then
    run "echo '{}' > '$CONFIG'"
  fi

  if command -v jq >/dev/null 2>&1; then
    TMP="$(mktemp)"
    if [ "$DRY_RUN" = "1" ]; then
      echo "  [dry-run] jq merge hermes/mcp.json into $CONFIG (HealthClaw keys overwritten)"
    else
      jq --slurpfile add "$REPO_ROOT/hermes/mcp.json" '
        .mcp_servers = ((.mcp_servers // {}) + ($add[0].mcp_servers // {}))
      ' "$CONFIG" > "$TMP" && mv "$TMP" "$CONFIG"
    fi
  else
    echo "  ⚠ jq not installed — skipping automatic merge."
    echo "    Manually copy the mcp_servers block from $REPO_ROOT/hermes/mcp.json"
    echo "    into $CONFIG under the top-level mcp_servers key."
  fi
fi

echo
echo "✓ Done."
echo
echo "Try it:"
echo "  hermes                                    # start a session"
echo "  /persona healthclaw                       # load the HealthClaw SOUL"
echo "  /mcp list                                 # confirm healthclaw-hosted is connected"
echo "  show me my conditions                     # SOUL takes over"
echo
echo "Skills can be edited / forked at:"
echo "  $DEST_SKILLS"
echo "Hermes will keep them as your skills; re-running this script with"
echo "--skills-only refreshes them from the repo without touching your edits"
echo "elsewhere."
