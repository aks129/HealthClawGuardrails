# Probe — what actually happens to `/healthz` under concurrent chat turns (#219)

- **Date:** 2026-09-03
- **Branch / HEAD:** `probe/219-thread-saturation` off `origin/main` @ `89b42fb`
- **Script:** `scripts/probe_219_thread_saturation.py` (committed; re-runnable
  from `main` by a non-author with
  `uv run python scripts/probe_219_thread_saturation.py`)
- **Production was not touched.** No load was sent to `careagents.cloud` or
  `app.healthclaw.io`. Every number below comes from a local gunicorn plus a
  fake HealthClaw on loopback.
- **One redaction:** the operator's home-directory name in pasted shell output
  reads `<user>`. Nothing else in any transcript is altered.

## Verdict: **REPRODUCES DIFFERENTLY**

The starvation half is real and worse than stated. The restart half does not
happen, and most of the data-loss half is already false.

| # | Claim in the issue | Measured |
|---|---|---|
| 1 | "One worker, 8 threads" | **Wrong.** `--workers 2 --threads 4`. Still 8 threads, but the split makes saturation start *earlier* and *non-deterministically* |
| 2 | "Each chat turn holds a thread ... 6 tool rounds × 25s + 90s LLM" | **Wrong mechanism, right symptom.** The web tier runs no inference since #257. The hold is the SSE replay loop, bounded by the 120s run deadline |
| 3 | "8 concurrent turns block everything including `/healthz`" | **Confirmed at 8, intermittent from 6.** At the code's own worst case `/healthz` waits **118s** |
| 4 | "Railway marks the container unhealthy → restart" | **Does not happen.** Railway's healthcheck is deploy-time only; it does not monitor a running container |
| 5 | "All chat histories wiped" | **False.** Transcripts live in HealthClaw, per tenant |
| 6 | "Rate-limit state wiped" | **Half true.** The in-process *burst* limiter resets (by design); the durable daily cap and the login-code attempt counter both survive |
| — | *(not in the issue)* | **`/healthz` breaks its own 5s probe budget at zero load**, because it makes a 25s-timeout call to HealthClaw |
| — | *(not in the issue)* | **Each open chat turn polls HealthClaw 4×/s.** 8 turns ≈ **31 req/s** aimed at the HealthClaw that serves the clinician |

---

## 1. What the code actually does (the premise moved under the issue)

The issue is quoted from the 2026-08-01 architecture audit
(`docs/2026-08-01-alignment-review.md:44`). Since then #257 moved inference out
of the web process, which changes the mechanism the issue describes.

A later review already caught this. `docs/2026-08-05-pattern-first-architecture-review.md:161`
describes the same component as "**8 request slots held by 150s SSE turns**" —
the correct mechanism, four days after the audit #219 quotes. The issue was
never updated to match, and has been read since as if the audit's version were
current. This probe confirms the 08-05 reading and measures it.

`careagents/wsgi.py:3`:

> The WSGI process only authenticates, enqueues, and replays durable run
> events. Inference and tools execute in ``python -m careagents.worker``.

So `/api/chat` (`careagents/app.py:873`) claims the inbound message, creates a
durable run, and returns an SSE stream. The gunicorn thread is held by
`_stream_run` (`careagents/app.py:833`), which polls HealthClaw for run events
every `run_sse_poll_seconds` until the run is terminal.

The numbers the issue multiplies together are the *worker's* budget, not the
web tier's:

| Setting | Value | Where |
|---|---|---|
| `MAX_TOOL_ROUNDS` | 6 | `careagents/agent.py:24` |
| HealthClaw client read timeout | 25.0s | `careagents/healthclaw.py:54` |
| `run_deadline_seconds` | 120 | `careagents/config.py` |
| `run_sse_timeout_seconds` | 150 | `careagents/config.py` |
| `run_sse_poll_seconds` | 0.25 | `careagents/config.py` |
| gunicorn | `--workers 2 --threads 4 --timeout 180` | `deploy/careagents/Dockerfile:54` |

Two consequences the issue does not account for:

- **The turn is already bounded.** `run_deadline_seconds` is 120 and the worker
  checks it between calls (`careagents/worker.py:439`). `llm._attempt_timeout`
  derives the model budget from it, explicitly so a call cannot outlive the
  deadline by more than one in-flight request. Mitigation 1 in the issue
  ("bound the turn") is substantially shipped; the remaining lever is
  *lowering* 120, not adding a bound.
- **The web thread hold is the SSE stream, and it can chain.** When a stream
  hits 150s before the run is terminal it emits `reconnect`, and the browser
  reopens `/api/chat/runs/<id>/events` (`careagents/static/chat.js:301`). One
  user's hold is therefore "until the run is terminal", across reconnects.

## 2. Rig — what was faked and why

Real: `careagents.wsgi:app` under the Dockerfile's own gunicorn invocation, the
real `HealthClawClient` with its real 25s timeout, real sessions, real
rate limiters, real sqlite account store.

