---
features: [AI agents, MCP, PHI redaction, Audit trail, Step-up authorization, Human-in-the-loop, Multi-tenancy]
languages: [Python, TypeScript, Shell]
runtimes: [Docker]
---
# HealthClaw Guardrails in front of Aidbox

Aidbox holds the record. An AI agent talks to a guardrail proxy in front of
it, and the proxy enforces four things the FHIR authorization model does not
express: redact on read, audit every access, step up on writes, and hold
anything irreversible for a human.

```text
AI agent ──▶ MCP server ──▶ Guardrail proxy ──▶ Aidbox
  :3001                        :5000              :8080
                                 │
                          PHI redaction
                          Audit trail
                          Step-up auth
                          Human-in-the-loop
                          Tenant isolation
```

Nothing about the Aidbox side is unusual, and that is the point. The guardrail
layer is additive; the FHIR server underneath keeps behaving like a FHIR
server, and still holds the complete, fully-identified record.

Companion to the article *"We standardized how to get health data. We never
standardized what an agent may do with it."*

## Prerequisites

1. Docker
2. Cloned repository

```bash
git clone https://github.com/aks129/HealthClawGuardrails.git
cd HealthClawGuardrails/examples/aidbox-healthclaw-guardrails
```

## Run it

```bash
cp .env.example .env      # set STEP_UP_SECRET; AIDBOX_LICENSE is optional
docker compose up -d
```

