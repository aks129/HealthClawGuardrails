# Deployed-surface inventory

**Date:** 2026-09-04 · **Measured at:** `89b42fbd0e66b0170e2e2d1248fcfeb8f91c9da5`
(`89b42fb`, tip of `main`) · **Produced by:** `scripts/surface_inventory.py`

**Result: we serve more than we watch.** The script probed 25 surfaces this
repository names as deployed, read-only. `scripts/prod_watch.py` requests 4 of
them. 16 answer and nothing checks them (4 ours, 12 third-party). 5 are named
here and do not answer.

#624 (an abandoned CareAgents instance on an old VPS) was found by accident,
when somebody asked whether a deploy script was still used. This exercise asks
the question that accident raised: what else is out there? The answer includes
the #624 box, still serving today, and two surfaces we own that no monitor has
ever requested.

## Re-run it

```bash
uv run --with requests python scripts/surface_inventory.py \
    --json-out /tmp/surface-inventory.json
```

One GET per endpoint. No retries, no credential, no method other than GET. The
script reads the retired VPS address from `deploy/careagents/deploy.sh` at run
time and scrubs it from every line it prints, so this document can quote the
output directly.

### On the DNS answers

Every name resolved twice, over DoH and through the system resolver, because
this network answers port 53 from a stale cache (`CLAUDE.md`). The two agreed
for every host except `healthclaw.io`, `mcp.healthclaw.io` and
`shl.healthclaw.io`, where the address sets differ. All three sit behind
Vercel, and both answers reached Vercel, so the likeliest cause is anycast or
per-resolver load balancing rather than a forged answer. This method cannot
tell those apart, and none of the three findings turns on the difference. The
one host where a wrong answer would flip a finding, `careagents.cloud`, is one
where the two resolvers agree.

## What the monitor actually watches

Not read from `prod_watch`'s constants. The script imports that module,
replaces its `get` and `post` with recorders, runs it with no network, and
takes the URLs it requested:

```
https://app.healthclaw.io/r6/fhir/health
https://app.healthclaw.io/r6/fhir/$conformance
https://app.healthclaw.io/r6/fhir/Condition?_count=5
https://app.healthclaw.io/r6/fhir/Patient?_count=200
https://careagents-production.up.railway.app/healthz
https://careagents-production.up.railway.app/
https://careagents-production.up.railway.app/auth
https://mcp-server-production-5112.up.railway.app/health
https://mcp-server-production-5112.up.railway.app/mcp
https://mcp-demo-production-ee2c.up.railway.app/health
https://mcp-demo-production-ee2c.up.railway.app/mcp
→ 4 hosts
```

Measuring rather than reading was the right call twice over. See findings 3 and
6: a check named for one product measures a hostname no user ever types, and
the count in the closing "all N checks passing" line is not a constant.

## Group 1: live and watched (4)

| Surface | Probe | Status | Running |
|---|---|---|---|
| healthclaw engine | `https://app.healthclaw.io/r6/fhir/health` | 200 | `version=1.10.0`, `fhirVersion=6.0.0-ballot3` |
| CareAgents (platform host) | `https://careagents-production.up.railway.app/healthz` | 200 | `build=89b42fbd0e66`, `built_at=1788143223`, workers up |
| MCP server (token-locked) | `https://mcp-server-production-5112.up.railway.app/health` | 200 | `version=1.9.0` |
| MCP server (public demo) | `https://mcp-demo-production-ee2c.up.railway.app/health` | 200 | `version=1.9.0` |

The CareAgents build matches the commit this document was measured at, so that
deployment is current. The engine's `1.10.0` matches `pyproject.toml`. The two
MCP servers do not match their source: see finding 2.

## Group 2: live and unwatched (16)

### Ours (4)

| Surface | Probe | Status | Running |
|---|---|---|---|
| CareAgents VPS (address in `deploy/careagents/deploy.sh`) | `/healthz`, hostname presented to that address | 200 | `status=ok`, `accounts=true`, `provider=openai`. **No `build`, no `built_at`, no `run_workers`.** |
| CareAgents consumer domain | `https://careagents.cloud/healthz` | 200 | `build=89b42fbd0e66`, `built_at=1788143223`, workers up |
| HealthClaw public site | `https://healthclaw.io/` | 200 | `Server: Vercel`. No build marker is served. |
| Agent-skills discovery document | `https://healthclaw.io/.well-known/agent-skills/index.json` | 200 | `Server: Vercel` |