Faked: **HealthClaw only**, as a loopback `ThreadingHTTPServer`, with
`HEALTHCLAW_BASE` pointed at it. Nothing is monkeypatched inside gunicorn, so
the client and its timeouts are exercised as shipped. The fake answers
token-mint, message-claim, run-create and worker-health instantly, and holds a
run in `running` for a controlled number of seconds. **That controlled duration
is the independent variable** — it is exactly how long a chat turn holds a
gunicorn thread, without needing a model or a network.

The LLM is not faked because the web tier never calls it.

Two settings were changed for the rig and are disclosed here because they could
otherwise fake a result: `CARE_CHAT_TURNS` and `CARE_CHAT_TURNS_PER_DAY` are
raised, so that a rate-limited turn — which returns 429 instantly and holds no
thread — cannot masquerade as a held thread. Every scenario reports how many
turns actually received their `accepted` event.

One macOS-only workaround: `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, without
which gunicorn's pre-fork children crash on this machine. The container runs
Linux, where the variable is inert.

## 3. Item 1 — does `/healthz` starve? Yes: total at N=8, intermittent at N=6

```
### Item 1 — does /healthz starve? ###
  [2w x 4t (8 threads)] N=0   started=0/0   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [2w x 4t (8 threads)] N=4   started=4/4   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 4t (8 threads)] N=6   started=6/6   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 4t (8 threads)] N=8   started=7/8   healthz(5s) 3/4 healthz(30s) 1/1 true wait 18.19s
  [2w x 4t (8 threads)] N=10  started=8/10  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.13s
  [2w x 4t (8 threads)] N=16  started=8/16  healthz(5s) 0/4 healthz(30s) 0/1 true wait 38.32s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  0    20s           0/0  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.01s       0.0
  4    20s           4/4  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.00s     109.9
  6    20s           6/6  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.00s     165.3
  8    20s           7/8  3/4 ok, max 5.00s 1/1 ok, max 18.19s     18.19s      30.9
 10    20s          8/10  1/4 ok, max 5.00s 1/1 ok, max 18.12s     18.13s      38.4
 16    20s          8/16  0/4 ok, max 5.00s 0/1 ok, max 30.00s     38.32s      31.2
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
  [1w x 8t (8 threads)] N=0   started=0/0   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=4   started=4/4   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=6   started=6/6   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=8   started=8/8   healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.18s
  [1w x 8t (8 threads)] N=10  started=8/10  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.28s
  [1w x 8t (8 threads)] N=16  started=8/16  healthz(5s) 0/4 healthz(30s) 0/1 true wait 38.49s

/healthz under N concurrent chat turns — 1w x 8t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  0    20s           0/0  4/4 ok, max 0.01s  1/1 ok, max 0.01s      0.01s       0.0
  4    20s           4/4  4/4 ok, max 0.00s  1/1 ok, max 0.00s      0.01s     110.3
  6    20s           6/6  4/4 ok, max 0.00s  1/1 ok, max 0.01s      0.01s     165.5
  8    20s           8/8  1/4 ok, max 5.00s 1/1 ok, max 18.18s     18.18s      31.0
 10    20s          8/10  1/4 ok, max 5.00s 1/1 ok, max 18.27s     18.28s      37.6
 16    20s          8/16  0/4 ok, max 5.00s 0/1 ok, max 30.00s     38.49s      30.8
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
```

The two budget columns are comparisons, not CareAgents' own settings: 5s is
the Dockerfile's `HEALTHCHECK --timeout`, and 30s is the `healthcheckTimeout`
this repo's `railway.toml` sets for *HealthClaw* (and Railway's
`healthcheckTimeout` is a deploy-window total, not a per-probe timeout).
`/healthz` was the only route measured; every route shares the same thread
pool by construction, but that was not separately exercised.

**Reading it.** `/healthz` does not fail closed or refuse a connection; it
connects instantly and then waits for a free thread. The wait is the remaining
lifetime of whichever turn frees first, which is why "true wait" tracks the
hold (20s) rather than growing with N. Above the thread count, waits stack:
at N=16 the second batch of turns starts before the first releases, so the
measured wait is **38.32s** for a 20s hold, and only 8 of the 16 turns get a
thread at all within the settle window.

**The 2×4 split makes it worse, not better.** Gunicorn pre-fork workers accept
from a shared socket, so six turns do not spread three-and-three. When they
land five-and-one, one worker is saturated at 4 while the other idles, and
`/healthz` starves or not depending on which worker accepts it. That is a coin
flip, and a single sample cannot show it:

```
### Why 2w x 4t starves below 8 — N=6, repeated ###
  arrivals in the same millisecond (burst):
  run 1/6: /healthz true wait 18.11s  (5s budget 3/4 ok)
  run 2/6: /healthz true wait 18.13s  (5s budget 1/4 ok)
  run 3/6: /healthz true wait 0.01s  (5s budget 1/4 ok)
  run 4/6: /healthz true wait 18.27s  (5s budget 1/4 ok)
  run 5/6: /healthz true wait 0.01s  (5s budget 4/4 ok)
  run 6/6: /healthz true wait 18.21s  (5s budget 3/4 ok)

  N=6 on 2w x 4t, 6 runs: /healthz stalled in 4/6. Waits: [18.11, 18.13, 0.01, 18.27, 0.01, 18.21]

  arrivals spread 0.3s apart (closer to real traffic):
  run 1/6: /healthz true wait 17.00s  (5s budget 1/4 ok)
  run 2/6: /healthz true wait 0.01s  (5s budget 4/4 ok)
  run 3/6: /healthz true wait 0.01s  (5s budget 1/4 ok)
  run 4/6: /healthz true wait 16.99s  (5s budget 1/4 ok)
  run 5/6: /healthz true wait 16.97s  (5s budget 2/4 ok)
  run 6/6: /healthz true wait 16.97s  (5s budget 1/4 ok)

  N=6 on 2w x 4t, 6 runs: /healthz stalled in 4/6. Waits: [17.0, 0.01, 0.01, 16.99, 16.97, 16.97]
