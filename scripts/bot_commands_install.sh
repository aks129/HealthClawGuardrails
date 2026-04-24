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

echo ">> scp commands.py"
scp -q "$SCRIPT_DIR/bot_commands.py" "$REMOTE:~/.healthclaw/commands.py"
ssh "$REMOTE" 'chmod 700 ~/.healthclaw/commands.py'

echo ">> Ensure itsdangerous installed (user site)"
ssh "$REMOTE" '/usr/bin/python3 -m pip install --user --quiet itsdangerous 2>/dev/null || true'

echo ">> Smoke-test /help + /dashboard on the Mac mini"
ssh "$REMOTE" '/usr/bin/python3 ~/.healthclaw/commands.py help'
echo
ssh "$REMOTE" '/usr/bin/python3 ~/.healthclaw/commands.py dashboard --agent bot | head -3'

echo
echo "✓ commands.py deployed. Next: update AGENTS.md per persona."