### Upstreams and partner surfaces (12)

Third-party services this code or these documents call. Unwatched is the
expected state for all of them, and none is a finding. They are listed so the
next reader does not have to rediscover which ones still answer.

| Surface | Probe | Status |
|---|---|---|
| HAPI FHIR public R4 | `https://hapi.fhir.org/baseR4/metadata` | 200 (nginx/1.28.3) |
| SMART Health IT R4 | `https://r4.smarthealthit.org/metadata` | 200 (nginx/1.10.3) |
| Firely public server | `https://server.fire.ly/R4/metadata` | 200 |
| Medplum hosted API | `https://api.medplum.com/fhir/R4/metadata` | 200 |
| tx.fhir.org terminology | `https://tx.fhir.org/r4/metadata` | 200 |
| Fasten Connect API | `https://api.connect.fastenhealth.com/v1` | 404 (host answers) |
| MEDENT FHIR | `https://fhir.medent.com/fhir/R4/metadata` | 301 (Apache) |
| HealthEx MCP | `https://api.healthex.io/mcp` | 404 (host answers) |
| Health Bank One MCP | `https://mcp.app.healthbankone.com/mcp` | 401 (istio-envoy) |
| Health Bank One OAuth | `https://oauth.app.healthbankone.com/` | 404 (host answers) |
| PromptOpinion marketplace | `https://app.promptopinion.ai/marketplace` | 200 (Kestrel) |
| ClawHub skill listing | `https://clawhub.ai/aks129/skills/fhir-r6-guardrails` | 200 |

## Group 3: referenced but dead (5)

| Surface | Probe | Result | Named at |
|---|---|---|---|
| `shl.healthclaw.io` | `/` | 404 from Vercel | `skills/share-health-qr/SKILL.md:214,215,226,230` |
| `healthclaw.up.railway.app` | `/r6/fhir/health` | 404, `Application not found` (railway-hikari) | `skills/personal-health-records/SKILL.md:108,132,158,171`; `scripts/build_quickstart_pdf.py:485` |
| `mcp.healthclaw.io` | `/mcp` | 404 from Vercel | `docs/specs/2026-08-16-mcp-authorization.md:128,130,549,746` |
| `schemas.agentskills.io` | `/discovery/0.2.0/schema.json` | NXDOMAIN | `app.py:339`; `tests/test_wellknown_skills.py:21` |
| `sharponmcp.com` | `/` | NXDOMAIN | `services/agent-orchestrator/src/index.ts:230,259,704`; `templates/wiki.html:562`; `templates/faq.html:328` |

`mcp.healthclaw.io` is already written up in
`docs/specs/2026-08-16-mcp-authorization.md` §9.6 as a dangling record. The
other four were not tracked anywhere before this document.

## Findings

### 1. The #624 box is still serving (owner decision)

```
$ curl --resolve careagents.cloud:443:<address in deploy/careagents/deploy.sh> \
       https://careagents.cloud/healthz
{"accounts":true,"provider":"openai","status":"ok"}
```

Identical to the payload in #624, including what is missing. No `build`, no
`built_at`, no `run_workers`. Those three fields are present on both live
CareAgents deployments, so this box predates the build stamping added in #258
and the worker readiness added since. It still holds an accounts store.

Public DNS for `careagents.cloud` does not point at it (confirmed over DoH and
through the system resolver, which agree), so no user is routed there. Reaching
it needs the address and the hostname together.

This is the finding #624 already carries. #624 was filed on 2026-09-04, the
same day as this sweep, so this is not an aged report: it is the same fact
reached from the other direction, by enumeration rather than by accident. That
is the only claim it supports. PR #623, which retires the deploy path, is still
open, so `deploy/careagents/deploy.sh` on `main` still defaults to that host.

Nothing here can be fixed in the repository. The decisions are the ones #624
lists: shut it down or watch it, rotate anything it shares with production, and
decide what happens to the account data.

### 2. Both MCP deployments run an older build than `main`, and no check asks