```

Bimodal, as the mechanism predicts: either ~0.0s (accepted by the free worker)
or ~18s (accepted by the saturated one). **The shipping configuration
intermittently starves at 6 concurrent turns, not 8.**

Both arrival patterns were run because a same-millisecond burst is a plausible
rig artifact — it could bias gunicorn's accept race toward whichever worker
woke first, and real arrivals are spread over seconds. **Spreading the arrivals
does not remove the stall**, which is the point that matters: staggering them
0.3s apart produced the same bimodal shape and a comparable rate.

The *rate* itself should not be quoted precisely. Across three attempts at
N=6 — one earlier burst run not reproduced here, plus the two above — the
stall count was 2, 4 and 4 out of 6. On six trials that spread is ordinary
noise, so the honest statement is "intermittent, roughly a third to two thirds
of the time", not "4 in 6". What is stable across all three is the shape: the
wait is either ~0s or ~18s, never in between, because it depends on which
worker accepted the probe.

## 4. Item 2 — the real per-turn thread hold

```
### Item 2 — real per-turn thread hold ###

Per-turn web-thread hold (one turn, 2w x 4t)
--------------------------------------------
 run duration   thread held   accepted after   poll reqs  events
           2s         2.09s           0.018s           9  accepted,text,done
          10s        10.13s           0.025s          40  accepted,text,done
          30s        30.02s           0.007s         116  accepted,text,done
```

The web thread is held for the whole life of the run, to within ~0.1s, and the
poll count confirms ~3.9 requests/second per open stream. Typical and worst
case are therefore not properties of the web tier at all — they are whatever
the *run* takes, bounded by:

- **typical:** one model round, no tools — a few seconds.
- **worst case as the code stands:** `run_deadline_seconds` = **120s**, plus at
  most one in-flight provider call that the between-calls deadline check cannot
  cancel (`careagents/worker.py:299` says so explicitly). Then the SSE emits
  `reconnect` at 150s and the browser reopens, so the *user-visible* hold
  chains until the run is terminal.

At that worst case:

```
### Worst case — 8 turns each holding 120s (the run deadline) ###
  [2w x 4t (8 threads)] N=8   started=8/8   healthz(5s) 0/4 healthz(30s) 0/1 true wait 118.12s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8   120s           8/8  0/4 ok, max 5.00s 0/1 ok, max 30.00s    118.12s      30.7
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
```

**8 concurrent turns at the code's own deadline make `/healthz` wait 118
seconds.** That is the load-bearing number for everything below.

## 5. Item 3 — what the platform's health check actually requires

This is the finding that decides the issue, and it is documentary, not
measured.

**Railway does not probe a running container.** From
<https://docs.railway.com/guides/healthchecks>:

> The healthcheck endpoint is currently **not used for continuous monitoring**
> as it is only called at the start of the deployment.

The default timeout is 300s, and on failure "the deploy will be marked as
failed" — the new deployment is not activated and traffic stays on the previous
version. The docs do not describe honouring a Dockerfile `HEALTHCHECK`.

**The Dockerfile's own arithmetic, since the question was asked.**
`deploy/careagents/Dockerfile:39` sets
`HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3`. A
probe that exceeds 5s counts as one failure, and three consecutive failures
30s apart mark the container `unhealthy` — so **90 seconds of continuous
saturation**. The 118s worst case in §4 does exceed that. Two caveats make it
moot on the hosts CareAgents actually runs on:

- **Docker does not restart an unhealthy container.** `unhealthy` is a status;
  something else (Swarm, Kubernetes, a `--restart` supervisor acting on it) has
  to convert it into a restart. Neither of CareAgents' hosts does.
- **Railway does not evaluate it at all** (the quote above), and the VPS
  alternative `deploy/careagents/careagents.service` has `Restart=on-failure`
  with *no* health check — systemd restarts on process *exit*, which saturation
  does not cause. Gunicorn's `--timeout 180` does not cause one either: it is a
  master-to-worker heartbeat that the worker's main loop refreshes on every
  poll iteration, and busy pool threads do not block that loop. It never fires
  from saturation, at any hold length.

**So there is no mechanism, on either host, to convert a slow `/healthz` into a
restart.** No restart, no wipe, no loop. The answer to "how many consecutive
slow health checks trigger a restart" is: on a Docker-native orchestrator that
acts on HEALTHCHECK, three (90s); on Railway and on the systemd VPS, no number
exists, because nothing is watching.

What saturation *does* cost on Railway is real but different: **a deploy that
lands while the container is saturated fails its health check and does not go
live.** Traffic stays on the old version. That is a stuck deploy, not an
outage.

## 6. Item 4 — is the data-loss half real? Mostly not.

```
### Item 4 — what a restart actually loses ###
DEV email — Verify your email for restart-probe-1788474575@example.invalid: 89021758
  burst limiter (CARE_CHAT_TURNS=3), 4 turns before restart: [200, 200, 200, 429]
  first turn AFTER restart: 200 data: {"type": "accepted", "run_id": "run-4", "next_cursor": 0}

