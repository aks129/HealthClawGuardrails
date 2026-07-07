# Quickstart: Claude (web, desktop, iPhone/Android)

Time: about 3 minutes. Requires a Claude Pro or Max plan (custom connectors
are not available on the free tier).

## 1. Add the connector (do this once, on the web)

1. Go to [claude.ai](https://claude.ai) in a browser and sign in.
2. Click your initials (bottom-left) → **Settings** → **Connectors**.
3. Click **Add custom connector**.
4. Fill in:
   - **Name:** `HealthClaw`
   - **URL:** `https://mcp-server-production-5112.up.railway.app/mcp`
5. Click **Add**. No login screen appears — anonymous access lands in the
   synthetic `desktop-demo` tenant (safe, fake data).

## 2. Use it in a chat

1. Start a new chat.
2. Click the **search-and-tools** (sliders) icon under the message box and
   make sure **HealthClaw** is toggled on.
3. Say:

   > What HealthClaw tools do you have? Then give me a summary of the health
   > record.

Claude will call the tools and narrate what it finds. From here, run the
[10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent).

## 3. On your phone

Nothing extra to do: connectors added on the web are available in the Claude
iOS/Android app. Open the app → new chat → tools icon → toggle HealthClaw on
→ talk to your record hands-free with voice dictation if you like.

## Claude Desktop (alternative, developer-style)

If you use Claude Desktop and prefer a config file, add to
`claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "healthclaw": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://mcp-server-production-5112.up.railway.app/mcp"
      ]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the tools icon.

## Pointing at your own records (optional)

Connect your providers at `https://app.healthclaw.io/connect/<your-tenant-id>`
(identity-verified via CLEAR/ID.me). When the connection completes, the page
shows a one-time **"Connect your AI assistant"** card — click "Copy setup
message" and paste it into your Claude chat (works on web, desktop, and
mobile). From then on Claude passes your tenant and read-only token on every
HealthClaw call. The token cannot write and expires in 30 days.
See [Connecting your own health data](README.md#connecting-your-own-health-data-fasten-connect).
Do not do this while screen-recording.

## Troubleshooting

- **Connector added but no tools show:** toggle it off/on in the tools menu,
  or start a fresh chat.
- **"Tool call failed":** the server may be cold-starting; retry once.
- **Claude refuses a health question:** rephrase as decision support — e.g.
  "explain these lab values in plain language" rather than "diagnose me."
  HealthClaw's outputs are decision support with disclaimers by design.
