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
  and HealthClaw commits that message, its `agent.text` event, and run
  completion in one fenced transaction. Recovery cannot append the final
  answer twice, and a stale worker cannot publish after lease revocation.
- Idle claims and owned-run heartbeats update durable worker presence only
  after a successful queue transaction. Web readiness and chat admission fail
  closed when no presence is fresh. The readiness poll also sweeps a bounded
  batch of overdue queued or running runs, so expiry does not depend on a
  client or worker.
- A run heartbeat never extends a lease beyond the hard run deadline. Once the
  deadline is reached, the authoritative heartbeat transaction revokes
  ownership before a late provider result can be persisted. Pure model work
  fails; an in-flight side effect instead enters reconciliation.

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
| `CARE_RUN_WORKER_STALE_SECONDS` | 30 | Maximum age of successful queue access |
| `CARE_RUN_SSE_TIMEOUT_SECONDS` | 150 | One browser projection window |

Scale worker processes horizontally only when both HealthClaw and the
CareAgents identity store use shared databases. The claim path is PostgreSQL
safe (`FOR UPDATE SKIP LOCKED`); SQLite remains development-only for multiple
hosts.

## Railway

Two services, one image, differing only by `CARE_ROLE`. `railway add` has no
start-command option, so the role has to travel in the environment:
`deploy/careagents/Dockerfile` dispatches on it at start-up — `web` (the
default) runs gunicorn, `worker` runs `python -m careagents.worker`, and any
other value exits non-zero instead of quietly starting a second web server
(#273).

Create the worker service from the same repo and the same Dockerfile path as
the web service, then set `CARE_ROLE=worker` on it. It performs the inference
and the tool calls, so it needs the same configuration the web service has, not
a subset — a worker pointed at a different HealthClaw, holding a different mint
secret, or reading a different accounts database claims nothing and its
presence never reaches the web role's readiness check. Railway variable
*references* share that configuration without copying secret values anywhere:

| Worker variable | Value |
| --- | --- |
| `CARE_ROLE` | `worker` |
| `HEALTHCLAW_BASE` | `${{careagents.HEALTHCLAW_BASE}}` |
| `HEALTHCLAW_MINT_SECRET` | `${{careagents.HEALTHCLAW_MINT_SECRET}}` |
| `CARE_DATABASE_URL` | `${{careagents.CARE_DATABASE_URL}}` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | `${{careagents.ANTHROPIC_API_KEY}}` |
| `CARE_ENV` | `${{careagents.CARE_ENV}}` |

Substitute the web service's own name for `careagents`. A reference resolves at
deploy time, so rotating the web service's secret rotates the worker's with it
and the value is never pasted into a second dashboard field.

Stamp the build marker for **both** services. Each is its own upload, so run

```bash
./deploy/careagents/stamp_build.sh "$STAGE"
```

against the staging directory before every `railway up`, whichever service it
targets. A worker deployed from an unstamped stage reports `build: unknown`
while the web service reports the commit you meant to ship, which is precisely
the pair of deployments #258 could not tell apart.

### A deployment with only the web service

It looks healthy in a browser — the landing page and `/auth` render — and fails
everywhere that matters:

```text
GET /healthz -> 503
{"accounts": true, "provider": "openai", "run_workers": false,
 "status": "degraded"}

POST /api/chat -> run_workers_unavailable
```

That is readiness failing closed because no durable worker presence is fresh,
not a regression. The fix is to add the worker service. Never relax the check:
a green `/healthz` with nothing draining the queue is the state the fail-closed
design exists to make visible.

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
SELECT worker_id, last_seen_at
FROM agent_worker_presence
ORDER BY last_seen_at DESC;
SELECT id, run_id, tool_name, error_class
FROM agent_tool_calls
WHERE status = 'needs_reconciliation';
```

An increasing `needs_reconciliation` count requires provider-specific truth
lookup before an operator resolves a tool call. Reconciliation requires the
separate `AGENT_RUN_RECONCILE_SECRET`, records only an opaque evidence ID, and
never turns an abandoned run into a silent success. The reconciliation UI and
alerts are tracked separately in issue #255.