id: 1
data: {"t
  durable daily counter (UsageDay.turns) across the restart: [4]
  login-code attempts read back through a new engine: 2
  chat turns persisted to HealthClaw, not CareAgents memory: 3 POSTs to /command-center/api/conversations
```

Four stores, measured separately:

| State | Where it lives | Survives a restart? |
|---|---|---|
| Chat transcripts | HealthClaw `cc_conversation_messages`, per tenant (`careagents/healthclaw.py:571`, `r6/command_center/models.py:62`) | **Yes** — it is not in CareAgents at all |
| Login-code attempts | `EmailToken.attempts`, a DB column (`careagents/models.py:145`) | **Yes** — read back as 2 through a fresh engine |
| Durable daily turn cap | `UsageDay` (`careagents/models.py:117`) | **Yes** — counted 4 across the restart |
| Chat *burst* limiter | in-process `turns` deque (`careagents/app.py:167`) | **No** — 429 before the restart, 200 after |

Only the last one is lost, and `careagents/config.py` already documents it as
deliberate:

> The burst limiter above is in-process, so it resets on restart and multiplies
> by gunicorn worker count; this one is DB-backed and is what actually bounds
> what a single account can cost the operator in a day.

**"Every chat history ... wiped" is false.** CareAgents stores no chat history
to lose — that is the published invariant (`docs/agent-task-guide.md:55`,
"CareAgents and SmartHealthConnect store **no PHI**") working as designed.
Restarting CareAgents cannot wipe a transcript any more than restarting a
browser can.

## 7. Item 5 — measured effect of each proposed mitigation

```
### Item 5 — measured effect of each mitigation ###

(a) bound the turn: same N, shorter run
  [2w x 4t (8 threads)] N=8   started=4/8   healthz(5s) 3/4 healthz(30s) 1/1 true wait 0.01s
  [2w x 4t (8 threads)] N=16  started=8/16  healthz(5s) 3/4 healthz(30s) 1/1 true wait 13.73s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8     5s           4/8  3/4 ok, max 5.00s  1/1 ok, max 8.49s      0.01s      15.1
 16     5s          8/16  3/4 ok, max 5.00s  1/1 ok, max 3.24s     13.73s      21.1
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

(b) drop the HealthClaw read timeout 25s -> 10s
  b1. /healthz's own call, no chat load:
  HealthClaw hangs, no chat load, client read timeout 25s (shipping default): /healthz took [25.01, 25.01] -> [503, 503]
----------------------------------------
Exception occurred during processing of request from ('127.0.0.1', 51180)
Traceback (most recent call last):
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 691, in process_request_thread
    self.finish_request(request, client_address)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 361, in finish_request
    self.RequestHandlerClass(request, client_address, self)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 755, in __init__
    self.handle()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 440, in handle
    self.handle_one_request()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 428, in handle_one_request
    method()
  File "/Users/<user>/Git/HealthClawGuardrails/.claude/worktrees/agent-a078e2964ba2642fb/scripts/probe_219_thread_saturation.py", line 155, in do_GET
    return self._reply(200, {"available": True, "workers": 1})
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/<user>/Git/HealthClawGuardrails/.claude/worktrees/agent-a078e2964ba2642fb/scripts/probe_219_thread_saturation.py", line 115, in _reply
    self.wfile.write(raw)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 834, in write
    self._sock.sendall(b)
BrokenPipeError: [Errno 32] Broken pipe
----------------------------------------
  HealthClaw hangs, no chat load, client read timeout 10s (proposed): /healthz took [10.01, 10.01] -> [503, 503]
```

| Mitigation | Measured effect | Verdict |
|---|---|---|
| **Bound the turn** | Shortening the turn 20s → 5s dropped the `/healthz` wait from **18.19s → 0.01s** at N=8, and **38.32s → 13.73s** at N=16. The starvation window tracks the turn length | **The lever that moves the number** — but the bound already exists (`run_deadline_seconds` = 120). The change is lowering it, which trades a hung page for a cut-off answer |
| **Read timeout 25s → 10s** | **Nothing while HealthClaw is healthy** — those calls return in milliseconds, so the saturation tables do not move. It binds only when HealthClaw *hangs*, and then in two places: `/healthz`'s own call (25.01s → 10.01s) and the thread hold itself, since `_stream_run` has no `except` and the timeout is what ends the generator | Real but narrow: it converts a 25s hang into a 10s hang. It does not help the healthy-but-busy case, which is the one #219 describes |
| **Raise threads (2×8 = 16)** | **Fixes N=8 outright** (18.19s → **0.00s**, 5s budget 3/4 → 4/4 ok). At N=16 it halves the wait (38.32s → **18.23s**) and starts all 16 turns instead of 8, but `/healthz` still blows the 5s budget 3 times in 4 | Buys roughly one doubling of headroom and no more. The failure moves from 8 concurrent to 16, it does not go away |

```
(c) raise threads: 2w x 8t (16 threads)
----------------------------------------
Exception occurred during processing of request from ('127.0.0.1', 51824)
Traceback (most recent call last):
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 691, in process_request_thread
    self.finish_request(request, client_address)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 361, in finish_request
    self.RequestHandlerClass(request, client_address, self)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 755, in __init__
    self.handle()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 442, in handle
    self.handle_one_request()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 408, in handle_one_request
    self.raw_requestline = self.rfile.readline(65537)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socket.py", line 718, in readinto
    return self._sock.recv_into(b)
           ^^^^^^^^^^^^^^^^^^^^^^^
ConnectionResetError: [Errno 54] Connection reset by peer
----------------------------------------
  [2w x 8t (16 threads)] N=8   started=8/8   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 8t (16 threads)] N=16  started=16/16  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.23s

