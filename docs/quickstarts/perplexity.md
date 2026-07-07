# Quickstart: Perplexity (Pro / Max)

Time: about 3 minutes. Custom connectors require a paid Perplexity plan.

## 1. Add the connector

1. Go to [perplexity.ai](https://www.perplexity.ai) → **Settings** →
   **Connectors** (on some plans this appears as **Apps & connectors**).
2. Choose **Add connector** → **Custom / Remote MCP**.
3. Fill in:
   - **Name:** `HealthClaw`
   - **Server URL:** `https://mcp-server-production-5112.up.railway.app/mcp`
   - **Authentication:** none
4. Save, and enable the connector for your searches/threads.

## 2. Use it

Start a new thread and ask:

> Using the HealthClaw tools, give me a summary of the health record, then
> interpret the recent labs and list any preventive care gaps.

Then run the rest of the
[10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent).

## Notes

- Anonymous access = synthetic `desktop-demo` tenant (fake data, camera-safe).
- Perplexity may interleave web citations with tool results; if it starts
  web-searching instead of using the tools, say "use the HealthClaw connector
  tools, not web search."
- Availability of custom MCP connectors varies by platform (web/macOS app)
  and plan tier — if you don't see the option, check Perplexity's help
  article "Local and Remote MCPs for Perplexity."