`/health` on both MCP servers reports `version: 1.9.0`. That field is
`SERVER_VERSION`, read from `services/agent-orchestrator/package.json`
(`services/agent-orchestrator/src/index.ts:32`). That file on `main` says
`1.10.0`. Both deployments are therefore behind the source, by at least the
change that raised the version.

Three checks in `prod_watch` speak for these two hosts. All three pass. Two ask
whether `/health` returns 200 and one asks whether the demo server answers an
unauthenticated handshake. None reads the version field the response already
carries, so all three are satisfied by any build at all. This is the #258 shape
exactly: the CareAgents build check exists because every other check was
equally satisfied by a months-old build, and the same hole is still open on the
MCP servers.

A third number disagrees with both. `server.json:9` declares `1.8.0` to the MCP
registry while the package it describes is at `1.10.0`.

What this does not establish: whether `1.9.0` is materially different from
`1.10.0`, or whether anything is broken. The MCP deployments are manual and
need explicit authorization, so a lag is expected. The finding is that the lag
is invisible, not that it is harmful.

**Reproduction:**

```bash
curl -s https://mcp-demo-production-ee2c.up.railway.app/health
grep '"version"' services/agent-orchestrator/package.json
```

### 3. `careagents.cloud` is the host users type, and nothing checks it

Every check named `careagents:` in `scripts/prod_watch.py` requests
`careagents-production.up.railway.app`. The domain in `CARE_ORIGIN` and
`CARE_RP_ID`, the one a person's browser shows and WebAuthn binds to, gets no
request from any check.

Today the two return the same `build` and `built_at`, so `careagents.cloud` is
a custom domain in front of the same Railway service rather than a second
instance. That makes the gap narrow but not empty. This repository already
records a Railway failure that breaks a custom domain while the container stays
healthy. A domain pinned to the wrong target port answers "Application failed
to respond" while the service serves normally (`CLAUDE.md`,
`docs/2026-08-06-two-generators-three-laws.md`). Under that failure all four
`careagents:` checks pass, the workflow stays green, and no user can sign in.

`scripts/prod_watch.py:64` names the constant `CAREAGENTS` and sets it to the
platform hostname. Reading the constant name would have reproduced the error
this inventory exists to catch.

**Reproduction:**

```bash
uv run --with requests python scripts/surface_inventory.py | grep careagents
```

The consumer domain appears under LIVE AND UNWATCHED; the platform host appears
under LIVE AND WATCHED.

### 4. `healthclaw.io` is a first-class host in the code and unwatched

`deployment.py:1-20` describes two hosts that "are not interchangeable" and
names `healthclaw.io` as the second. It serves the public site, the skills
catalogue, and the `r6-dashboard` the README sends visitors to. It answers 200.
No check requests it.

Its Vercel purpose is recorded as an open owner decision in
`docs/2026-08-16-system-topology.md` ("purpose unsettled"). Unwatched is a
defensible answer for a host nobody has decided to keep. It is not a defensible
answer by default, which is what it is today.

### 5. Four dead names are still published, two of them by running code

Two are documentation and cost a reader their time:

- `skills/share-health-qr/SKILL.md` shows `shl.healthclaw.io` viewer and manage
  links as the output of the skill. The host 404s.
- `skills/personal-health-records/SKILL.md` and
  `scripts/build_quickstart_pdf.py` tell readers to configure
  `https://healthclaw.up.railway.app/mcp`. Railway answers
  `Application not found`. `SKILL.md:158` also gives a `curl -X POST` against
  that host, so a reader following it writes nothing anywhere.

Two are served by running code, which is worse, because the reader is a machine:

- `app.py:339` puts `https://schemas.agentskills.io/discovery/0.2.0/schema.json`
  in the `$schema` field of the live discovery document at
  `https://healthclaw.io/.well-known/agent-skills/index.json`. That hostname is
  NXDOMAIN. The parent zone `agentskills.io` resolves; the `schemas` label does
  not. `tests/test_wellknown_skills.py:21` asserts the prefix, so the test
  passes on a URL that cannot be fetched.
- `services/agent-orchestrator/src/index.ts:259,704` returns
  `spec: "https://sharponmcp.com"` in the MCP `initialize` response. That domain
  is NXDOMAIN at the registry level.

Neither of those is a security problem and neither breaks a client that does not
dereference the URL. Both are claims our servers make that no longer resolve.

