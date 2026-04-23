# Mac mini — "Always-on healthcare agent" setup

Your Mac mini is the ideal host for the personal-health-agent stack:
- **OpenClaw Gateway** — multi-channel AI assistant (Telegram, WhatsApp, iMessage, Slack…)
- **Open Wearables sidecar** — pulls Fitbit, Oura, Whoop, Garmin, Apple Health data
- **HealthClaw local instance** (optional) — if you want a fully private stack alongside Railway

This guide makes the Mac mini:
1. Reachable over SSH and Remote Desktop from your laptop on the same WiFi
2. Always-on (no sleep, auto-wake after power loss)
3. Running OpenClaw + Open Wearables as LaunchAgents (auto-start on boot)

Everything below runs **on the Mac mini**. I'll mark commands that need admin rights with `sudo`.

---

## 1. Enable SSH (Remote Login)

From the Mac mini's Terminal:

```bash
# Turn on SSH daemon
sudo systemsetup -setremotelogin on

# Confirm
sudo systemsetup -getremotelogin
# → Remote Login: On

# Check your local IP (the address your laptop will SSH to)
ipconfig getifaddr en0   # Ethernet
ipconfig getifaddr en1   # WiFi (try en0 first, then en1)
```

From your laptop:

```bash
ssh <your-mac-username>@<mac-ip>
# First connection: accept the fingerprint
```

**Recommended**: add your laptop's public key so you don't type a password every time:

```bash
# On laptop
ssh-copy-id <your-mac-username>@<mac-ip>

# Or manually: paste your laptop's ~/.ssh/id_ed25519.pub (or .id_rsa.pub)
# into the Mac's ~/.ssh/authorized_keys
```

---

## 2. Enable Screen Sharing (macOS Remote Desktop equivalent)

```bash
# Enable Screen Sharing (built-in, works with macOS ↔ macOS via VNC-compatible protocol)
sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.screensharing.plist

# Allow Apple Remote Desktop + VNC clients
sudo /System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart \
  -activate -configure \
  -access -on \
  -users <your-mac-username> \
  -privs -all -restart -agent
```

From your laptop's Finder: **Go → Connect to Server** → `vnc://<mac-ip>` → log in.

Alternatively, **System Settings → Sharing → Screen Sharing** via the GUI if you're sitting at the Mac mini.

---

## 3. Always-on power settings

Keep the machine awake and auto-recover after power loss:

```bash
sudo pmset -a sleep 0                 # never sleep
sudo pmset -a disksleep 0             # spinning disks don't spin down
sudo pmset -a displaysleep 10         # display sleeps after 10 min (save energy)
sudo pmset -a womp 1                  # wake on magic packet (network)
sudo pmset -a autorestart 1           # auto-restart after power failure
sudo pmset -a ttyskeepawake 1         # stay awake while SSH sessions exist
sudo pmset -a powernap 0              # disable Power Nap (can interfere with daemons)

# Verify
pmset -g
```

To prevent sleep only while OpenClaw is running without changing system policy, you can wrap the service in `caffeinate`:

```bash
caffeinate -i -s ./start-openclaw.sh
```

---

## 4. Install OpenClaw

```bash
# Install Node 22+ if not present (OpenClaw needs Node 22+)
brew install node@22
echo 'export PATH="/opt/homebrew/opt/node@22/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
node --version   # confirm v22.x

# Install OpenClaw CLI (check docs.openclaw.ai for the official install command —
# it's typically one of):
npx -y @openclaw/cli init      # if distributed as an npm package
# or
brew install openclaw/tap/openclaw  # if distributed via a tap

# Authenticate and start the Gateway
openclaw auth login
openclaw gateway start            # runs on http://localhost:4319 by default

# Confirm the gateway is live
curl http://localhost:4319/healthz
```

