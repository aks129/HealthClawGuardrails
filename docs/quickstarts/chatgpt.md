# Quickstart: ChatGPT (Plus/Pro — Developer Mode)

Time: about 4 minutes. ChatGPT's public app directory does not list health
connectors (its policy restricts PHI apps), but **Developer Mode lets any
Plus/Pro user add a remote MCP server by URL** — HealthClaw works there today.

## 1. Turn on Developer Mode

1. ChatGPT → **Settings** → **Apps & Connectors** (web) →
   **Advanced settings** → enable **Developer mode**.

## 2. Add the connector

1. Settings → **Apps & Connectors** → **Create** (or **Add connector**).
2. Fill in:
   - **Name:** `HealthClaw`
   - **MCP server URL:** `https://mcp-server-production-5112.up.railway.app/mcp`
   - **Authentication:** No authentication
3. Accept the unverified-connector notice and save.

## 3. Use it

In a new chat, open the tools/plus menu, enable **HealthClaw**, and ask:

> Use the HealthClaw tools to summarize the health record, interpret the
> recent labs, and check preventive care gaps.

Then run the rest of the
[10-minute demo script](README.md#the-10-minute-demo-script-works-in-any-connected-agent).

## Notes

- Anonymous access = synthetic `desktop-demo` tenant (fake data, camera-safe).
- Developer-mode connectors are marked "unverified" — expected.
- ChatGPT sometimes needs the nudge "call the HealthClaw tool for this
  rather than answering from memory."
