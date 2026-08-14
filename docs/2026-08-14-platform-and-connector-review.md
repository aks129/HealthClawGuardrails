# Platform and connector review — 2026-08-14

What we run, where, and what is not earning its keep. Written from the live
platforms rather than from memory: `railway list --json`, the Vercel API, and
the repo's own config files.

Findings are ordered by what they cost if left alone, not by how easy they are
to fix. Two are already fixed in the PR that carries this document; the rest
need a decision that is not mine to make.

## What is actually deployed

**Railway — `awake-serenity`** is production. One project, thirteen services:

| | |
|---|---|
| app | `HealthClawGuardrails`, `careagents`, `careagents-worker`, `mcp-server`, `mcp-demo`, `openclaw-bot`, `shl-server` |
| data | `Postgres`, `Postgres-_BFS` |
| cache | `Redis`, `Redis-3Yfx`, `Redis-CS4j`, `Redis-u_QV` |

Four other Railway projects exist (`castage`, `generous-radiance`,
`shl-deploy`, `melodious-truth`) and none of them is HealthClaw.

**Vercel** holds ten projects under `aks129s-projects`; `healthclaw` and
`careagents` are the two that relate to this repo. Since the 2026-08-06
incident, `app.healthclaw.io` is served by Railway, and Vercel's role is the
marketing site and demos.

## Findings

### 1. The Medplum connector only worked against hosted Medplum — FIXED

`_MEDPLUM_TOKEN_ENDPOINT` was the constant `https://api.medplum.com/oauth2/token`
with no override. Self-hosting is Medplum's central proposition, so pointing
`MEDPLUM_BASE_URL` at your own server was the expected case, and it sent your
self-hosted client credentials to a service that has never heard of them.

The endpoint is now derived from the base URL that the proxy was built with,
with `MEDPLUM_TOKEN_URL` as an explicit override.

### 2. A Medplum token failure became an anonymous request — FIXED

`_inject_bearer` caught every exception, logged it, and returned — after which
the request went out **with no `Authorization` header at all**. A token failure
was silently downgraded to an anonymous request against the record system.

This is what made finding 1 dangerous rather than merely broken: wrong
endpoint, swallowed failure, unauthenticated read. It now raises, so a caller
sees a failed request instead of a successful one nobody intended.

Both defects survived because every Medplum test mocks `_fetch_medplum_token`.
The guardrails wrapped around the connector were thoroughly covered; the
connector's authentication had never run.

### 3. Four Redis instances and two Postgres, for seven services

Nobody set out to run four caches. This is what a year of "add a plugin to
unblock the thing" looks like, and each one is a monthly line item, a backup
surface, and a credential.

Not a change to make from a service list: the mapping from service to
datastore has to come from each service's environment. Worth an hour with the
Railway dashboard before the next invoice, and a decision recorded here.

### 4. `vercel.json` uses the legacy `builds` array

```json
"builds": [{ "src": "api/index.py", "use": "@vercel/python" }]
```

A `builds` array opts the project out of zero-config, which means out of Fluid
Compute defaults, the current Python runtimes, and build-output optimisations.
Vercel's current guidance is `vercel.ts` with a typed config.

The reason to be careful rather than quick: `api/index.py` is a **second entry
point into the same Flask app**, and the 2026-08-06 outage was two generators
disagreeing about which was authoritative. Modernising the config without
first settling what Vercel is *for* would repeat that. My recommendation is to
settle the question first: if Vercel is marketing plus demos, it should not be
building the Flask app at all.

### 5. The prod stack is single-instance

`railway.toml` sets a healthcheck and `restartPolicyType = "ON_FAILURE"` with
three retries, which is the right shape. What it does not set is replicas or a
region, so every service is one container in one place. A restart is a cold
start with downtime, and Railway supports replicas per service.

Worth doing for `HealthClawGuardrails` and `mcp-server` specifically — the two
things a partner or a demo audience talks to. `careagents-worker` should stay
single-instance unless its job claiming is idempotent, which is a separate
question from this review.

### 6. `preDeployCommand` seeds the demo tenant on every deploy

```
flask --app main init-db && flask --app main seed-demo --tenant-id desktop-demo
```

This is the mechanism behind #457, where the demo tenant duplicated itself into
nineteen patients. Seeding is idempotent now and the drift guard checks the
exact patient set, so the arrangement is safe today — but a deploy-time write
to a tenant that a demo depends on is a standing risk, and it is worth asking
whether the seed belongs in a deploy hook at all rather than in a command
somebody runs deliberately.

## The connector question

The request was for "standard connectors to Medplum and Health Samurai". Both
exist, by different routes, and that is the actual problem:

| | Medplum | Aidbox |
|---|---|---|
| how | `MEDPLUM_BASE_URL` + client credentials | `FHIR_UPSTREAM_URL` + HTTP Basic |
| code | `MedplumProxy` subclass | base `FHIRUpstreamProxy` |
| auth | OAuth2 client-credentials | HTTP Basic |
| verified against a live server | yes, self-hosted 5.1.30 ([runbook](runbooks/medplum-self-host-qa.md)) | yes ([example](../examples/aidbox-healthclaw-guardrails/)) |

Two code paths, two naming conventions, no way for an operator to ask what is
supported. Both are verified now. Aidbox is still the one with no name of its own.

**Recommendation, not done here:** one connector registry keyed by a
`FHIR_UPSTREAM_KIND` of `aidbox` / `medplum` / `hapi` / `generic`, each
declaring its auth style and its token endpoint rule, with the env names
unified under `FHIR_UPSTREAM_*`. `MEDPLUM_*` stays as an alias so nothing
breaks. That is a focused change, but it is a change to the one path every
upstream read and write goes through, and it deserves its own PR rather than a
ride along with a bug fix.

The prerequisite was a live Medplum to verify against. That has since been
done — see [the runbook](runbooks/medplum-self-host-qa.md). A self-hosted
Medplum needs no credentials from anybody, and it is the configuration that
matters: the hosted service exercises the one code path that already worked.

The run confirmed the two fixes above against a real server and found a third
defect, in the QA script rather than the product. `smoke_medplum.py`'s first
check is named `write blocked without step-up (401)` and was asserting on a
400: it sent no `Content-Type`, so the body never parsed and the request was
refused as malformed before the credential was considered. It failed rather
than passed, which is the only reason it was noticed.

Final state: 8/8 guardrail checks against self-hosted Medplum 5.1.30.
