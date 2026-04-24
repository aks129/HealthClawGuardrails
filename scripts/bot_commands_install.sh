#!/usr/bin/env bash
# scripts/bot_commands_install.sh
#
# Deploys scripts/bot_commands.py to the Mac mini at
#   ~/.healthclaw/commands.py
# and updates each active agent's AGENTS.md with a HealthClaw slash-command
# section so the LLM knows what /dashboard, /health, etc. mean and how to
# dispatch them.
#
# Run from the laptop:
#   bash scripts/bot_commands_install.sh
# or override the remote:
#   SSH_USER=coopeydoop SSH_HOST=192.168.5.121 bash scripts/bot_commands_install.sh

set -euo pipefail

SSH_USER="${SSH_USER:-coopeydoop}"
SSH_HOST="${SSH_HOST:-192.168.5.121}"
REMOTE="${SSH_USER}@${SSH_HOST}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" true \
  || { echo "ERROR: cannot SSH to $REMOTE" >&2; exit 1; }

echo ">> Creating ~/.healthclaw on $REMOTE"
ssh "$REMOTE" 'mkdir -p ~/.healthclaw && chmod 700 ~/.healthclaw'

echo ">> Creating isolated venv at ~/.healthclaw/venv (if missing)"
ssh "$REMOTE" '
  if [ ! -x ~/.healthclaw/venv/bin/python3 ]; then
    /usr/bin/python3 -m venv ~/.healthclaw/venv
    ~/.healthclaw/venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  fi
  ~/.healthclaw/venv/bin/pip install --quiet requests icalendar itsdangerous >/dev/null 2>&1 || true
  ~/.healthclaw/venv/bin/python3 -c "import requests, icalendar, itsdangerous; print(\"venv deps:\", requests.__version__, icalendar.__version__)"
'

echo ">> scp commands.py (shebang points at the venv)"
scp -q "$SCRIPT_DIR/bot_commands.py" "$REMOTE:~/.healthclaw/commands.py"
ssh "$REMOTE" 'chmod 700 ~/.healthclaw/commands.py'

echo ">> Smoke-test /help + /dashboard on the Mac mini"
ssh "$REMOTE" '/usr/bin/python3 ~/.healthclaw/commands.py help'
echo
ssh "$REMOTE" '/usr/bin/python3 ~/.healthclaw/commands.py dashboard --agent bot | head -3'

echo
echo "✓ commands.py deployed. Next: update AGENTS.md per persona."