Install healthclaw-specific skills (from this repo's `skills/` folder) into OpenClaw's workspace:

```bash
cd ~/.openclaw/workspace/skills
git clone https://github.com/aks129/HealthClawGuardrails hcg
ln -s hcg/skills/curatr curatr
ln -s hcg/skills/personal-health-records personal-health-records
ln -s hcg/skills/phi-redaction phi-redaction
ln -s hcg/skills/fhir-upstream-proxy fhir-upstream-proxy
ln -s hcg/skills/fasten-connect fasten-connect
ln -s hcg/skills/healthex-export healthex-export
```

---

## 5. Install Open Wearables sidecar

Open Wearables is the adapter that pulls from Fitbit/Oura/Whoop/Garmin/Apple Health on your behalf.

```bash
# Clone
git clone https://github.com/cloudwerxlab/open-wearables ~/open-wearables
cd ~/open-wearables

# Install
npm install

# Configure — edit .env
cp .env.example .env
# Fill in provider OAuth client IDs/secrets you want to support

# Start
npm start
# Default: http://localhost:3030
```

For **Apple Health**, install the companion "Health Auto Export" iOS app and point it at:
`http://<mac-ip>:3030/providers/apple-health/ingest`

---

## 6. Wire HealthClaw (Railway) → OpenClaw (Mac mini) + Wearables

You want the Railway-hosted Flask service to probe your Mac mini's OpenClaw Gateway and show live sessions in the command center.

**Problem**: Railway runs in a US-East data center. Your Mac mini is behind your home NAT. Direct access won't work unless you punch a hole.

**Recommended**: Tailscale (free) — creates a secure mesh VPN. Railway services join the mesh as clients.

```bash
# On Mac mini
brew install tailscale
sudo tailscale up

# Get Mac mini's Tailscale hostname
tailscale status
# e.g. mac-mini.tail-scale-net.ts.net
```

On Railway (from your laptop):

```bash
# Add Tailscale to the HealthClawGuardrails service. Railway docs:
# https://docs.railway.com/guides/tailscale
railway service link HealthClawGuardrails
railway variables --set TAILSCALE_AUTHKEY=<get-one-time-key-from-tailscale-admin>

# Set the gateway URL to the Tailscale hostname
railway variables --set OPENCLAW_GATEWAY_URL=http://mac-mini.tail-scale-net.ts.net:4319/healthz
railway variables --set OPEN_WEARABLES_URL=http://mac-mini.tail-scale-net.ts.net:3030
```

**Alternative (no Tailscale)**: Cloudflare Tunnel. Similar idea, uses CF's edge.

---

## 7. Auto-start services as LaunchAgents

So OpenClaw + Open Wearables auto-restart after a Mac reboot.

Create `~/Library/LaunchAgents/com.openclaw.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.openclaw.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/caffeinate</string>
        <string>-i</string><string>-s</string>
        <string>/opt/homebrew/bin/openclaw</string>
        <string>gateway</string><string>start</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/openclaw.log</string>
    <key>StandardErrorPath</key><string>/tmp/openclaw.err</string>
</dict>
</plist>
```

Same pattern for Open Wearables at `~/Library/LaunchAgents/com.openwearables.plist`.

Load them:

```bash
launchctl load ~/Library/LaunchAgents/com.openclaw.gateway.plist
launchctl load ~/Library/LaunchAgents/com.openwearables.plist

# Verify
launchctl list | grep -E "openclaw|openwearables"
```

They'll auto-start on every boot from now on.

---

## 8. Verify the dashboard sees your Mac mini

From any machine, visit https://app.healthclaw.io/command-center. The **System Status** row should show:

```
● OpenClaw Gateway — http://mac-mini.tail-scale-net.ts.net:4319/healthz · v<version>
```

And the **OpenClaw Live Sessions** panel should populate with active chat sessions.

---

## Appendix A — Useful always-on commands

```bash
# Quick uptime + session check
uptime
who

# Which of my LaunchAgents are running?
launchctl list | grep -v com.apple

# Tail the OpenClaw log
tail -f /tmp/openclaw.log

# Restart the services without reboot
launchctl unload ~/Library/LaunchAgents/com.openclaw.gateway.plist
launchctl load   ~/Library/LaunchAgents/com.openclaw.gateway.plist
```

## Appendix B — Importing your existing health data

Once OpenClaw is up on the Mac mini with the `healthex-export` skill loaded,
ask your Telegram bot (or OpenClaw directly):

```
"Pull my complete HealthEx history and import into my HealthClaw tenant"
```

This runs the HealthEx MCP pull, de-identifies, pre-tags curatr quality
issues, and POSTs the bundle to your HealthClaw instance.

Or run manually:

```bash
# From your laptop, SSH'd into the Mac mini
HEALTHEX_AUTH_TOKEN=<from-Claude.ai> STEP_UP_SECRET=<from-Railway> \
python scripts/export_healthex_mcp.py \
  --tenant-id ev-personal \
  --output exports/healthex-$(date +%Y-%m-%d).json \
  --import
```
