# Agent run control plane

The durable run API backs CareAgents' dedicated worker and replayable SSE
projection. Flask web processes enqueue work but never execute inference or
tools inline.

## Durable records

- `agent_runs` correlates one inbound conversation message with one run. Its
  tenant/message unique constraint makes creation idempotent.
- `agent_tool_calls` correlates a provider tool-call ID with a canonical input
  hash. Re-registering the same call returns its existing state and result;
  reusing the ID with different input fails closed.
- `agent_run_events` is append-only outside tenant purge. Its integer ID is the
  durable replay cursor used by a future SSE projection.

Run states are `queued`, `running`, `waiting_for_human`, `completed`, `failed`,
and `cancelled`. Invalid transitions are rejected in code and invalid stored
states are rejected by database constraints.

## Authentication boundaries

Tenant sessions or tenant-bound step-up credentials may create, inspect,
replay, and cancel their own runs. Worker operations span tenant queues and
therefore require `X-Internal-Secret` matching `INTERNAL_TOKEN_MINT_SECRET`.
The worker secret must never be sent to a browser or messaging client.

Claim responses are the only worker endpoint that returns inbound message
text. Tenant-facing run detail omits message text and tool arguments/results.
Operational logs must use run IDs, status, attempts, hashes, references, and
error classes only—never event payloads, message text, or tool results.

## Worker contract

1. Create a run after the inbound message is durably claimed.
2. Claim with a stable worker ID and a 10–600 second lease.
3. Heartbeat before the lease expires; stop if `cancel_requested` is true.
4. Append UI/progress events before publishing them to a transport.
5. Register each provider tool call before execution. If it replays as
   `completed`, reuse the stored result instead of executing again.
6. Transition the run to a terminal state or `waiting_for_human`.
7. An approval surface resumes a waiting run through the internal resume API.

Expired running leases are appended as `run.lease_expired`, requeued, and then
claimed with an incremented attempt. Queued runs past their deadline fail as
`RunDeadlineExceeded` rather than running late.

## HTTP endpoints

All paths are under `/command-center/api/runs`:

- `POST /` — idempotently create from `{tenant_id, message_id}`.
- `GET /<id>` — PHI-redacted run/tool projection.
- `GET /<id>/events?after=<cursor>` — ordered durable replay.
- `POST /<id>/cancel` — durable cancellation request.
- `POST /claim` — atomically claim the next queued run (internal).
- `POST /<id>/heartbeat` — renew the owning worker lease (internal).
- `POST /<id>/transition` — state transition by the owning worker.
- `POST /<id>/events` — append a bounded event before publishing it.
- `POST /<id>/tool-calls` and `/tool-calls/<call>/transition` — durable,
  idempotent tool lifecycle.
- `POST /<id>/resume` — approval surface requeues a human-waiting run.

Event and tool payloads are capped at 256 KiB. Error fields accept a class name,
not raw exception text, to keep secrets and health content out of operations.

## Deployment verification

Run migrations before web or worker processes:

```bash
uv run flask --app main init-db
```

Then verify the three new tables exist and no stale run is silently stranded:

```sql
SELECT status, COUNT(*) FROM agent_runs GROUP BY status;

SELECT id, worker_id, lease_expires_at
FROM agent_runs
WHERE status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP;
```

The second query may briefly show an expired lease; the next claim records its
recovery event and requeues it. Repeated expirations indicate a worker incident.