### 6. Two honesty defects in `scripts/prod_watch.py`

Reported, not patched. QA does not edit production code.

**6a. "all N checks passing" counts what ran, not what exists.**
`scripts/prod_watch.py:396` prints `f"all {len(results)} checks passing"`, and
`results` is appended to at run time. The build check is only recorded when
`--expect-sha` is given; in informational mode it prints through `report()` and
is never counted. The same fully-healthy production therefore reports two
different totals:

```
RESULT no --expect-sha        -> len(results) = 11
RESULT --expect-sha given     -> len(results) = 12
```

Reproduction: `docs/evidence/2026-09-04-surface-inventory-check-count.py`
(committed alongside this document) stubs `get`/`post` with a healthy response
and runs `prod_watch.run` twice. Severity is low, because the run that omits the
build check is the run that already failed the readiness check next to it. It is
still a completeness claim derived from whatever ran, in the one script whose
stated purpose is that it verified every line it prints.

**6b. Check names carry a product, targets carry a hostname.** Finding 3 above.

## What was excluded, and why

The repository names roughly 300 distinct hostnames. These classes were left
out on purpose, so the omissions are visible rather than silent:

| Class | Examples | Why |
|---|---|---|
| Code system identifiers | `hl7.org`, `loinc.org`, `snomed.info`, `unitsofmeasure.org`, `dicom.nema.org` | `system` values in FHIR codings. They are namespaces, not endpoints we depend on. |
| Package registries and CDNs | `files.pythonhosted.org`, `registry.npmjs.org`, `cdnjs.cloudflare.com` | Build-time dependencies, covered by the lockfiles. |
| Documentation and badge links | `github.com`, `img.shields.io`, `opencollective.com`, `keepachangelog.com` | Link-outs. A dead one costs a click. |
| Reserved and example names | `example.com`, `*.example`, `*.invalid`, `*.test`, `evil.com`, `self.hosted.example` | Cannot resolve by design. |
| Loopback and container names | `localhost:*`, `127.0.0.1:*`, `aidbox:8080`, `db.internal:5432`, `open-wearables:8000` | Local or compose-internal. Not deployed surfaces. |
| Provider APIs called with a key | `api.anthropic.com`, `api.openai.com`, `api.resend.com`, `api.telegram.org`, `api.twilio.com`, `api.bland.ai` | Probing them without a credential proves nothing, and this exercise sends no credential. |

## What this did NOT cover

- **Anything behind authentication.** Every probe was unauthenticated. The
  signed-in journey (sign up, email code, connect, chat, approve) is untested
  here, exactly as `prod_watch` states for itself.
- **Ports.** Only 443 was contacted, on names this repository writes down. A
  service on another port, or on a host nobody wrote down, is invisible to this
  method. That is the residual #624 risk and this document does not close it.
- **The Mac mini relay.** `deploy/careagents/imessage_relay.py` runs on a
  machine with no public name in this repository. It holds a mint secret. It was
  not probed and is not in any group above.
- **The Telegram surface.** The bot handle comes from `CARE_TELEGRAM_BOT`, which
  is empty in the repository, so there is no fixed `t.me` name to probe.
- **Whether a live surface is correct.** The build markers say which artifact is
  deployed. They do not say it works. Only CareAgents reports a commit; the
  engine and the two MCP servers report a package version, so for those three
  "current" means "matches the version declared in the source", which is a
  weaker claim. A rebuild that forgets to raise the version is invisible to it.
- **Anything Vercel serves under a preview URL.** No `*.vercel.app` name appears
  in the repository, so none was probed.

## Follow-ups filed

| Finding | Tracked in |
|---|---|
| 1. The #624 box still serves | **#624** (existing). Confirmed independently on 2026-09-04. Owner decision: shut down or watch, rotate the shared credential, decide what happens to the account data. |
| 2. MCP builds behind `main` | **#625** |
| 3. `careagents.cloud` unwatched | **#625** |
| 4. `healthclaw.io` unwatched | **#625** |
| 5. Four dead published names | **#626** |
| 6. Monitor honesty defects | **#625** |

Nothing in findings 2 to 6 was fixed here. Each is a decision with a cost
attached, and this document reports rather than patches.
