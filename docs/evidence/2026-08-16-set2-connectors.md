# Feature set 2 — upstream connectors: Wave-1 evidence

**Owner:** owner-connectors · **Date:** 2026-08-16 · **Verdict: EVIDENCE PARTIAL**

> **2026-09-04 (#530):** §3 and §4 — the two live runs, and the "2 of 4" claim
> the process documents carry — were **re-run by someone other than this pack's
> author and reproduced**, against the same two public servers, from a script
> now committed as `scripts/walkthrough-upstream.sh`. One line differs and is
> better: `$conformance` against HAPI graded **B 6/7**, not the F 1/7 in §3,
> because #514 fixed the probe collision §3 diagnosed. See
> `docs/evidence/2026-09-04-set2-connectors-rerun.md`. Sections 5, 6 and 7 have
> **not** been independently re-run — their scripts are still uncommitted and
> the scratch directory is gone. Nothing below is rewritten; 2026-08-16's
> findings stand as 2026-08-16's findings.

**One redaction:** the operator's home-directory name in pasted shell output
reads `<user>`. Nothing else in any transcript below is altered — the redaction
is incidental to every claim the output supports.

**Dated note (added 2026-09-04, #615):** redaction behaviour changed after
this pack was written — identifier values are now removed, not truncated to
their last four characters. The `***XXXX` values in the transcripts below
show the shape that was true on 2026-08-16, not current behaviour.

Two of the four connector kinds ran a full live walkthrough against a real
server of that kind. Two did not run at all, because the servers they need
were not running on this machine and starting them was out of scope for this
pass.

The claim under test was the registry's: four connector kinds — `aidbox`,
`medplum`, `hapi`, `generic` — resolved by `r6/upstream_connectors.py`. Before
today two of them (`hapi`, `generic`) had never been run against a live server
of any kind. After today they have, and the two that had been are the two that
have not been re-run.

## The premise changed under the task

The task named four live services on this machine, verified with `docker ps`:
Aidbox on 8080, Medplum on 8103 and 3000, the guardrails app on 5099. None of
them was running when this pass started, and the Docker daemon itself was down.

```
$ docker context ls
NAME              DOCKER ENDPOINT                                      ERROR
default           unix:///var/run/docker.sock
desktop-linux *   unix:///Users/<user>/.docker/run/docker.sock

$ docker info --format '{{.ServerVersion}}'
failed to connect to the docker API at unix:///Users/<user>/.docker/run/docker.sock;
check if the path is correct and if the daemon is running:
dial unix /Users/<user>/.docker/run/docker.sock: connect: no such file or directory

$ ls -la ~/.docker/run          # the socket file itself is gone
total 0
drwxr-xr-x@  2 <user>  staff   64 Aug 16 17:16 .

$ pgrep -fl 'com.docker.backend|Docker Desktop|dockerd'
(no output)

$ for u in 8080/health 8103/healthcheck 3000/ 5099/r6/fhir/health; do ... done
http://localhost:8080/health               000
http://localhost:8103/healthcheck          000
http://localhost:3000/                     000
http://localhost:5099/r6/fhir/health       000
```

`lsof -nP -iTCP -sTCP:LISTEN` showed 30 listeners, none of them on those ports.
`ControlCe *:5000` was among them, which is the AirPlay Receiver the example's
own README warns about.

The instruction was not to start, stop, rebuild, or deploy anything, so the
daemon was left down and Aidbox and Medplum were not started. Egress works
(`hapi.fhir.org` answered 200 in 0.87s), so the two kinds that could be proven
from here were proven from here.

**What the live runs below actually exercised:** the source tree at
`2b7872d`, run as `uv run python main.py` against public FHIR servers, with a
throwaway SQLite database in a scratch directory. It reports itself as version
`1.10.0`, the same version as the images the example pins, but it is not the
pinned image. No container was involved in any run below.

---

## 1. `aidbox` — NOT RUN (server absent)

Run as instructed, against the live stack, exactly as the task specified.

```
$ HEALTHCLAW_PORT=5099 bash scripts/walkthrough.sh

0. Preflight

The guardrail proxy is not answering on http://localhost:5099.
Is it up?  docker compose ps
On macOS, port 5000 belongs to AirPlay Receiver — set HEALTHCLAW_PORT=5099.
(exit 2)
```

It has 6 steps. **Step 0 (Preflight) executed and refused to continue. Steps 1
through 5 did not run.** Nothing about the Aidbox connector was observed today.

This is the script behaving correctly. It dies at the preflight rather than
reporting on steps it never took, which is the property the walkthrough exists
to have. There is no partial credit to claim here and none is claimed.

Also missing: the example has no `.env` (`ls -la` shows `.env.example` only),
so `STEP_UP_SECRET` and `MCP_AUTH_TOKEN` are unset and `docker compose up`
would refuse on both — they are `${VAR:?...}` in the compose file.

## 2. `medplum` — NOT RUN as a connector, and the QA does not notice

The runbook is `docs/runbooks/medplum-self-host-qa.md`. Its four steps were
attempted in order.

**Step 1 — is the self-host up?** No.

```
  GET http://localhost:8103/healthcheck -> HTTP 000 (connection refused)
```

**Step 2 — bootstrap a project and a client.** Cannot run: it needs a running
Medplum. Stating this plainly rather than skipping it: **`MEDPLUM_CLIENT_ID`
and `MEDPLUM_CLIENT_SECRET` are not set in this environment and are recorded
nowhere in the repository.** `env | grep -i medplum` returns nothing; the seven
files that mention the names (`INTEGRATION.md`, `README.md`, `.env.example`,
`docs/upstream-connectors.md`, the runbook, the recipe, and a skill) reference
the variable names, not values. A Medplum connector run needs a Medplum
bootstrap first, and that needs a Medplum.

**Steps 3 and 4 — run anyway, without the credential.** This is the part worth
reading. Steps 3 and 4 were run with the runbook's exact environment
(`MEDPLUM_BASE_URL` set, `MEDPLUM_CLIENT_ID`/`_SECRET` absent, `PORT`,
`STEP_UP_SECRET=<set>`, `APP_ENV=development` — the runbook does not set
`READ_AUTH_ENABLED`), with **no Medplum in existence at that address**.

```
--- runbook step 3: point HealthClaw at it (credential absent) ---
  GET /r6/fhir/health ->
    {
        "checks": {
            "database": "ok",
            "upstream": "not_configured"
        },
        "fhirVersion": "6.0.0-ballot3",
        "mode": "upstream",
        "status": "healthy",
        "version": "1.10.0"
    }
  the runbook says to expect: "software": "medplum", "status": "connected"

--- runbook step 4: run the QA ---
    [PASS] write blocked without step-up (401) — got 401
    [PASS] guardrailed create -> Medplum (201) — id=042b613b-b57e-441e-a69d-3b7c35e83af3
    [PASS] read returns 200 — status 200
    [PASS] name redacted (initial only) — family='T.'
    [PASS] SSN masked in read
    [PASS] phone redacted in read
    [FAIL] Medplum-sourced
    [PASS] access audited (AuditEvent present)

  7/8 guardrail checks passed.
  (smoke_medplum.py exit: 1)
```

**The Medplum QA reports 7 of 8 checks passing against a Medplum that does not
exist.** The check named `guardrailed create -> Medplum (201)` passed, with an
id, having written to the proxy's own SQLite file. One check — `Medplum-sourced`,
which asserts `_source == "upstream"` — is the entire distance between this
output and a false pass. See register entries R1 and R2.

For completeness, the same run with `READ_AUTH_ENABLED=true` added scores 4/8,
and two of its four passes are vacuous — `SSN masked in read` and `phone
redacted in read` both passed on the body of a 401:

```
    [FAIL] read returns 200 — status 401
    [FAIL] name redacted (initial only) — family=''
    [PASS] SSN masked in read
    [PASS] phone redacted in read
```

That is the exact trap the Aidbox walkthrough was fixed for and `smoke_medplum.py`
was not. Register entry R3.

## 3. `hapi` — PROVEN LIVE against hapi.fhir.org

No `hapi` walkthrough script exists in the repo. The Aidbox walkthrough was
re-pointed, keeping its assertions and their order, with the differences from
the Aidbox script marked in the source and reported rather than softened. The
script is not committed — see register entry R8.

Upstream: `https://hapi.fhir.org/baseR4`, HAPI FHIR Server 8.11.16-SNAPSHOT,
FHIR 4.0.1. Configuration: `FHIR_UPSTREAM_KIND=hapi`, `READ_AUTH_ENABLED=true`,
`STEP_UP_SECRET=<set>`. Synthetic data only.

```
0. Preflight
    proxy version 1.10.0, mode upstream
    upstream: kind=hapi software='HAPI FHIR Server' fhirVersion=4.0.1 status=connected
  PASS proxy is in upstream mode, connected, and reports the kind it was built as
  NOTE the upstream serves ANONYMOUS callers (HTTP 200). Expected for a
       public sandbox (connector auth=none). The "proxy holds its own
       credential" property is NOT demonstrated by this connector kind.

1a. A subject to write about (synthetic)
  PASS guardrailed create -> upstream (Patient/137354718)
  PASS the write reached the upstream (asked https://hapi.fhir.org/baseR4 directly)

1b. A CLINICAL write, and two gates that do not substitute for each other
    neither gate               -> HTTP 428
  PASS neither gate
    confirmed, no credential   -> HTTP 401
  PASS confirmed, no credential
    credential, no human       -> HTTP 428
  PASS credential, no human
    both gates satisfied       -> HTTP 201
  PASS both gates satisfied
  PASS the Observation reached the upstream (1 found)

2. The same resource, both ways
  through the guardrail proxy:
    {"resourceType": "Patient", "id": "137354718", "name": [{"family": "Z.", "given": ["E."]}],
     "identifier": [{"system": "urn:set2:evidence", "value": "***5409"}], "birthDate": "1980",
     "telecom": [{"system": "phone", "value": "[Redacted]"}], "_source": "upstream"}
  PASS the upstream holds the full record; the agent's path does not

3. The read left a record
    AuditEvent entries: 5
  PASS audit written, and PHI-free

4. Grade the deployment
    grade F (1/7)
    failing properties: ['audit_trail', 'step_up_enforcement', 'human_in_the_loop',
                         'tenant_isolation', 'medical_disclaimer', 'error_fidelity']
  FAIL properties that should hold did not: audit_trail, human_in_the_loop,
       medical_disclaimer, step_up_enforcement, tenant_isolation

5. What the agent actually connects to
    tools/list, no token      -> HTTP 000
  FAIL expected 401 from an unauthenticated tools/list, got 000 (NOT RUNNING = not checked)
```

Every guardrail the walkthrough asserts held against a live HAPI: the four-way
write gate matrix, redaction on the read path, the write landing upstream, and
a PHI-free audit trail. The `_source: "upstream"` field confirms the record came
from HAPI rather than the local store.

Two failures, neither of which is a guardrail failure:

**Step 5 is not a failure, it is an absence.** The MCP server runs in Docker,
Docker is down. Reported as red because nothing ran, which is the correct
report.

> **Count corrected 2026-09-04 (#605).** The sentence below read "Four of the
> six failing properties report a gate that is working as broken" from
> 2026-08-16 until today, and register entry R4 said the same. **No count in
> the transcript beneath it yields four.** The harness names six failing
> properties; the walkthrough asserts on five, excluding `error_fidelity` (the
> known #498 failure, which fails against Firely too); five FAIL blocks are
> printed; and **two** of them carry `on_failure` text blaming a gate. This
> was wrong on the day rather than overtaken since. The finding it supports is
> unchanged and slightly stronger — five of the six failures were the
> collision, not the guardrails. The transcript below is not edited.

**Step 4's Grade F is caused by the harness, not the guardrails**, and the
mechanism was traced to the end rather than assumed. Five of the six failing
properties are this collision rather than a defect, and two of them report a
gate that is working as broken in so many words:

```
### FAIL step_up_enforcement
    ok | write without step-up token is rejected (401)          | status 401
    ok | write with a forged step-up token is rejected (401)     | status 401
    NO | write carrying a valid step-up token is accepted        | status 412
       | on_failure: the gate refuses authorized writes too, so its 401s prove nothing

### FAIL human_in_the_loop
    ok | clinical write without human confirmation is blocked (428) | status 428
    NO | confirmed clinical write is accepted                       | status 400
       | on_failure: the gate blocks confirmed writes too, so its 428s prove nothing

### FAIL tenant_isolation
    NO | synthetic patient created

### FAIL medical_disclaimer
    NO | synthetic observation created | create returned 400

### FAIL audit_trail
    ok | AuditEvent endpoint readable | status 200
    NO | resource READ is recorded in the audit trail
    ok | no raw SSN in the audit trail
```

The cause is `_synthetic_patient()` in `r6/conformance/probes.py`, which returns
a byte-identical body on every call, and hapi.fhir.org runs a duplicate-detection
interceptor. Reproduced directly, outside the proxy, with the harness's own body:

```
$ curl -X POST -H 'Content-Type: application/fhir+json' \
       --data @<probes._synthetic_patient()> https://hapi.fhir.org/baseR4/Patient
HTTP=412
diagnostics: HAPI-2840: Can not create resource duplicating existing resource: Patient/137354720
```

The first probe (`phi_redaction`) creates it and passes 8/8. Every later probe
that calls `_create_synthetic` gets 412, falls back to the dangling
`Patient/conformance-subject` reference, and HAPI refuses the dependent
Observation with 400. The `on_failure` strings then attribute the failure to the
gate. Register entry R4.

The same harness against Firely (below) grades B 6/7, which is the control that
confirms this diagnosis.

## 4. `generic` — PROVEN LIVE against Firely Server

Deliberately not a HAPI server, so `generic` is not one kind standing in for
another. Upstream: `https://server.fire.ly/R4`, Firely Server 6.9.1, FHIR 4.0.1.
Configuration: `FHIR_UPSTREAM_KIND=generic`, no credentials (so the anonymous
branch of `generic`), `READ_AUTH_ENABLED=true`.

```
0. Preflight
    proxy version 1.10.0, mode upstream
    upstream: kind=generic software='Firely Server' fhirVersion=4.0.1 status=connected
  PASS proxy is in upstream mode, connected, and reports the kind it was built as
  NOTE the upstream serves ANONYMOUS callers (HTTP 200). Expected for a
       public sandbox (connector auth=none). The "proxy holds its own
       credential" property is NOT demonstrated by this connector kind.

1a. A subject to write about (synthetic)
  PASS guardrailed create -> upstream (Patient/9896ab04-6d32-41b2-b960-5b842909c891)
  PASS the write reached the upstream (asked https://server.fire.ly/R4 directly)

1b. A CLINICAL write, and two gates that do not substitute for each other
    neither gate               -> HTTP 428
  PASS neither gate
    confirmed, no credential   -> HTTP 401
  PASS confirmed, no credential
    credential, no human       -> HTTP 428
  PASS credential, no human
    both gates satisfied       -> HTTP 201
  PASS both gates satisfied
  PASS the Observation reached the upstream (1 found)

2. The same resource, both ways
  through the guardrail proxy:
    {"resourceType": "Patient", "id": "9896ab04-...", "name": [{"family": "Z.", "given": ["E."]}],
     "identifier": [{"system": "urn:set2:evidence", "value": "***5539"}], "birthDate": "1980",
     "telecom": [{"system": "phone", "value": "[Redacted]"}], "_source": "upstream"}
  PASS the upstream holds the full record; the agent's path does not

3. The read left a record
    AuditEvent entries: 3
  PASS audit written, and PHI-free

4. Grade the deployment
    grade B (6/7)
    failing properties: ['error_fidelity']
  PASS 6/7 — only error fidelity fails (known, #498)

5. What the agent actually connects to
    tools/list, no token      -> HTTP 000
  FAIL expected 401 from an unauthenticated tools/list, got 000 (NOT RUNNING = not checked)
```

Grade B 6/7 with `error_fidelity` the only failure is exactly what the Aidbox
walkthrough documents for upstream mode, and #498 is the open issue for it.

**What this does not prove:** `generic`'s HTTP Basic branch. Firely's public
server takes no credential, so this run exercised `generic` with `basic_auth`
resolving to `None`. The Basic branch was proven separately on the wire — see
section 6 — but not end to end against a server that requires it.

## 5. Registry contract, asserted against a booted app

Not unit tests. Each case boots the real Flask app with a real environment and
asks `/r6/fhir/health` what it resolved.

**Case 1 — `FHIR_UPSTREAM_URL` takes precedence over `MEDPLUM_BASE_URL`. HOLDS.**

```
  FHIR_UPSTREAM_URL = https://server.fire.ly/R4
  MEDPLUM_BASE_URL  = https://hapi.fhir.org/baseR4   (must be IGNORED)
  MEDPLUM_CLIENT_ID / _SECRET = must-not-be-used

  resolved -> {"mode": "upstream", "kind": "generic",
               "upstream_url": "https://server.fire.ly/R4", "software": "Firely Server"}
  PASS FHIR_UPSTREAM_URL won; MEDPLUM_BASE_URL was not used
```

The `software` field is the Firely server naming itself in its own
CapabilityStatement, so this is the upstream confirming which server was
actually reached, not our configuration restating itself.

**Case 2 — an unknown kind raises rather than falling through. HOLDS, with a
caveat about where.**

```
  FHIR_UPSTREAM_KIND = totally-made-up
  /r6/fhir/health -> HTTP 500

  Traceback (most recent call last):
      raise ValueError(
  ValueError: FHIR_UPSTREAM_KIND='totally-made-up' is not one of
  ['aidbox', 'generic', 'hapi', 'medplum']. Refusing to guess: an unknown kind
  means an unknown auth style, and defaulting to anonymous would send
  unauthenticated requests at a record system.
```

The security property holds: it raises, and it does not default to `generic`
or to anonymous. The caveat is that the app **boots** and raises per request,
returning 500 on `/r6/fhir/health` rather than refusing to start. In the example's
compose that still fails closed, because the healthcheck is `urlopen()`, which
raises on any non-2xx. Register entry R6.

**Case 3 — `MEDPLUM_BASE_URL` with no credentials.** See register entry R1;
this is where the half-configured behaviour was found.

## 6. What the proxy actually sends, per kind

The `hapi` connector's published summary — the string `supported_connectors()`
returns to an operator asking what this build supports — reads:

> "HAPI FHIR. Public sandboxes take no credential; add
> FHIR_UPSTREAM_CLIENT_ID/_SECRET for one behind HTTP Basic."

Tested by pointing the proxy at a local HTTP server that records the
`Authorization` header it receives, with identical credentials set, changing
only the kind:

```
Identical env for both cases:
  FHIR_UPSTREAM_CLIENT_ID     = set2-probe-client
  FHIR_UPSTREAM_CLIENT_SECRET = <set>
Question: what Authorization header reaches the upstream?

  kind=hapi     -> Authorization: None (NO credential sent)
  kind=generic  -> Authorization: Basic <redacted>
```

**The `hapi` kind silently drops the credentials its own summary tells operators
to set.** Register entry R5 — the highest-severity finding in this pass.

## 7. Image pins

`examples/aidbox-healthclaw-guardrails/docker-compose.yaml` names four images.
Resolved today via the registry HTTP API (Docker was down, so not via `docker`):

| image | pinned? | digest today |
|---|---|---|
| `ghcr.io/aks129/healthclaw-guardrails:1.10.0` | version tag | `sha256:57b345e0c8f6a6bf88690084f57fe863bd02882ade0e2c7b70002baa4c0e225b` |
| `ghcr.io/aks129/healthclaw-mcp-server:1.10.0` | version tag | `sha256:d37c997ea15c73c715cdc2b90ea01aa6a49dc34cb6897913ce1691d8d012cd53` |
| `healthsamurai/aidboxone:edge` | **NO** — moving tag, `pull_policy: always` | `sha256:42e4e8e10d9d42b54bf3f4602b3f584e06acc70738cdcf280ce7634bdb5e58b3` |
| `postgres:18` | partial — floating patch | `sha256:06cad38a5d9f5d24b4d83d86def30795d5e4b757fedbf5281172b576dedcd941` |

**The question "do they still resolve to the digest they were pinned at" cannot
be answered, because no digest was ever recorded.** A repo-wide search for
`sha256:` in yaml/yml/md/txt returns one unrelated hit. A version tag is mutable
at the registry; nothing in the repo would detect a re-push. The digests above
are therefore recorded here as the **baseline for the next run**, not as a
comparison result.

Two supporting observations:

- `latest` currently resolves to the **same digest as `1.10.0`** for both ghcr
  images, so the drift the compose comment describes ("for a while it pointed at
  1.9.0") is not present today.
- Published tags for both images: `latest, v1.4.0, 1.5.0, 1.5, 1.6.0, 1.6,
  1.7.0, 1.7, 1.8.0, 1.8, 1.9.0, 1.9, 1.10.0`. `1.10.0` has no `1.10` alias,
  unlike every prior minor.

---

## The four-row table

| kind | proven live? | against what | evidence |
|---|---|---|---|
| `aidbox` | **NO** — not run | nothing; Aidbox was not running and Docker was down | §1. `walkthrough.sh` exited 2 at step 0 of 6; steps 1–5 never ran |
| `medplum` | **NO** — not run | nothing; Medplum was not running and no client credentials exist | §2. Runbook steps 1–2 blocked; steps 3–4 run without a Medplum score 7/8 |
| `hapi` | **YES** | hapi.fhir.org, HAPI FHIR Server 8.11.16-SNAPSHOT, FHIR 4.0.1 | §3. Gate matrix 428/401/428/201, write landed upstream, redaction and audit held. `$conformance` F for an upstream-specific reason (R4); MCP step not run |
| `generic` | **YES**, anonymous branch only | server.fire.ly, Firely Server 6.9.1, FHIR 4.0.1 | §4. Same walkthrough, all guardrails held, `$conformance` B 6/7 (only #498). Basic branch proven on the wire only (§6) |

Two of four. The two that were already believed to work are the two with no
evidence from today; the two that had never been run now have a full run each.

## Edge-case register

Ordered by severity. None of these were fixed — no production code was modified
in this pass.

**R1 — A half-configured upstream reports `mode: "upstream"` and `status:
"healthy"` while serving from the local store. No issue yet.**
With `MEDPLUM_BASE_URL` and `MEDPLUM_CLIENT_ID` set and `MEDPLUM_CLIENT_SECRET`
missing, `get_proxy()` correctly returns `None` (refusing to make anonymous
requests at a record system), but `is_proxy_enabled()` at `r6/routes.py:1980`
tests only whether the env var is *set*. Measured:

```
  GET /r6/fhir/health -> HTTP 200
  status = 'healthy' | mode = 'upstream' | checks.upstream = "not_configured"
  POST /r6/fhir/Patient -> id 66dcb4f4-e809-40af-a718-75c38f2ff45a
  GET  /r6/fhir/Patient/<id> -> _source = None
    r6_resources Patient rows = 1
```

Writes an operator believes are landing in Medplum land in the proxy's own
SQLite. In the example's compose that file is inside an ephemeral container.
The health endpoint returns 200 and `status: healthy`, so a container
healthcheck and any orchestrator probe both report the deployment fine. This is
the "a control that looks like one thing and quietly does two" shape from
`docs/2026-08-02-retro.md`. Suggested change (not made): `mode` should be
derived from `get_proxy()`, and `checks.upstream == "not_configured"` while an
upstream URL is set should degrade `status`.

**R2 — `smoke_medplum.py` reports 7/8 with no Medplum present. No issue yet.**
Measured in §2. The check named `guardrailed create -> Medplum (201)` passes
against the local store. Only `Medplum-sourced` (`_source == "upstream"`)
fails. Two consequences: the QA cannot be read as "the Medplum connector works"
without reading that one line, and a future edit that relaxes or reorders that
check turns the script into a full false pass. Suggested change (not made):
assert the upstream identity in a preflight — `/r6/fhir/health` reporting
`software: medplum` and `status: connected` — and refuse to run otherwise, the
way `walkthrough.sh` does.

**R3 — Two `smoke_medplum.py` checks pass vacuously on a refusal. No issue yet.**
With `READ_AUTH_ENABLED=true`, the read returns 401 and:

```
    [FAIL] read returns 200 — status 401
    [PASS] SSN masked in read
    [PASS] phone redacted in read
```

`SSN masked` and `phone redacted` test `token not in json.dumps(body)`, which is
true of an empty body. This is the defect the Aidbox walkthrough was fixed for
in #499 ("the redaction demo was asserting on a refusal") and the same shape the
runbook's own "What this run found" section describes for check 1. The positive
assertion exists (`read returns 200`) but does not gate the absence checks. Also: `read_hdr` carries no step-up token, so
the script cannot pass at all against a deployment with read auth on — which
production has.

**R4 — `$conformance` cannot be re-run against an upstream with duplicate
detection, and misattributes the failure to the guardrails. FIXED by #514;
re-measured 2026-09-04 (#601). Count corrected 2026-09-04 (#605): this entry
read "Four properties then fail with `on_failure` text naming the gate" —
**two** do.** The finding as written on 2026-08-16, with that one count
corrected in place:
Measured in §3. `_synthetic_patient()` in `r6/conformance/probes.py` returns a
constant body; HAPI answers the second and later creates with 412 `HAPI-2840`.
Five of the six graded properties then fail for that reason rather than a
defect, and **two** of them fail with `on_failure` text naming the gate ("the
gate refuses authorized writes too, so its 401s prove nothing") when the gate
was never reached. Grade F against a deployment whose guardrails were, in the
same session, observed to hold. The code comment on `_synthetic_observation` records
the same class of misattribution happening before against Aidbox (422, dangling
reference), so this is the second instance of one shape. Suggested change (not
made): give the synthetic identifier a per-run nonce — `uuid` is already
imported in that module. Related but not the same: #463 (closed) covered tenant
pollution by the same harness.

**R5 — The `hapi` connector silently drops the credentials its own summary tells
operators to set. No issue yet. Highest severity.**
Measured on the wire in §6. `CONNECTORS["hapi"]` is `auth=AUTH_NONE`, and
`UpstreamConfig.basic_auth` returns credentials only when `auth == AUTH_BASIC`,
so `FHIR_UPSTREAM_CLIENT_ID`/`_SECRET` are accepted, logged as configured, and
never sent. An operator following the registry's published guidance against a
HAPI behind HTTP Basic gets anonymous requests, a 401 from the upstream, and a
sanitized 502 that reads as the upstream's fault — the precise failure the
module docstring says the module exists to prevent ("a deployment that stops
authenticating does not fail loudly"). `generic` with identical env sends
`Basic`. Suggested change (not made): `hapi` becomes `AUTH_BASIC`, which already
degrades to anonymous when no credentials are set, so both sentences of its
summary become true and no working deployment changes.
**The unit tests pin the current behaviour**: `tests/test_upstream_connector_registry.py:83`
asserts `("hapi", AUTH_NONE)`. 25 tests pass and none of them asks what goes on
the wire. This is a pin that encodes the defect; per standing order 4 it has not
been edited.

**R6 — An unknown `FHIR_UPSTREAM_KIND` boots and 500s per request rather than
refusing to start. No issue yet. Low severity.**
Measured in §5 case 2. The security property holds (it raises; it does not
default to anonymous). Under compose it still fails closed, because that
healthcheck raises on non-2xx. Worth knowing that the symptom of a typo is a
running container returning 500 with the explanation only in the logs.

**R7 — The example's compose sets `DATABASE_URL`, which the application never
reads. No issue yet.**
`docker-compose.yaml:129` sets `DATABASE_URL: sqlite:////tmp/healthclaw.db`. The
app reads `SQLALCHEMY_DATABASE_URI` (`main.py:_database_uri`); a repo-wide grep
finds `DATABASE_URL` read only by `migrations/env.py` (Alembic). In development
the fallback is `sqlite:///mcp_server.db`, so the container writes to
`instance/mcp_server.db`, not to the path the compose file names. Harmless today
and misleading to anyone reasoning about where the example's data lives.

**R8 — There is no `hapi` or `generic` walkthrough in the repo. CLOSED
2026-09-04 by `scripts/walkthrough-upstream.sh` (#530).** Both kinds were
re-run from it by someone other than this pack's author and reproduced; see
`docs/evidence/2026-09-04-set2-connectors-rerun.md`. The finding as written on
2026-08-16 was:
The evidence in §3 and §4 was produced by a re-pointed copy of
`examples/aidbox-healthclaw-guardrails/scripts/walkthrough.sh` living in a
scratch directory, which means it is not runnable by anyone else and will not
run in CI. It carries two changes the Aidbox script does not need: a per-run
nonce in the created identifier (R4's cause), and the anonymous-caller check
reported as a NOTE rather than asserted, because a public sandbox is
`auth=none` by design and the "proxy holds its own credential" property cannot
be demonstrated by these two kinds at all.

**R9 — `healthsamurai/aidboxone:edge` is not pinned.** `pull_policy: always` on a
moving tag, in the same file whose comment explains why the guardrails image is
pinned. Digest recorded in §7. `postgres:18` floats at the patch level.

**R10 — `$conformance` responses are cached.** The payload carries
`"cached": true` with a `measured_at`, so a re-run after a configuration change
can return the previous grade. Noted because it affected reading the results
during this pass, not diagnosed further.

**R11 — Upstream error diagnostics are dropped, including actionable ones.**
HAPI's 412 carried `HAPI-2840: Can not create resource duplicating existing
resource: Patient/137354712`. The proxy returned a generic OperationOutcome
("The upstream FHIR server could not process the request.") and logged
`Upstream create Patient returned 412` — the status only, not the diagnostics.
The sanitization is deliberate and correct for the caller; the consequence is
that diagnosing this required reproducing the call outside the proxy. Worth a
decision about whether the upstream diagnostics belong in the server-side log.

**R12 — Confirmed, not new: the gate matrix is a property of clinical writes.**
The Aidbox walkthrough's 428/401/428/201 holds for `Observation`. For `Patient`
the same four requests return 401/401/201/(412 on a duplicate), because
`require_human_confirmation` fires on `CLINICAL_RESOURCE_TYPES` and `Consent`
only. This matches the documented rule ("clinical writes need human
confirmation") and is recorded because a demo that generalizes the matrix to
"writes" would be wrong. Measured in the first §3 run before the script was
corrected.

## What I did NOT check

- **The `aidbox` connector, in any respect.** No Aidbox ran. Nothing in the
  Aidbox path — the Basic credential, the AccessPolicy, the init bundle, the
  502-on-unactivated behaviour — was observed today.
- **The `medplum` connector, in any respect.** No Medplum ran and no client
  credentials exist here. In particular the two defects the runbook says this
  loop found — the derived token endpoint and the token-failure fallthrough —
  were not re-verified. `_inject_bearer`'s raise-rather-than-send behaviour was
  read, not executed.
- **`generic` with HTTP Basic against a server that requires it.** Proven only
  as far as the header leaving the proxy (§6).
- **The MCP server**, in every run. It runs in Docker. Step 5 is red in both
  live walkthroughs for that reason and for no other.
- **The pinned container images.** Every live run used the source tree at
  `2b7872d` via `uv run python main.py`. The images were queried for digests
  and never pulled or started, so nothing here says the 1.10.0 images behave as
  the source does.
- **Whether the pinned tags still resolve to their original digests**, which is
  unanswerable — no baseline was ever recorded (§7).
- **The `qa/demo.spec.ts` Playwright recording.** The second of the four
  artifacts — a recording produced BY the run that asserts — does not exist for
  this pass. It needs the compose stack.
- **Two of the four artifacts are missing**, which is why this is EVIDENCE
  PARTIAL: no recording, and no sign-offs (QA adversarial, end-user).
- **`uv run pytest -q` on the full suite and `uv run ruff check .`** were not
  run. No production code was modified and nothing is being pushed;
  `tests/test_upstream_connector_registry.py` was run on its own (25 passed) and
  is cited in R5 only as evidence of what the tests pin.
- **Cleanup of synthetic resources created on public test servers.** Two
  Patients and one Observation on hapi.fhir.org, one Patient and one Observation
  on server.fire.ly, plus whatever `$conformance` created on hapi.fhir.org
  during its probe run. All synthetic, no PHI, left in place.
- **Production was not touched**, and no container, service, or deployment was
  started, stopped, or rebuilt. The Flask processes started for these runs were
  ephemeral, bound to 5092–5098 on localhost, used scratch SQLite databases
  outside the repository, and were all stopped at the end of the pass.

## Reproducing this

**Updated 2026-09-04 (#530).** The scratch directory this section named is
gone, and nothing was recovered from it. The two live walkthroughs are now
reproducible from the repository:

- `scripts/walkthrough-upstream.sh hapi` and `… generic`
- transcripts of a second person's run: `docs/evidence/2026-09-04-set2-rerun/`
- what that run found: `docs/evidence/2026-09-04-set2-connectors-rerun.md`

The four other scripts of this pass — `registry-contract.sh`, `auth_probe.py`,
`halfconfig.sh`, `medplum-qa.sh` — are still uncommitted and still gone. §5, §6
and R1 rest on them and have **not** been independently re-run. What this
section said on 2026-08-16:

> Scratch directory for this pass (scripts and raw run logs):
> `…/scratchpad/set2/` — `walkthrough-hapi.sh`, `registry-contract.sh`,
> `auth_probe.py`, `halfconfig.sh`, `medplum-qa.sh`, and the `*-run.txt` capture
> of each. Not committed; R8 covers what should be.
