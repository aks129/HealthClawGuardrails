# CareAgents durable worker and SSE replay

CareAgents has two independent process roles:

- **Web:** authenticates the account, durably claims the inbound message,
  creates its queued `AgentRun`, and projects durable events over SSE.
- **Worker:** claims queued runs, heartbeats a lease, performs model/tool work,
  persists the assistant outcome, and marks the run terminal.

The browser stream is not the lifecycle. Closing a tab or losing a connection
only ends that projection. Reconnect with the run ID and the last event cursor:

```text
GET /api/chat/runs/<run_id>/events?agent_id=<agent_id>&after=<cursor>
```

## Ordering and recovery guarantees

- A database lock on `cc_conversations` allows only one running turn per
  conversation across all worker processes.
- History is loaded through the claimed `message_id`, not from the live tail.
  Later queued messages therefore cannot enter an earlier prompt.
- Each model result is checkpointed before tools execute.
- Tool identity is `(run_id, provider_call_id)`. Completed results are replayed
  after recovery without calling the tool again.
- A tool left `running` after lease expiry has an unknown provider outcome. It
  moves to `needs_reconciliation`, and its run pauses in
  `waiting_for_human`. Workers never retry that side effect blindly.
- Assistant messages use `run:<run_id>:assistant` as their durable request key,
  so recovery cannot append the final answer twice.

## systemd

Install and enable both units from `deploy/careagents/`:

```bash
sudo systemctl enable --now careagents careagents-worker
sudo systemctl status careagents careagents-worker
```

The worker pool is bounded by `CARE_RUN_WORKERS` (default 4). Important knobs:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `CARE_RUN_WORKERS` | 4 | Concurrent worker slots per process |
| `CARE_RUN_DEADLINE_SECONDS` | 120 | End-to-end run deadline |
| `CARE_RUN_LEASE_SECONDS` | 60 | Claim lease, heartbeated every third |
| `CARE_RUN_POLL_SECONDS` | 0.5 | Empty-queue polling delay |
| `CARE_RUN_SSE_TIMEOUT_SECONDS` | 150 | One browser projection window |

Scale worker processes horizontally only when both HealthClaw and the
CareAgents identity store use shared databases. The claim path is PostgreSQL
safe (`FOR UPDATE SKIP LOCKED`); SQLite remains development-only for multiple
hosts.

## Compose

Run the optional local profile:

```bash
docker compose --profile careagents up --build
```

This starts `careagents` and `careagents-worker` against the same HealthClaw
service and CareAgents data volume.

## Operations

Queue and lease health live in HealthClaw:

```sql
SELECT status, COUNT(*) FROM agent_runs GROUP BY status;
SELECT id, worker_id, lease_expires_at
FROM agent_runs
WHERE status = 'running'
ORDER BY lease_expires_at;
SELECT id, run_id, tool_name, error_class
FROM agent_tool_calls
WHERE status = 'needs_reconciliation';
```

An increasing `needs_reconciliation` count requires provider-specific truth
lookup before an operator resolves a tool call. The reconciliation UI and
alerts are tracked separately in issue #255.