/healthz under N concurrent chat turns — 2w x 8t (16 threads)
-------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           8/8  4/4 ok, max 0.00s  1/1 ok, max 0.00s      0.00s     222.3
 16    20s         16/16  1/4 ok, max 5.00s 1/1 ok, max 18.23s     18.23s      61.8
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
```

### 7b. The read timeout under load, measured

```
  b2. under load — HealthClaw hangs on run events, so the read timeout IS the thread hold (N=8):
  [2w x 4t (8 threads)] N=8   started=8/8   healthz(5s) 0/4 healthz(30s) 1/1 true wait 23.03s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           8/8  0/4 ok, max 5.00s 1/1 ok, max 23.04s     23.03s       0.3
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
  [2w x 4t (8 threads)] N=8   started=4/8   healthz(5s) 2/4 healthz(30s) 1/1 true wait 18.07s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           4/8  2/4 ok, max 5.00s  1/1 ok, max 0.01s     18.07s       0.4
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
```

With HealthClaw hanging on the run-events endpoint, the read timeout *is* the
thread hold: `_stream_run` has no `except`, so the client giving up is what
ends the generator and releases the thread. At the shipping 25s default that
shows cleanly — all 8 turns took threads and `/healthz` waited **23.03s**,
which is the read timeout, not the 20s run length.

**The 10s comparison is not clean, and should not be quoted as one.** That
sample recorded 18.07s, *higher* than one would expect from a 10s timeout,
because only 4 of the 8 turns got threads in the first wave and a second wave
followed — the same uneven-distribution effect as §3, not a property of the
timeout. One sample each is enough to demonstrate the mechanism and not enough
to size the improvement. What can be said: the read timeout does bound the
thread hold when HealthClaw hangs, and this is a HealthClaw-outage scenario,
not the concurrency one #219 describes.

One defect visible here and not in the issue: `_stream_run` has no `try/except`
around `hc.agent_run_events`, so a `HealthClawError` mid-stream kills the
generator and the browser sees the connection drop with **no `error` event** —
the chat just stops. Small, separate, worth its own issue.

(The `ConnectionResetError` traceback in the (c) block above is the *fake*
HealthClaw logging a client that disconnected mid-request when its gunicorn
thread was torn down. Rig noise, not a finding — left in rather than stripped,
so the transcript is what the script actually printed.)

## 8. Finding not in the issue: `/healthz` fails its own probe budget at zero load

`/healthz` calls `_worker_state()` (`careagents/app.py:1377` → `:814`), which
makes an HTTP call to HealthClaw with the client's 25s timeout. With **no chat
load at all**, a slow HealthClaw makes `/healthz` slow:

```
HealthClaw hangs, no chat load, client read timeout 25s (shipping default): /healthz took [25.01, 25.01] -> [503, 503]
HealthClaw hangs, no chat load, client read timeout 10s (proposed):         /healthz took [10.01, 10.01] -> [503, 503]
```

It answers correctly (503, degraded) — it just takes 25 seconds to say so, and
the Dockerfile's `--timeout=5s` cannot pass. The endpoint's own docstring
already warns about this shape:

> Callers that only need "is this process up" (a boot gate, a restart probe)
> must not use this endpoint — it answers for the whole system, including
> dependencies it does not control.

Nothing currently acts on that failure (§5), so it is latent — but it is the
part of #219 that a Docker-native host, or a future Railway that adds runtime
probes, would actually trip. **It is triggered by HealthClaw being slow, not by
concurrency**, which is the opposite of the issue's causal story.

## 9. Finding not in the issue: the poll storm

Each open chat turn polls HealthClaw's run-events endpoint every 0.25s. The
tables' `HC req/s` column is measured against the fake: **~31 requests/second
at N=8**, ~62 at N=16 with 16 threads. Those requests go to the HealthClaw
instance that also serves the clinician. That is the more plausible way
CareAgents concurrency causes harm outside itself, and the issue does not
mention it. Not chased here — flagging only.

## 10. What could NOT be measured locally

Stated plainly, because a silent gap is how this gets re-litigated:

1. **Whether Railway sets `CARE_WEB_WORKERS` for the CareAgents service, and to
   what.** The Dockerfile defaults to 2; the actual value is a dashboard
   variable. Both 2×4 and 1×8 were measured to cover it; a larger value was
   not.
2. **The CareAgents Railway service's own healthcheck settings.** The repo's
   `railway.toml` is HealthClaw's (`healthcheckPath = "/r6/fhir/health"`).
   CareAgents deploys by `railway up` from a staged directory, so its
   `healthcheckPath` / `healthcheckTimeout` live in the dashboard and are not
   in this repo. §5's conclusion rests on Railway's documented platform
   behaviour, which is settings-independent — but the specific values were not
   read.
3. **Real HealthClaw latency under the poll storm.** The fake answers
   instantly. What 31 req/s does to a real HealthClaw was not measured, and
   measuring it properly needs a full local HealthClaw plus a run worker, or
   production — which is out of bounds.
4. **The real distribution of turn durations in production.** The hold was
   controlled, not observed. Whether real turns cluster at 3s or at the 120s
   deadline decides how often N=6 is even reached, and that number is only
   available from production telemetry.
5. **The "turns started" column is noisy, and was not diagnosed.** It counts
   turns whose `accepted` event arrived inside a 2s settle window, and it moves
   around in ways the thread count alone does not explain (4/8 at a 5s hold
   versus 7/8 at a 20s hold, same N). SQLite is one likely contributor — the
   rig uses it where production uses Postgres, and the `claim_daily_turn` write
   at the start of every turn serialises — but that was not confirmed. Read the
   column as indicative only. It does not touch the starvation numbers: the
   thread hold is the SSE loop, which never reads the account store.
6. **Railway's edge proxy behaviour on a 150s SSE stream.** If the edge cuts
   the stream earlier than the app does, the reconnect path runs more often
   than modelled here.
7. **Whether any of this has happened.** This measures a mechanism, not an
   incident rate. No production logs were consulted.

## 11. Recommendation

Not a fix — a re-scoping, for the CTO and founder to rule on:

- **Close the restart-loop and data-wipe halves of #219 as not reproducible**
  (§5, §6). They rest on a platform behaviour Railway does not have and on
  state CareAgents does not hold.
- **Keep the starvation half, re-titled**, with the corrected trigger: 6
  concurrent turns on the shipping 2×4 config, up to 118s of every route
  blocked, no restart. Its user-visible cost is a hung page for whoever is not
  already in a turn, and a deploy that will not go live while it lasts.
- **Of the three mitigations listed, only two move the measured number**, and
  neither removes the failure: lowering `run_deadline_seconds` scales the
  starvation window 1:1, and more threads raises the concurrency at which it
  starts. Dropping the read timeout does not address the case the issue
  describes. *(Observation, outside what was asked: the thread is held only to
  poll on the browser's behalf, and the client already reconnects by design —
  so the hold is structural rather than necessary. Sizing that is Dev's call,
  not this probe's.)*
- **File `/healthz`'s 25s dependency call separately** (§8). It is a one-line
  shape, it is real today, and it has nothing to do with concurrency.

## Appendix — full run transcript

```
==============================================================================
probe #219 — /healthz under concurrent chat turns
==============================================================================
repo HEAD      : 89b42fb
python         : 3.11.15
fake HealthClaw: http://127.0.0.1:63715
hold per turn  : 20.0s     concurrency levels: [0, 4, 6, 8, 10, 16]
scratch        : /var/folders/tp/f0jrwq392932r3h4x6b95n9c0000gn/T/probe219-jxep821s
seeded         : 16 accounts (1 per virtual user)

