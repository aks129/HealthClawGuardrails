# Which FHIR server we sit in front of

HealthClaw is a guardrail layer, so the server holding the records is
somebody else's. This is how you tell it which one, and how it authenticates.

## Configure

```bash
FHIR_UPSTREAM_KIND=aidbox                      # aidbox | medplum | hapi | generic
FHIR_UPSTREAM_URL=https://aidbox.example/fhir  # the FHIR base
FHIR_UPSTREAM_CLIENT_ID=healthclaw
FHIR_UPSTREAM_CLIENT_SECRET=...
FHIR_UPSTREAM_TOKEN_URL=                       # only to override the derived one
```

`FHIR_UPSTREAM_KIND` is optional and defaults to `generic`. What naming a kind
buys you is the auth style and the token-endpoint rule, so you do not have to
work either out from our source.

| kind | auth | notes |
|---|---|---|
| `aidbox` | HTTP Basic | An Aidbox `Client` credential, scoped by an AccessPolicy. |
| `medplum` | OAuth2 client-credentials | A Medplum `ClientApplication`. Token endpoint is the server's origin + `/oauth2/token`. |
| `hapi` | none | Public sandboxes take no credential. Add the client id/secret for one behind Basic. |
| `generic` | HTTP Basic | Any FHIR server. Basic when credentials are set, anonymous when they are not. |

An unknown kind is refused rather than defaulted. An unknown kind means an
unknown auth style, and quietly falling back to anonymous would point
unauthenticated requests at a record system because somebody mistyped a
variable.

## Verify

`GET /r6/fhir/health` reports the kind **we** resolved beside the software the
server names itself:

```json
{"checks": {"upstream": {
  "status": "connected", "kind": "medplum", "software": "medplum",
  "fhir_version": "4.0.1", "upstream_url": "http://localhost:8103/fhir/R4"}}}
```

Read those two together. When they disagree — `kind: medplum` against
`software: aidbox` — the deployment is pointed somewhere nobody meant, and
that is otherwise invisible until a request fails for a reason that names
neither.

## Existing deployments

Nothing to change. `MEDPLUM_BASE_URL` / `MEDPLUM_CLIENT_ID` /
`MEDPLUM_CLIENT_SECRET` still work and imply `kind=medplum`, and
`FHIR_UPSTREAM_URL` with Basic credentials still works as `generic`.
`FHIR_UPSTREAM_URL` continues to win over `MEDPLUM_BASE_URL` when both are
set. Every one of those combinations is pinned in
`tests/test_upstream_connector_registry.py`, because this is the one path
every upstream read and write goes through and a deployment that stops
authenticating does not crash — it returns 502s that look like the upstream's
fault.

## What this does not cover

The **SHARP** per-request path, where the caller supplies the server in
`X-FHIR-Server-URL` and their own token. That is a different trust
relationship: the credential belongs to the caller, not to us, which is why
its 401s pass through to them instead of becoming our 502s. Folding it into
this registry would put a caller-supplied server through the same resolution
as an operator-configured one.

## Verified

Both connectors have been run end to end, not just reasoned about.

- **Aidbox** — [`examples/aidbox-healthclaw-guardrails`](../examples/aidbox-healthclaw-guardrails/),
  six asserted steps against Aidbox `edge`.
- **Medplum** — [the self-host runbook](runbooks/medplum-self-host-qa.md),
  8/8 against Medplum 5.1.30. Self-hosted on purpose: the hosted service
  exercises the one code path that always worked.

Adding a connector means adding a row to `CONNECTORS` in
`r6/upstream_connectors.py` and running it against a real instance of that
server. The second half is not optional — every defect found in these two was
invisible from the code.