**Activate Aidbox.** Open <http://localhost:8080> and click *Continue with
Aidbox account*. Until you do, Aidbox answers **every** route with a 302 to
"Log in to activate Aidbox" — including `/health`, so the symptom reads as a
network fault rather than a licence one. Compose is waiting on that health
check and starts the proxy by itself once you are through. To skip the click
entirely, put a free self-hosted key from [aidbox.app](https://aidbox.app)
(account → licenses) in `AIDBOX_LICENSE`; unattended runs need that path.

```bash
./scripts/seed-aidbox.sh  # 1 Patient, 1 Condition, 3 Observations
./scripts/walkthrough.sh  # the steps below, asserted
```

`walkthrough.sh` opens with a preflight that separates "Aidbox is not
activated" from "the proxy image is too old to authenticate", because both
otherwise surface as the same unexplained failure several steps later.

Two more things that will bite you:

- **macOS holds port 5000.** AirPlay Receiver (ControlCenter) listens there,
  so `up` fails with "address already in use". Set `HEALTHCLAW_PORT=5099` in
  `.env` and read `:5099` for `:5000` throughout.
- **The image tags are pinned, deliberately.** `:latest` is only republished
  when a release is cut, and for a while it pointed at a build that predated
  upstream authentication — so it ignored the two `FHIR_UPSTREAM_CLIENT_*`
  variables this example depends on, and the wiring below was configured
  correctly and did nothing. A pin turns that into a pull failure instead.

One more, if you also develop HealthClaw itself: **running this example makes
120 of HealthClaw's own tests fail.** `FHIR_VALIDATOR_URL` defaults to
`http://localhost:8080`, and the availability probe treats anything answering
`/health` as a FHIR validator — which Aidbox, on that port, is not. The
failures present as `assert 422 == 201` on ordinary writes and point nowhere
near the cause. `docker compose stop aidbox` before running the suite, or set
`FHIR_VALIDATOR_URL` to something unreachable. Tracked as
[#488](https://github.com/aks129/HealthClawGuardrails/issues/488).

From a HealthClaw checkout you can build the two images instead of pulling
them:

```bash
docker compose -f docker-compose.yaml -f docker-compose.build.yaml up -d --build
```

## What each step shows

### 1. The same read, with and without governance

Direct to Aidbox, the record is fully identified, as it should be:

```bash
curl -u "$AIDBOX_CLIENT:$AIDBOX_SECRET" http://localhost:8080/fhir/Patient/pt-demo
```

```json
{ "resourceType": "Patient", "id": "pt-demo",
  "name": [{"given": ["Maria"], "family": "Alvarez"}],
  "identifier": [{"system": "urn:mrn", "value": "MRN-88214"}],
  "birthDate": "1974-03-11",
  "address": [{"line": ["221 Baker St"], "city": "Pittsburgh"}] }
```

Through the proxy, same resource, same Aidbox:

```bash
curl -H "X-Tenant-Id: demo" http://localhost:5000/r6/fhir/Patient/pt-demo
```

The name is reduced to initials, the MRN is masked, the address and phone are
gone, and the birth date is truncated to a year.

Aidbox still holds the complete record. Redaction is a property of the path
the agent uses, not a modification of the data. `walkthrough.sh` asserts this
by looking for the distinctive values themselves rather than for a mask
string, because a redactor that returned `***masked***` and nothing else
would satisfy the lazier check.

### 2. The read left a record

```bash
curl -H "X-Tenant-Id: demo" "http://localhost:5000/r6/fhir/AuditEvent?_count=1"
```

An AuditEvent naming the tenant, the agent, the resource and the time, with no
PHI in the detail — so the record you hand a reviewer is safe to hand over.

### 3. A write, and two gates that do not substitute for each other

Recording a blood pressure needs a machine credential *and* a human
confirmation, and neither one stands in for the other. Four requests show
that better than two, because a pair of refusals in sequence only proves that
*some* refusal happened:

| `X-Human-Confirmed` | `X-Step-Up-Token` | result |
|---|---|---|
| — | — | **428** confirmation missing |
| `true` | — | **401** a confirmation is not a credential |
| — | valid | **428** a credential is not a confirmation |
| `true` | valid | **201** |

The bare request reports **428** rather than 401 because the human-in-the-loop
check runs in a `before_request` hook, ahead of every handler's auth gate —
that ordering is what stops an unauthenticated caller reaching the handler at
all. Worth knowing before you read a status code as evidence of which gate
you tripped.

Only after both does the Observation reach Aidbox, which the walkthrough
verifies by asking Aidbox rather than the proxy — a proxy reporting its own
201 says nothing about what was stored:

```bash
curl -u "$AIDBOX_CLIENT:$AIDBOX_SECRET" \
  "http://localhost:8080/fhir/Observation?subject=Patient/pt-demo&code=85354-9"
```

That matrix is the whole argument. The agent did useful work. It could not
finish alone.

One caveat the table cannot carry: `X-Human-Confirmed` is set by the caller,
so it evidences a human the way a checkbox does. It is a compensating
control, not proof. See *What this example does not show* below.

### 4. Grade the deployment

```bash
curl "http://localhost:5000/r6/fhir/\$conformance?format=text"
```

Seven properties, 35 checks, run against the deployment that is actually
running. The same harness runs in HealthClaw's CI as a merge gate, so a
regression shows up as a grade change rather than an incident.

## How the two servers are wired

The proxy holds its own Aidbox credential — the `healthclaw` Client in
[`init-bundle/bundle.json`](init-bundle/bundle.json) — so the agent never
receives, and never needs, an Aidbox credential:

```yaml
healthclaw:
  environment:
    FHIR_UPSTREAM_URL: http://aidbox:8080/fhir
    FHIR_UPSTREAM_CLIENT_ID: healthclaw
    FHIR_UPSTREAM_CLIENT_SECRET: ${FHIR_UPSTREAM_CLIENT_SECRET}
    STEP_UP_SECRET: ${STEP_UP_SECRET}
    READ_AUTH_ENABLED: "true"
```

The AccessPolicy scopes that Client to `/fhir/*`. An agent that gets past the
proxy still has no route to Aidbox's admin surface, `$sql`, or the Client that
authorizes it.

`FHIR_LOCAL_BASE_URL` makes the proxy rewrite Aidbox's base URL out of every
response. An agent that learns the real endpoint will try to route around the
guardrails, so it is never told what it is.

## What this example does not show

- **A licence check is not a guardrail.** Everything above assumes Aidbox is
  reachable. The proxy's failure mode when it is not is a degraded health
  check and a 502, not a silent empty bundle — but that is a different
  property from the four this example demonstrates.
- **Redaction is a compensating control, not a de-identification
  determination.** A record with a rare diagnosis and an unusual date
  sequence is not made unlinkable by masking a name. What changes is the
  default.
- **The clinical-write human gate is a header today** (`X-Human-Confirmed`),
  which the caller that sets it can spoof. It is documented as a compensating
  control rather than proof a human acted; the action rail's separate
  approval endpoint is the real mechanism. HealthClaw tracks this as a known
  gap.

## Links

- HealthClaw Guardrails: <https://github.com/aks129/HealthClawGuardrails> (MIT)
- Live conformance grade: <https://app.healthclaw.io/r6/fhir/$conformance>
- Aidbox: <https://aidbox.app>
