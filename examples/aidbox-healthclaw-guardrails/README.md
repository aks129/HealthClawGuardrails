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

## Run it

You need a free Aidbox licence from [aidbox.app](https://aidbox.app)
(Aidbox account → licenses → new self-hosted licence).

```bash
cp .env.example .env     # paste AIDBOX_LICENSE, set STEP_UP_SECRET
docker compose up -d
./scripts/seed-aidbox.sh # 1 Patient, 1 Condition, 3 Observations
./scripts/walkthrough.sh # the five steps below, asserted
```

Two things that will bite you before anything interesting happens:

- **No licence, no API.** Without `AIDBOX_LICENSE` Aidbox starts and looks
  healthy, then answers every API call with a 302 to a browser page reading
  "Log in to activate Aidbox". Through the proxy that surfaces as a degraded
  upstream, which says nothing about licensing. Compose refuses to start
  without the variable for exactly this reason.
- **macOS holds port 5000.** AirPlay Receiver (ControlCenter) listens there,
  so `up` fails with "address already in use". Set `HEALTHCLAW_PORT=5099` in
  `.env` and read `:5099` for `:5000` throughout.

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

### 3. A write, blocked twice

Recording a blood pressure, with no step-up token, returns **401**. Mint a
token and retry: **428**, pending human confirmation. Only after confirmation
does the Observation reach Aidbox, which you can verify by querying Aidbox
directly, going around the proxy:

```bash
curl -u "$AIDBOX_CLIENT:$AIDBOX_SECRET" \
  "http://localhost:8080/fhir/Observation?subject=Patient/pt-demo&code=85354-9"
```

That sequence is the whole argument. The agent did useful work. It could not
finish alone.

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
