#!/usr/bin/env bash
# scripts/kristy_install.sh
#
# One-shot installer for Kristy's schedule watcher on a Mac mini.
#
# Deploys:
#   ~/.kristy/watcher.py                     — the watcher script
#   ~/.kristy/env                            — secrets (0600 perms)
#   ~/Library/LaunchAgents/io.healthclaw.kristy.plist
#                                            — runs the watcher daily at 06:00
#
# Run from the laptop (over SSH):
#   SSH_USER=coopeydoop SSH_HOST=192.168.5.121 \
#   STEP_UP_SECRET=<railway-secret> \
#   FAMILY_ICAL_URLS='Henry Football|https://...ics,Max Football|https://...ics' \
#     bash scripts/kristy_install.sh
#
# Or pass args on the command line:
#   bash scripts/kristy_install.sh --host 192.168.5.121 --user coopeydoop
#
# The STEP_UP_SECRET is pulled from Railway if available via `railway variables`.

set -euo pipefail

SSH_USER="${SSH_USER:-coopeydoop}"
SSH_HOST="${SSH_HOST:-192.168.5.121}"
TENANT="${TENANT:-ev-personal}"
COMMAND_CENTER_API="${COMMAND_CENTER_API:-https://app.healthclaw.io/command-center/api}"
HORIZON_DAYS="${FAMILY_HORIZON_DAYS:-28}"
ALLOWED_DAYS="${FAMILY_ALLOWED_DAYS:-sun,mon,tue,wed,thu,sat}"
SCHEDULE_HOUR="${KRISTY_SCHEDULE_HOUR:-6}"   # run daily at 06:00 local

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) SSH_HOST="$2"; shift 2;;
    --user) SSH_USER="$2"; shift 2;;
    --tenant) TENANT="$2"; shift 2;;
    --api) COMMAND_CENTER_API="$2"; shift 2;;
    --hour) SCHEDULE_HOUR="$2"; shift 2;;
    -h|--help)
      sed -n '2,25p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

REMOTE="${SSH_USER}@${SSH_HOST}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Resolve STEP_UP_SECRET ──────────────────────────────────────────────
if [[ -z "${STEP_UP_SECRET:-}" ]]; then
  echo ">> Pulling STEP_UP_SECRET from Railway (service=HealthClawGuardrails)"
  STEP_UP_SECRET="$(railway variables -s HealthClawGuardrails --kv 2>/dev/null \
                    | awk -F= '/^STEP_UP_SECRET=/ {sub(/^STEP_UP_SECRET=/, ""); print; exit}')"
  if [[ -z "${STEP_UP_SECRET}" ]]; then
    echo "ERROR: STEP_UP_SECRET not set and not found via 'railway variables'." >&2
    echo "       Pass it via env or run 'railway link HealthClawGuardrails' first." >&2
    exit 1
  fi
fi

if [[ -z "${FAMILY_ICAL_URLS:-}" ]]; then
  echo "ERROR: FAMILY_ICAL_URLS env var is required."
  echo "       Format: 'Label|https://...,Label|https://...'"
  exit 1
fi

# ── Pre-flight SSH check ────────────────────────────────────────────────
ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" true \
  || { echo "ERROR: cannot SSH to $REMOTE" >&2; exit 1; }

# ── Deploy watcher + env + plist ────────────────────────────────────────
echo ">> Creating ~/.kristy on $REMOTE"
ssh "$REMOTE" 'mkdir -p ~/.kristy && chmod 700 ~/.kristy'

echo ">> scp watcher.py"
scp -q "$SCRIPT_DIR/kristy_schedule_watcher.py" "$REMOTE:~/.kristy/watcher.py"
ssh "$REMOTE" 'chmod 700 ~/.kristy/watcher.py'

echo ">> Writing ~/.kristy/env (0600)"
# Here-doc assembled locally, then piped so secrets never hit argv.
{
  echo "# Kristy watcher environment — DO NOT COMMIT"
  echo "STEP_UP_SECRET=${STEP_UP_SECRET}"
  echo "COMMAND_CENTER_API=${COMMAND_CENTER_API}"
  echo "DEFAULT_TENANT=${TENANT}"
  echo "KRISTY_AGENT_ID=kristy"
  echo "FAMILY_HORIZON_DAYS=${HORIZON_DAYS}"
  echo "FAMILY_ALLOWED_DAYS=${ALLOWED_DAYS}"
  # FAMILY_ICAL_URLS can contain commas + pipes; carry over verbatim.
  echo "FAMILY_ICAL_URLS=${FAMILY_ICAL_URLS}"
} | ssh "$REMOTE" 'umask 077; cat > ~/.kristy/env && chmod 600 ~/.kristy/env'

echo ">> Installing icalendar + requests to user-site (no root)"
ssh "$REMOTE" '/usr/bin/python3 -m pip install --user --quiet icalendar requests 2>/dev/null || true'

# ── LaunchAgent plist — runs every morning at $SCHEDULE_HOUR:00 ─────────
LABEL="io.healthclaw.kristy"
PLIST_PATH="~/Library/LaunchAgents/${LABEL}.plist"

PLIST_CONTENT=$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/${SSH_USER}/.kristy/watcher.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>${SCHEDULE_HOUR}</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>/Users/${SSH_USER}/.kristy/watcher.log</string>
  <key>StandardErrorPath</key><string>/Users/${SSH_USER}/.kristy/watcher.err.log</string>
  <key>WorkingDirectory</key><string>/Users/${SSH_USER}/.kristy</string>
</dict>
</plist>
PLIST
)

echo ">> Writing LaunchAgent plist: ${PLIST_PATH}"
echo "$PLIST_CONTENT" | ssh "$REMOTE" "mkdir -p ~/Library/LaunchAgents && cat > ${PLIST_PATH}"

echo ">> Reloading LaunchAgent"
ssh "$REMOTE" "launchctl bootout gui/\$(id -u) ${PLIST_PATH} 2>/dev/null || true; \
               launchctl bootstrap gui/\$(id -u) ${PLIST_PATH}"

echo ">> Running the watcher once now for a smoke test"
ssh "$REMOTE" 'cd ~/.kristy && /usr/bin/python3 ./watcher.py 2>&1 | tail -20'

echo
echo "✓ Kristy watcher installed. Next run at ${SCHEDULE_HOUR}:00 local time daily."
echo "  Logs:  ~/.kristy/watcher.log  &  ~/.kristy/watcher.err.log"
echo "  On-demand: ssh ${REMOTE} 'python3 ~/.kristy/watcher.py'"
echo "  Uninstall: ssh ${REMOTE} 'launchctl bootout gui/\$(id -u) ${PLIST_PATH}; rm -rf ~/.kristy ${PLIST_PATH}'"
