#!/usr/bin/env bash
# Deploy CareAgents to the careagents.cloud VPS.
#
#   ./deploy/careagents/deploy.sh [user@host]      # default root@187.77.4.50
#
# Idempotent: rsyncs the careagents package, (re)builds the venv, installs the
# systemd unit, swaps nginx's `location /` from the static stub to the app
# (leaving /gateway/, /telegram/, /hermes/, /health untouched), and restarts.
# Secrets are NOT shipped by this script — /etc/careagents/careagents.env is
# created once on the host (template printed if missing).
set -euo pipefail

HOST="${1:-root@187.77.4.50}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "→ ensure target dirs exist"
ssh "$HOST" 'id -u careagents &>/dev/null || useradd --system --home /opt/careagents careagents; mkdir -p /opt/careagents/app /etc/careagents'

echo "→ rsync app to $HOST"
rsync -az --delete \
  "$REPO_ROOT/careagents" \
  "$HOST:/opt/careagents/app/"

echo "→ remote install"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
id -u careagents &>/dev/null || useradd --system --home /opt/careagents careagents
mkdir -p /opt/careagents/app /etc/careagents

# venv (python3.12 on the VPS)
if [ ! -x /opt/careagents/venv/bin/python ]; then
  python3 -m venv /opt/careagents/venv
fi
/opt/careagents/venv/bin/pip install --quiet --upgrade \
  flask gunicorn requests itsdangerous anthropic webauthn sqlalchemy

# accounts DB lives on a persisted, 0700 dir owned by the service user
mkdir -p /opt/careagents/data

# env file: create a template on first run; never overwrite
if [ ! -f /etc/careagents/careagents.env ]; then
  cat > /etc/careagents/careagents.env <<'ENV'
CARE_ENV=production
HEALTHCLAW_BASE=https://app.healthclaw.io
CARE_SESSION_SECRET=__SET_ME_32_CHARS_MIN__
HEALTHCLAW_MINT_SECRET=__SET_ME__

# --- accounts (identity: email codes + passkeys) ---
# WebAuthn is bound to the public origin; these MUST match the browser URL.
CARE_RP_ID=careagents.cloud
CARE_RP_NAME=CareAgents
CARE_ORIGIN=https://careagents.cloud
# Account store — SQLite on the persisted data dir (survives redeploys).
CARE_DATABASE_URL=sqlite:////opt/careagents/data/careagents.db
# Transactional email for login codes (Resend). Without a key, prod refuses
# to boot; in dev the code is logged to stderr instead.
RESEND_API_KEY=__SET_ME__
CARE_EMAIL_FROM=CareAgents <login@careagents.cloud>

# --- verified-provider records (Fasten Connect) ---
FASTEN_PUBLIC_KEY=
# --- Telegram surface (bot username, no @) ---
CARE_TELEGRAM_BOT=
# --- iMessage surface (the handle the Mac-mini relay sends/receives on;
#     empty = tile hidden). The relay runs deploy/careagents/imessage_relay.py
#     on the Mac mini with CAREAGENTS_MINT_SECRET=<this mint secret>. ---
CARE_IMESSAGE_HANDLE=

# Provider: ANTHROPIC_API_KEY (claude-sonnet-5) takes precedence when set.
# Otherwise the OpenAI-compatible fallback is used — works with OpenAI or,
# as shipped today, Google Gemini's compat endpoint:
#   OPENAI_API_KEY=<gemini key>
#   OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
#   CARE_OPENAI_MODEL=gemini-3.5-flash
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OPENAI_BASE_URL=
CARE_OPENAI_MODEL=
CARE_MODEL=claude-sonnet-5
ENV
  chmod 600 /etc/careagents/careagents.env
  echo "!! populate /etc/careagents/careagents.env before the service will boot"
fi
chown -R careagents:careagents /opt/careagents
chmod 700 /opt/careagents/data
REMOTE

echo "→ install unit + nginx"
scp -q "$REPO_ROOT/deploy/careagents/careagents.service" "$HOST:/etc/systemd/system/careagents.service"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
# Point nginx's `location /` at the app (both :80 and :443 servers), once.
CFG=/etc/nginx/sites-enabled/careagents.cloud
# The existing `location /health` is a PREFIX match that would shadow app
# routes like /healthz — pin it to an exact match. Idempotent.
sed -i 's|location /health {|location = /health {|' "$CFG"

if ! grep -q "proxy_pass http://127.0.0.1:8600" "$CFG"; then
  cp "$CFG" "$CFG.bak-$(date +%s)"
  python3 - "$CFG" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path).read()
block = """    location / {
        proxy_pass http://127.0.0.1:8600;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 180s;
    }"""
src, n = re.subn(r"    location / \{[^}]*\}", block, src)
open(path, "w").write(src)
print(f"nginx: replaced {n} location / block(s)")
PY
fi
nginx -t
systemctl daemon-reload
systemctl enable --now careagents
systemctl restart careagents nginx
sleep 2
systemctl is-active careagents
curl -sf http://127.0.0.1:8600/healthz && echo
REMOTE

echo "✓ deployed — verify: curl -s https://careagents.cloud/healthz"