### Item 1 — does /healthz starve? ###
  [2w x 4t (8 threads)] N=0   started=0/0   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [2w x 4t (8 threads)] N=4   started=4/4   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 4t (8 threads)] N=6   started=6/6   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 4t (8 threads)] N=8   started=7/8   healthz(5s) 3/4 healthz(30s) 1/1 true wait 18.19s
  [2w x 4t (8 threads)] N=10  started=8/10  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.13s
  [2w x 4t (8 threads)] N=16  started=8/16  healthz(5s) 0/4 healthz(30s) 0/1 true wait 38.32s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  0    20s           0/0  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.01s       0.0
  4    20s           4/4  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.00s     109.9
  6    20s           6/6  4/4 ok, max 0.01s  1/1 ok, max 0.00s      0.00s     165.3
  8    20s           7/8  3/4 ok, max 5.00s 1/1 ok, max 18.19s     18.19s      30.9
 10    20s          8/10  1/4 ok, max 5.00s 1/1 ok, max 18.12s     18.13s      38.4
 16    20s          8/16  0/4 ok, max 5.00s 0/1 ok, max 30.00s     38.32s      31.2
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
  [1w x 8t (8 threads)] N=0   started=0/0   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=4   started=4/4   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=6   started=6/6   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.01s
  [1w x 8t (8 threads)] N=8   started=8/8   healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.18s
  [1w x 8t (8 threads)] N=10  started=8/10  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.28s
  [1w x 8t (8 threads)] N=16  started=8/16  healthz(5s) 0/4 healthz(30s) 0/1 true wait 38.49s

