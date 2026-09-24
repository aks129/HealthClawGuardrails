# Quickstart: Telegram (OpenClaw bot)

The chat-app path: a Telegram bot wired to the same guardrailed stack. Pure
phone experience — no plan or connector setup, just a chat.

## Use a hosted bot

Ask the HealthClaw team (support@healthclaw.io) for the current bot handle,
open it in Telegram, and send:

```text
/start        bind the chat (demo tenant by default)
/health       stack health check
/conditions   condition list
/labs         recent lab results
/curatr       data-quality scan (`/curatr fix` proposes fixes)
/approve      approve a pending fix — the human-in-the-loop step
/connect      pull your records (Fasten; TEFCA when enabled)
/dashboard    signed 24-hour command-center link
```

The `/curatr` → `/approve` pair is the guardrail showcase: the bot proposes,
a human approves, and only then does anything change.

**Where `/approve` works today: the demo environment, not production.** In
production `$curatr-apply-fix` demands a step-up token bound to the fix
(`audience=curatr`, one operation), and no client can mint one: the only
mint endpoint issues a plain tenant token, so the bot's `/approve` is
answered "Token audience mismatch" there (#413). That is the gate doing its
job against a credential that is not an approval. The production path for a
data-quality fix is the action rail (propose, then the person approves on
their own review surface); until the fix rides that rail, `/approve` is a
demo-environment feature and this page says so rather than implying
otherwise.

## Run your own bot (5 minutes, any machine with Python)

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and
   copy the token.
2. From a clone of this repo:

   ```bash
   TELEGRAM_BOT_TOKEN=<your token> \
   TENANT_ID=desktop-demo \
   MCP_BASE_URL=https://mcp-demo-production-ee2c.up.railway.app \
   FHIR_BASE_URL=https://app.healthclaw.io/r6/fhir \
   uv run --with "python-telegram-bot==21.*" --with requests python openclaw/bot.py
   ```

3. Message your bot `/start`. That's it — it talks to the public demo server
   against the synthetic demo tenant.

`TENANT_ID` above is set to the demo tenant deliberately. The demo server is
pinned to that tenant and ignores the value, so pointing this at a real tenant
would not fail — it would quietly keep answering with synthetic data. Real
tenants need the production endpoint and its bearer token; see
[mcp-generic.md](mcp-generic.md#tenancy-and-auth).

Docker alternative: `docker-compose --profile openclaw up -d` with the same
env vars.

## WhatsApp / iMessage

Not supported yet — there is no MCP surface for them today. Telegram is the
chat-app path; the Claude and Perplexity mobile apps are the phone-native
alternative (see [claude.md](claude.md)).
