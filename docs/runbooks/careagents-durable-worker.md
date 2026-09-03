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
- A claim whose response never reaches its worker — a 502, a dropped
  connection, the seconds of a rolling redeploy — is redelivered on that
  worker's next poll, with `attempt` unchanged and a `run.claim_redelivered`
  event. Redelivery goes only to the worker id that claimed the run, and only
  while the queue shows nothing since `run.started`: a run that registered a
  tool or reported a step was received, so its silence is ambiguous and stays
  with lease recovery. Without this the run sat `running` with a live lease
  and no executor for the whole lease period, which the patient experiences as
  a chat that hangs with nothing on the stream (#374).
- A worker id therefore has to name one live claim loop, not one host and PID.
  `careagents/worker.py` gives each process instance a random suffix, because
  a restarted container can be handed both of the others back.
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
| `CARE_RUN_POLL_SECONDS` | 0.5 | Empty-queue polling delay, and the floor idle backoff returns to |
| `CARE_RUN_POLL_MAX_SECONDS` | 6.0 | Idle backoff cap; doubles from the floor, any claim resets every slot. Set to the floor to pin the interval flat — the rollback, no redeploy |
| `CARE_RUN_WORKER_STALE_SECONDS` | 30 | Maximum age of successful queue access |
| `CARE_RUN_SSE_TIMEOUT_SECONDS` | 150 | One browser projection window |

Scale worker processes horizontally only when both HealthClaw and the
CareAgents identity store use shared databases. The claim path is PostgreSQL
safe (`FOR UPDATE SKIP LOCKED`); SQLite remains development-only for multiple
hosts.

## Railway

Two services built from one image. The only configuration that differs is
`CARE_ROLE` — and the public domain, which only the web service needs.
`railway add` has no start-command option, so the role has to travel in the
environment:
`deploy/careagents/Dockerfile` dispatches on it at start-up — `web` (the
default) runs gunicorn, `worker` runs `python -m careagents.worker`, and any
other value exits non-zero instead of quietly starting a second web server
(#273).

Two things own the role, and neither is the dashboard: the image, and
`CARE_ROLE`. Leave `startCommand` unset on both services. A custom start
command in the console silently overrides the dispatch, and `CARE_ROLE` then
means nothing — the live web service runs on Railway defaults
(`startCommand`, `builder`, `rootDirectory`, `restartPolicyType` all unset) for
exactly that reason.

### Stage the build

**Do not create the service from the GitHub repo.** A repo-connected build
picks up the repo-root `railway.toml`, which points at the repo-root
`Dockerfile` — the HealthClaw Flask app, not CareAgents. You would get a
"worker" serving `gunicorn main:app`, running the Flask migrations and demo
seeding in `preDeployCommand` against whatever database is bound. Deploy from a
staging directory instead, the same way the web service is deployed and for the
same reason `docs/development.md` gives for the MCP server.

From the repo root, on the commit you intend to ship:

```bash
STAGE="$(mktemp -d)"
cp pyproject.toml uv.lock "$STAGE/"
cp -R careagents "$STAGE/"
cp deploy/careagents/Dockerfile "$STAGE/Dockerfile"
./deploy/careagents/stamp_build.sh "$STAGE"    # prints e.g. build 4f2a91cbeef1
```

That is everything the image's build context needs. The Dockerfile goes to the
stage root under its default name so Railway's builder finds it without a
`dockerfilePath` — the same reason it must not be a repo-connected build.

Stamp last: `cp -R careagents` may carry a stale `BUILD_SHA` in from your
checkout, and the stamp overwrites it. Stamp for **both** services — each
`railway up` is its own upload, so a worker deployed from an unstamped stage
reports `build: unknown` while the web service reports the commit you meant to
ship. That is the pair of deployments #258 could not tell apart.

### Create the worker service

The worker runs `Config()` (`careagents/worker.py`), the same unconditional
constructor the web app runs. In production it `_require`s
`CARE_SESSION_SECRET` (32 chars or more), `HEALTHCLAW_MINT_SECRET`,
`RESEND_API_KEY`, and an LLM credential — **including the two the worker never
uses**. It sends no email and has no sessions; it still refuses to boot without
them, and a worker service missing either crash-loops on:

```text
careagents.config.ConfigError: CARE_SESSION_SECRET is required in production
careagents.config.ConfigError: RESEND_API_KEY is required in production
```

So do not hand-pick variables. The rule is: **mirror every non-`RAILWAY_*`
variable from the web service**, as a reference rather than a copied value.
Enumerate them rather than trusting a list in a document — the set drifts:

Run this from the repo root. Link the **project** first — not the worker
service, which does not exist yet:

```bash
railway link --project <project-id> --environment production
```

The whole block runs in a subshell. It exits on any failure, and `exit` in a
shell you pasted into is your session — which would also lose `$STAGE`, since
that is a `mktemp -d` path held only in a shell variable, forcing you to redo
the staging. The parentheses keep the failure inside.

```bash
(
WEB=careagents                      # the existing web service

# Names only: --json keeps every value inside the pipe. Parsing --kv with sed
# would put a multi-line value's continuation line into the name list, and from
# there into a `railway add` argv and your shell history.
#
# Everything that can go wrong here fails CLOSED, because the service this
# creates boots GREEN when it is wrong: without CARE_ENV, Config() takes the
# development path, requires nothing, and the container runs while claiming no
# work. Railway shows it healthy, /healthz stays 503, and the ConfigError this
# runbook tells you to look for was never raised.
NAMES=$(railway variables list --service "$WEB" --json | python3 -c '
import json, re, sys
d = json.load(sys.stdin)                       # empty/banner/error output -> dies here
if not isinstance(d, dict):
    sys.exit("expected a JSON object of variables")

names = [k for k in d if not re.match(r"RAILWAY_|PORT$|CARE_ROLE$", k)]

# Reject unreferenceable names LOUDLY. Filtering them would be a silent drop,
# and nothing downstream could tell that from a healthy read. The rule is
# Railway/POSIX, not Python: str.isidentifier() accepts "CAFÉ_KEY" and the
# Cyrillic-Е homoglyph "CARE_ЕNV", neither of which ${{web.NAME}} resolves.
# Checked after the exclusion, so a name we were never going to forward cannot
# block the run.
bad = sorted(k for k in names if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
if bad:
    sys.exit("cannot build a reference for: " + ", ".join(bad))

# What the worker cannot boot without, from careagents/config.py. Named, not
# counted: a threshold blocks a legitimate cleanup and waves through fifteen
# names that happen to omit CARE_ENV — the one whose absence turns a broken
# worker green.
missing = {"CARE_ENV", "CARE_SESSION_SECRET", "HEALTHCLAW_MINT_SECRET",
           "RESEND_API_KEY"} - set(names)
if missing:
    sys.exit("enumeration is missing " + ", ".join(sorted(missing)))
if not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} & set(names):
    sys.exit("enumeration has no LLM credential")

print("\n".join(names))
') || exit 1

args=(--service careagents-worker --variables "CARE_ROLE=worker")
# `while read`, not `for name in $NAMES`: zsh does not word-split unquoted
# expansions, so a for-loop runs ONCE over the whole list and collapses all 17
# names into a single malformed --variables argument. The service then boots
# without CARE_ENV, takes the development path where nothing is required, and
# runs green while draining nothing — no ConfigError to find. This loop
# behaves the same in bash and zsh.
while IFS= read -r name; do
  [ -n "$name" ] && args+=(--variables "$name=\${{$WEB.$name}}")
done <<< "$NAMES"

railway add "${args[@]}"
)
```

If it refuses, it names what it found and nothing was created. **`cannot build
a reference for: MY-VAR`** — Railway holds a name that `${{web.NAME}}` cannot
express. Either rename it on the web service, or, if the worker does not need
it, add it to the exclusion pattern beside `PORT` and `CARE_ROLE`.
**`enumeration is missing CARE_ENV`** — you are reading the wrong service or
project; check `railway status` before retrying. In both cases `$STAGE` is
intact and you can re-run the block as-is.

A reference resolves at deploy time, so rotating the web service's secret
rotates the worker's with it and no secret is ever pasted into a second field.
At the time of writing the web service carries 17 of them: `CARE_DATABASE_URL`,
`CARE_EMAIL_FROM`, `CARE_ENV`, `CARE_IMESSAGE_HANDLE`, `CARE_MODEL`,
`CARE_OPENAI_MODEL`, `CARE_ORIGIN`, `CARE_RP_ID`, `CARE_RP_NAME`,
`CARE_SESSION_SECRET`, `CARE_TELEGRAM_BOT`, `FASTEN_PUBLIC_KEY`,
`HEALTHCLAW_BASE`, `HEALTHCLAW_MINT_SECRET`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `RESEND_API_KEY`. Note which LLM credential that is:
`ANTHROPIC_API_KEY` is **not** set, which is why `/healthz` reports
`provider: openai`. Referencing a variable the web service does not have gives
you an empty value — a boot refusal if it was the only credential, or worse, a
worker that claims runs and fails every one at inference while readiness reports
green.

Give the worker **no healthcheck path and no public domain**. It serves no
HTTP, so Railway's default (no path configured, which is what the web service
also runs with) is correct; a healthcheck path copied across from the web
service leaves the deploy permanently un-healthy. The `HEALTHCHECK` in the
Dockerfile is a different thing — Railway ignores it, and it is role-aware
anyway (`careagents/healthcheck.py` checks queue presence for the worker).

### Deploy and confirm it worked

**`cd` into the stage; do not pass it as an argument.** `railway up <path>`
roots the archive at the *project directory* rather than at the path unless you
add `--path-as-root`, and it applies `.gitignore` — which lists
`careagents/BUILD_SHA`. Run from the repo root and the marker is dropped from
the upload, leaving a deployment that reports `build: unknown` and alarms as
stale forever. `cd`-then-`up` is the form that has actually been used to deploy
this service.

**`railway link` is per-directory, so you must link again inside the stage.**
The earlier link applied to the repo root. `railway up` in an unlinked
directory does not fail — it **creates a brand-new project** named after the
directory and deploys there, printing a cheerful `✓ Project tmp.XXXXXXXX`.
Your real services are untouched, the deploy you think you shipped is running
somewhere nobody looks, and the only clue is a project name you did not choose.
This happened on the first run of this runbook.

```bash
cd "$STAGE"
railway link --project <project-id> --service careagents-worker --environment production
railway up --service careagents-worker --detach
```

Sanity-check before you upload: `railway status` from inside `$STAGE` must name
your real project. If it names anything else, stop — do not `railway up`.

One stage serves both roles. Upload it twice — once per service — and the two
report the same `build`. You are already linked to the project from the step
above, so `--service` alone picks the target:

```bash
cd "$STAGE"
railway up --service careagents        --detach   # production web redeploy
railway up --service careagents-worker --detach
```

Then confirm from the **web** service, which is where the worker becomes
visible. Readiness flips only once a worker's presence is fresh:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://careagents.cloud/healthz   # 200
curl -s https://careagents.cloud/healthz                                    # "run_workers": true
```

`200` with `"run_workers": true` and a `build` matching the commit you staged is
the finish line. If it stays 503, the worker is not running: check its deploy
logs for a `ConfigError`, and check presence directly with the
`agent_worker_presence` query under [Operations](#operations).

### A deployment with only the web service

It looks healthy in a browser — the landing page and `/auth` render — and fails
everywhere that matters:

```text
GET /healthz -> 503
{"accounts": true, "provider": "openai", "run_workers": false,
 "run_workers_state": "not_ready", "status": "degraded"}

POST /api/chat -> run_workers_unavailable
```

That is readiness failing closed because no durable worker presence is fresh,
not a regression. The fix is to add the worker service. Never relax the check:
a green `/healthz` with nothing draining the queue is the state the fail-closed
design exists to make visible.

### Telling that apart from "we could not ask" (#219)

`run_workers_state` names which of three things happened, and only one of them
fails the endpoint:

| `run_workers_state` | `run_workers` | `/healthz` | Means |
| --- | --- | --- | --- |
| `ready` | `true` | 200 | A worker's presence is fresh. The finish line above. |
| `not_ready` | `false` | **503** | HealthClaw answered: nothing is draining the queue. The section above — fix the deployment. |
| `unknown` | `false` | 200 | We could not reach HealthClaw's worker-health endpoint within 2s. |

`unknown` is a statement about HealthClaw, not about this container, so it does
not fail readiness: Railway probes only at the start of a deploy, and blocking
a CareAgents deploy on a HealthClaw outage blocks the deploy that would fix it
while traffic stays on a previous version with the same dependency. Chat still
refuses (`run_workers_unknown`), so nobody is routed into a turn no worker can
run. **The finish-line check is therefore `200` *and* `"run_workers": true`,
not `200` alone.**

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

-- Lost claim responses, recovered and unrecovered. The first counts claims
-- the edge dropped after they committed; a rise tracks redeploys. The next
-- two are what that used to cost before redelivery existed, so they should
-- now move only on a real worker crash.
SELECT count(*) FROM agent_run_events WHERE event_type = 'run.claim_redelivered';
SELECT count(*) FROM agent_run_events WHERE event_type = 'run.lease_expired';
SELECT count(*) FROM agent_runs WHERE attempt > 1;
```

An increasing `needs_reconciliation` count requires provider-specific truth
lookup before an operator resolves a tool call. Reconciliation requires the
separate `AGENT_RUN_RECONCILE_SECRET`, records only an opaque evidence ID, and
never turns an abandoned run into a silent success. The reconciliation UI and
alerts are tracked separately in issue #255.
