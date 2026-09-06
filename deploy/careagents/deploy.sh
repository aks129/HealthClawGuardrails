#!/usr/bin/env bash
# RETIRED — this is not how CareAgents is deployed. It refuses to run.
#
# It deployed CareAgents to the careagents.cloud VPS (187.77.4.50). That host
# is no longer in front of any user: careagents.cloud is served by Railway,
# careagents.cloud/healthz and careagents-production.up.railway.app/healthz
# report the same build, and scripts/prod_watch.py watches only the Railway
# name (#289). #264 (2026-08-02) called for exactly this — finish the DNS
# cutover, keep one origin, retire this path — because two origins over one
# account store break passkeys, which are bound to the origin they were
# enrolled against. The cutover happened; the issue is still open.
#
# The live path is a staged `railway up` for the web and worker services:
# docs/runbooks/careagents-durable-worker.md (## Railway).
#
# Kept rather than deleted so the retired path can still be read, and refusing
# rather than warning because what it would do is the failure it was last
# changed to catch: ship to a second origin that no monitor checks. Measured
# 2026-09-04, that host is still serving CareAgents on its old vhost
# (`curl --resolve careagents.cloud:443:187.77.4.50 https://careagents.cloud/healthz`)
# and answers without `build`, `built_at` or `run_workers` — code from before
# #257 and #258. Shutting it down is an owner action, not this script's.
#
# Everything below the refusal is the deploy as it last ran, on 2026-08-02.
set -euo pipefail

cat >&2 <<'RETIRED'
deploy.sh is retired and did nothing.

careagents.cloud is served by Railway. This script deploys to the VPS at
187.77.4.50, which no user reaches and no monitor checks, against the same
account store — a passkey enrolled there works on neither origin (#264).

Deploy CareAgents with the staged `railway up` for BOTH services, per
docs/runbooks/careagents-durable-worker.md (## Railway).
RETIRED
exit 1

HOST="${1:-root@187.77.4.50}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "→ ensure target dirs exist"
ssh "$HOST" 'id -u careagents &>/dev/null || useradd --system --home /opt/careagents careagents; mkdir -p /opt/careagents/app /etc/careagents'

echo "→ stamp build marker"
# So the running process can say which commit it is (#258): both deployments
# were once months behind main while every production check was green. The
# format lives in stamp_build.sh alone — the Railway path calls the same script
# against its staging dir, so the two cannot disagree about what a marker means.
"$REPO_ROOT/deploy/careagents/stamp_build.sh" "$REPO_ROOT"

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

# --- durable worker pool ---
CARE_RUN_WORKERS=4
CARE_RUN_DEADLINE_SECONDS=120
CARE_RUN_LEASE_SECONDS=60
CARE_RUN_WORKER_STALE_SECONDS=30
ENV
  chmod 600 /etc/careagents/careagents.env
  echo "!! populate /etc/careagents/careagents.env before the service will boot"
fi
chown -R careagents:careagents /opt/careagents
chmod 700 /opt/careagents/data
REMOTE

echo "→ install unit + nginx"
scp -q "$REPO_ROOT/deploy/careagents/careagents.service" "$HOST:/etc/systemd/system/careagents.service"
scp -q "$REPO_ROOT/deploy/careagents/careagents-worker.service" "$HOST:/etc/systemd/system/careagents-worker.service"
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
systemctl enable --now careagents careagents-worker
systemctl restart careagents careagents-worker nginx
sleep 2
systemctl is-active careagents
systemctl is-active careagents-worker
for _attempt in {1..30}; do
  if curl -sf http://127.0.0.1:8600/healthz >/dev/null; then
    break
  fi
  sleep 1
done
curl -sf http://127.0.0.1:8600/healthz && echo
REMOTE

echo "✓ deployed — verify: curl -s https://careagents.cloud/healthz"
