# Conversation identity and migration

HealthClaw stores each chat turn under a durable, tenant-owned
`conversation_id`. CareAgents uses `careagents:<agent_id>` as the stable default,
so web, Telegram, and iMessage can resume the same thread for that agent without
sharing history with another agent attached to the same record connection.

## Caller contract

- Send the same `conversation_id` on every turn that belongs to one thread.
- Send the CareAgents agent UUID as `agent_id`. HealthClaw treats it as an opaque,
  tenant-scoped identity rather than a command-center registry key.
- Send a stable `request_id` for every inbound delivery. A retry returns HTTP 200
  with `idempotent_replay: true` and the original message ID; a new claim returns
  HTTP 201. Never run inference or a tool after an idempotent replay.
- Set `surface` to `web`, `telegram`, `imessage`, `api`, or the originating
  adapter. Set an assistant message's `reply_to` to the claimed inbound message
  ID.
- A conversation cannot change tenants or agents after creation. Attempts return
  404 for a tenant mismatch and 409 for an agent mismatch.

If an older caller omits `conversation_id`, HealthClaw uses
`careagents:<agent_id>` when an agent is present, otherwise
`legacy:<tenant_id>`. Explicit IDs are preferred.

## Deployment and backfill

Migration `0004_conversation_identity` creates `cc_conversations`, adds thread,
request, and reply fields to messages, and adds a unique database constraint on
`(tenant_id, conversation_id, request_id)`. Existing tenant-wide messages are
preserved in one compatibility thread named `legacy:<tenant_id>`; no transcript
rows are discarded.

Run the normal migration command before starting web processes:

```bash
uv run flask --app main init-db
```

CareAgents workers serialize a conversation by locking its durable
`cc_conversations` row while claiming the next run. Multiple web and worker
processes are therefore safe without a process-local transcript or Redis lock;
the database uniqueness constraint remains the inbound idempotency boundary.

After deployment, verify that every message has a conversation and that no
duplicate request keys exist:

```sql
SELECT COUNT(*) FROM cc_conversation_messages WHERE conversation_id IS NULL;

SELECT tenant_id, conversation_id, request_id, COUNT(*)
FROM cc_conversation_messages
WHERE request_id IS NOT NULL
GROUP BY tenant_id, conversation_id, request_id
HAVING COUNT(*) > 1;
```

Both queries should return zero rows/counts. Legacy threads remain readable and
may be archived later only through an explicit retention decision.