/healthz under N concurrent chat turns — 1w x 8t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  0    20s           0/0  4/4 ok, max 0.01s  1/1 ok, max 0.01s      0.01s       0.0
  4    20s           4/4  4/4 ok, max 0.00s  1/1 ok, max 0.00s      0.01s     110.3
  6    20s           6/6  4/4 ok, max 0.00s  1/1 ok, max 0.01s      0.01s     165.5
  8    20s           8/8  1/4 ok, max 5.00s 1/1 ok, max 18.18s     18.18s      31.0
 10    20s          8/10  1/4 ok, max 5.00s 1/1 ok, max 18.27s     18.28s      37.6
 16    20s          8/16  0/4 ok, max 5.00s 0/1 ok, max 30.00s     38.49s      30.8
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

### Item 2 — real per-turn thread hold ###

Per-turn web-thread hold (one turn, 2w x 4t)
--------------------------------------------
 run duration   thread held   accepted after   poll reqs  events
           2s         2.09s           0.018s           9  accepted,text,done
          10s        10.13s           0.025s          40  accepted,text,done
          30s        30.02s           0.007s         116  accepted,text,done

### Worst case — 8 turns each holding 120s (the run deadline) ###
  [2w x 4t (8 threads)] N=8   started=8/8   healthz(5s) 0/4 healthz(30s) 0/1 true wait 118.12s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8   120s           8/8  0/4 ok, max 5.00s 0/1 ok, max 30.00s    118.12s      30.7
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

### Why 2w x 4t starves below 8 — N=6, repeated ###
  arrivals in the same millisecond (burst):
  run 1/6: /healthz true wait 18.11s  (5s budget 3/4 ok)
  run 2/6: /healthz true wait 18.13s  (5s budget 1/4 ok)
  run 3/6: /healthz true wait 0.01s  (5s budget 1/4 ok)
  run 4/6: /healthz true wait 18.27s  (5s budget 1/4 ok)
  run 5/6: /healthz true wait 0.01s  (5s budget 4/4 ok)
  run 6/6: /healthz true wait 18.21s  (5s budget 3/4 ok)

  N=6 on 2w x 4t, 6 runs: /healthz stalled in 4/6. Waits: [18.11, 18.13, 0.01, 18.27, 0.01, 18.21]

  arrivals spread 0.3s apart (closer to real traffic):
  run 1/6: /healthz true wait 17.00s  (5s budget 1/4 ok)
  run 2/6: /healthz true wait 0.01s  (5s budget 4/4 ok)
  run 3/6: /healthz true wait 0.01s  (5s budget 1/4 ok)
  run 4/6: /healthz true wait 16.99s  (5s budget 1/4 ok)
  run 5/6: /healthz true wait 16.97s  (5s budget 2/4 ok)
  run 6/6: /healthz true wait 16.97s  (5s budget 1/4 ok)

  N=6 on 2w x 4t, 6 runs: /healthz stalled in 4/6. Waits: [17.0, 0.01, 0.01, 16.99, 16.97, 16.97]

### Item 4 — what a restart actually loses ###
DEV email — Verify your email for restart-probe-1788474575@example.invalid: 89021758
  burst limiter (CARE_CHAT_TURNS=3), 4 turns before restart: [200, 200, 200, 429]
  first turn AFTER restart: 200 data: {"type": "accepted", "run_id": "run-4", "next_cursor": 0}

