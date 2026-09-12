# Quickstart: Claude (web, desktop, iPhone/Android)

Time: about 3 minutes. Requires a Claude Pro or Max plan (custom connectors
are not available on the free tier).

## 1. Add the connector (do this once, on the web)

1. Go to [claude.ai](https://claude.ai) in a browser and sign in.
2. Click your initials (bottom-left) → **Settings** → **Connectors**.
3. Click **Add custom connector**.
4. Fill in:
   - **Name:** `HealthClaw`
   - **URL:** `https://mcp-demo-production-ee2c.up.railway.app/mcp`
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
        "https://mcp-demo-production-ee2c.up.railway.app/mcp"
      ]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the tools icon.

## Pointing at your own records (optional)

> **Not available through a claude.ai connector today.** The demo URL above is
> hard-pinned to the synthetic tenant: it *ignores* the tenant and token that
> the setup message passes, and answers from demo data anyway — so it will look
> like it worked while showing you fake records. Real tenants require the
> production endpoint, which needs a bearer header that hosted connectors
> cannot attach ([#290](https://github.com/aks129/HealthClawGuardrails/issues/290)).
> Until that lands, use your own records through
> CareAgents only if your account has separately authorized real-record
> access, or through an operator-configured local MCP client that can set
> headers. The current synthetic beta cohort does not open real-record
> connections.

Do not paste a private tenant token or a "Copy setup message" into this demo
chat. The connector still reads synthetic records. Keep credentials in the
client's protected configuration.

See [Connecting your own health data](README.md#connecting-your-own-health-data-fasten-connect)
for the separate paths and their prerequisites. Do not record private-record
setup or credentials.

## Troubleshooting

- **"Couldn't register with HealthClaw's sign-in service" / Claude asks for an
  OAuth Client ID:** you are pointed at the production endpoint
  (`mcp-server-production-5112...`), not the demo one. That server requires a
  bearer token, so it answers `401`, and Claude tries to start an OAuth
  sign-in — but the server publishes no OAuth metadata, so registration fails
  and Claude falls back to asking you for a Client ID. **There is no Client ID
  to enter.** Remove the connector and re-add it with the demo URL in step 4
  above. (Hosted connectors cannot attach a static bearer header, so the
  production endpoint is not usable from claude.ai —
  [#290](https://github.com/aks129/HealthClawGuardrails/issues/290).)
- **Connector added but no tools show:** toggle it off/on in the tools menu,
  or start a fresh chat.
- **"Tool call failed":** the server may be cold-starting; retry once.
- **Claude refuses a health question:** rephrase as decision support — e.g.
  "explain these lab values in plain language" rather than "diagnose me."
  HealthClaw's outputs are decision support with disclaimers by design.
