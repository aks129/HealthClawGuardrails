#!/usr/bin/env bash
# scripts/update_agent_prompts.sh
#
# Idempotently appends (or replaces) the "## HealthClaw slash commands"
# section in each persona's ~/.openclaw/workspace/<id>/AGENTS.md so the
# LLM knows which slash commands to handle and how to exec commands.py.
#
# Per-persona command allowlist:
#   sally    dashboard health tasks help
#   mary     dashboard health tasks help
#   dom      dashboard health tasks help
#   shervin  dashboard health tasks help
#   ronny    dashboard health tasks help
#   joe      dashboard health tasks conflicts help
#   kristy   dashboard health tasks week conflicts help
#
# main (the router) also gets a version that adds "/route-to-<persona>" hand-off.

set -euo pipefail
SSH_USER="${SSH_USER:-coopeydoop}"
SSH_HOST="${SSH_HOST:-192.168.5.121}"
REMOTE="${SSH_USER}@${SSH_HOST}"

# Build the section body once; customize per agent via env subst.
build_section() {
  local agent="$1"
  local extras="$2"    # additional command docs
  cat <<EOF

## HealthClaw slash commands

You run on the OpenClaw gateway with exec permission for the HealthClaw helper
script at \`/Users/${SSH_USER}/.healthclaw/commands.py\`. When the user sends
one of the slash commands below, exec the helper, read its stdout, and
paraphrase the result in-character.

**Core commands every agent handles:**

- \`/dashboard\` — mint a fresh 24-hour signed URL to the command center.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py dashboard --agent ${agent}\`
  Take the first stdout line (the URL) and reply with: "Here's your fresh
  dashboard link (valid 24h): <URL>. Keep it private — it auto-logs you in."

- \`/health\` — probe the HealthClaw stack.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py health --agent ${agent}\`
  Paraphrase the stdout lines into a readable status summary.

- \`/tasks\` — list pending tasks for the active tenant.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py tasks --agent ${agent}\`

- \`/help\` — print my capabilities (this list plus my persona's specialty).

${extras}

### Hand-off rules

- If the user asks something outside your specialty (see the personas table
  in ROUTER.md or your own description), redirect them to the appropriate
  specialist by replying with their @handle. Example: "That's a Mary job —
  DM @mary_coopdoop_bot and she'll handle refills."

### Safety

- Never reveal the STEP_UP_SECRET, tokens, or raw helper output that includes
  secrets. The helper is designed to be paraphrased, not echoed.
- All writes to HealthClaw resources (meds, tasks, appointments) require
  explicit human confirmation. For a multi-step action, describe what you'd
  do first and wait for the user to say "yes" / "go" / "approve".
EOF
}

# Per-persona extras
sally_extras=""
mary_extras=""
dom_extras=""
shervin_extras=""
ronny_extras=""
joe_extras="
- \`/conflicts\` — family-schedule conflicts pending.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py conflicts --agent joe\`
"
kristy_extras="
**Kristy-specific commands:**

- \`/week\` — run the family schedule scan end-to-end (fetches iCals, detects
  conflicts, emits new AgentTasks). Takes ~5 seconds.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py week --agent kristy\`
  Pastes the run summary; if conflicts were created, walk the user through each.

- \`/conflicts\` — list family-conflict tasks currently pending.
  Exec: \`python3 /Users/${SSH_USER}/.healthclaw/commands.py conflicts --agent kristy\`
"

# Router (main) extras
router_extras='
**Router-specific hand-off table:**

When a user DMs me (@coopdoop_bot), I am the front door. Direct them to the
right specialist by persona. See ROUTER.md for the full table of:
- Sally 🩺 (@sally_coopdoop_bot)
- Mary 💊 (@mary_coopdoop_bot)
- Dom 🏃 (@dom_coopdoop_bot)
- Kristy 🗓️ (@Kristy_healthclaw_bot)
- Shervin 🧠 / Ronny 👨‍👩‍👧 / Joe ⚙️ (no bot yet — use me for meta questions)

I handle /dashboard /health /tasks /help myself; everything else, route.
'

# Write each section to a temp file locally, then scp + merge on remote.
merge_on_remote() {
  local id="$1"
  local content="$2"
  local ws_subpath="workspace/${id}"
  [ "$id" = "main" ] && ws_subpath="workspace"

  # Write locally
  local local_tmp="/tmp/_agents_${id}.md"
  printf '%s' "$content" > "$local_tmp"

  # scp, then Python-merge on the remote (keeps existing content above, replaces
  # any prior "## HealthClaw slash commands" section).
  scp -q "$local_tmp" "${REMOTE}:/tmp/_agents_${id}.md"
  ssh "$REMOTE" "/usr/bin/python3 - <<PY
from pathlib import Path
import re

ws = Path.home() / '.openclaw' / '${ws_subpath}'
ws.mkdir(parents=True, exist_ok=True)
target = ws / 'AGENTS.md'
new_section = Path('/tmp/_agents_${id}.md').read_text()

existing = target.read_text() if target.exists() else ''
# Strip any prior '## HealthClaw slash commands' section
pattern = re.compile(r'\n## HealthClaw slash commands.*?(?=\n## |\Z)', re.DOTALL)
stripped = pattern.sub('', existing).rstrip() + '\n'
merged = stripped + new_section.strip() + '\n'
target.write_text(merged)
print(f'  wrote {target} ({len(merged)} bytes)')
PY
  /bin/rm /tmp/_agents_${id}.md"
  /bin/rm -f "$local_tmp"
}

echo ">> Updating AGENTS.md for each persona"
for persona in sally mary dom shervin ronny joe kristy main; do
  case "$persona" in
    sally)   section="$(build_section sally   "$sally_extras")";;
    mary)    section="$(build_section mary    "$mary_extras")";;
    dom)     section="$(build_section dom     "$dom_extras")";;
    shervin) section="$(build_section shervin "$shervin_extras")";;
    ronny)   section="$(build_section ronny   "$ronny_extras")";;
    joe)     section="$(build_section joe     "$joe_extras")";;
    kristy)  section="$(build_section kristy  "$kristy_extras")";;
    main)    section="$(build_section main    "$router_extras")";;
  esac
  echo "  [${persona}]"
  merge_on_remote "$persona" "$section"
done

echo
echo "✓ All persona AGENTS.md updated."
echo "  (OpenClaw picks up changes on next conversation turn — no restart needed.)"