id: 1
data: {"t
  durable daily counter (UsageDay.turns) across the restart: [4]
  login-code attempts read back through a new engine: 2
  chat turns persisted to HealthClaw, not CareAgents memory: 3 POSTs to /command-center/api/conversations

### Item 5 — measured effect of each mitigation ###

(a) bound the turn: same N, shorter run
  [2w x 4t (8 threads)] N=8   started=4/8   healthz(5s) 3/4 healthz(30s) 1/1 true wait 0.01s
  [2w x 4t (8 threads)] N=16  started=8/16  healthz(5s) 3/4 healthz(30s) 1/1 true wait 13.73s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8     5s           4/8  3/4 ok, max 5.00s  1/1 ok, max 8.49s      0.01s      15.1
 16     5s          8/16  3/4 ok, max 5.00s  1/1 ok, max 3.24s     13.73s      21.1
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

(b) drop the HealthClaw read timeout 25s -> 10s
  b1. /healthz's own call, no chat load:
  HealthClaw hangs, no chat load, client read timeout 25s (shipping default): /healthz took [25.01, 25.01] -> [503, 503]
----------------------------------------
Exception occurred during processing of request from ('127.0.0.1', 51180)
Traceback (most recent call last):
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 691, in process_request_thread
    self.finish_request(request, client_address)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 361, in finish_request
    self.RequestHandlerClass(request, client_address, self)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 755, in __init__
    self.handle()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 440, in handle
    self.handle_one_request()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 428, in handle_one_request
    method()
  File "/Users/<user>/Git/HealthClawGuardrails/.claude/worktrees/agent-a078e2964ba2642fb/scripts/probe_219_thread_saturation.py", line 155, in do_GET
    return self._reply(200, {"available": True, "workers": 1})
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/<user>/Git/HealthClawGuardrails/.claude/worktrees/agent-a078e2964ba2642fb/scripts/probe_219_thread_saturation.py", line 115, in _reply
    self.wfile.write(raw)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 834, in write
    self._sock.sendall(b)
BrokenPipeError: [Errno 32] Broken pipe
----------------------------------------
  HealthClaw hangs, no chat load, client read timeout 10s (proposed): /healthz took [10.01, 10.01] -> [503, 503]

  b2. under load — HealthClaw hangs on run events, so the read timeout IS the thread hold (N=8):
  [2w x 4t (8 threads)] N=8   started=8/8   healthz(5s) 0/4 healthz(30s) 1/1 true wait 23.03s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           8/8  0/4 ok, max 5.00s 1/1 ok, max 23.04s     23.03s       0.3
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.
  [2w x 4t (8 threads)] N=8   started=4/8   healthz(5s) 2/4 healthz(30s) 1/1 true wait 18.07s

/healthz under N concurrent chat turns — 2w x 4t (8 threads)
------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           4/8  2/4 ok, max 5.00s  1/1 ok, max 0.01s     18.07s       0.4
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

(c) raise threads: 2w x 8t (16 threads)
----------------------------------------
Exception occurred during processing of request from ('127.0.0.1', 51824)
Traceback (most recent call last):
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 691, in process_request_thread
    self.finish_request(request, client_address)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 361, in finish_request
    self.RequestHandlerClass(request, client_address, self)
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socketserver.py", line 755, in __init__
    self.handle()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 442, in handle
    self.handle_one_request()
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/http/server.py", line 408, in handle_one_request
    self.raw_requestline = self.rfile.readline(65537)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/<user>/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/lib/python3.11/socket.py", line 718, in readinto
    return self._sock.recv_into(b)
           ^^^^^^^^^^^^^^^^^^^^^^^
ConnectionResetError: [Errno 54] Connection reset by peer
----------------------------------------
  [2w x 8t (16 threads)] N=8   started=8/8   healthz(5s) 4/4 healthz(30s) 1/1 true wait 0.00s
  [2w x 8t (16 threads)] N=16  started=16/16  healthz(5s) 1/4 healthz(30s) 1/1 true wait 18.23s

/healthz under N concurrent chat turns — 2w x 8t (16 threads)
-------------------------------------------------------------
  N   hold turns started  healthz 5s budget healthz 30s budget  true wait  HC req/s
-----------------------------------------------------------------------------------
  8    20s           8/8  4/4 ok, max 0.00s  1/1 ok, max 0.00s      0.00s     222.3
 16    20s         16/16  1/4 ok, max 5.00s 1/1 ok, max 18.23s     18.23s      61.8
  'turns started' = turns that got their `accepted` event within the 2s settle;
  'true wait'     = /healthz latency measured with a budget large enough to record it.

machine-readable report: /var/folders/tp/f0jrwq392932r3h4x6b95n9c0000gn/T/probe219-jxep821s/probe-219-report.json
```
