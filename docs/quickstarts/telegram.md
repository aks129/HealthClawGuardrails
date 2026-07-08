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
/connect      pull your records (Fasten + TEFCA)
/dashboard    signed 24-hour command-center link
```

The `/curatr` → `/approve` pair is the guardrail showcase: the bot proposes,
a human approves, and only then does anything change.

## Run your own bot (5 minutes, any machine with Python)

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and
   copy the token.
2. From a clone of this repo:

   ```bash
   TELEGRAM_BOT_TOKEN=<your token> \
   TENANT_ID=desktop-demo \
   MCP_BASE_URL=https://mcp-server-production-5112.up.railway.app \
   FHIR_BASE_URL=https://app.healthclaw.io/r6/fhir \
   uv run --with "python-telegram-bot==21.*" --with requests python openclaw/bot.py
   ```

3. Message your bot `/start`. That's it — it talks to the production
   guardrail stack against the synthetic demo tenant.

Docker alternative: `docker-compose --profile openclaw up -d` with the same
env vars.

## WhatsApp / iMessage

Not supported yet — there is no MCP surface for them today. Telegram is the
chat-app path; the Claude and Perplexity mobile apps are the phone-native
alternative (see [claude.md](claude.md)).
