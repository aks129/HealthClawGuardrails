# Running the Medplum QA against a self-hosted Medplum

The connector's authentication had never run against a real Medplum. Every
test mocked `_fetch_medplum_token`, so the guardrails wrapped around it were
covered while the connector itself was not, and two defects lived there:
a token endpoint hardcoded to Medplum's hosted service, and a token failure
that fell through to an unauthenticated request.

Self-hosted, not hosted, is the configuration that matters here. The hosted
service exercises the one code path that already worked; the endpoint bug
only appears when the server is somewhere else. This runbook is the loop that
found it, so it can be run again.

No Medplum credentials are needed. A self-hosted instance issues its own.

## 1. Start Medplum

```bash
curl -O https://raw.githubusercontent.com/medplum/medplum/main/docker-compose.full-stack.yml
```

Two settings have to be overridden for a headless bootstrap. Put this beside
it as `docker-compose.override.yml`:

```yaml
services:
  medplum-server:
    environment:
      # The upstream compose enables signup captcha with Medplum's public test
      # keys. Those validate against Google, and a script has no browser.
      MEDPLUM_RECAPTCHA_SITE_KEY: ''
      MEDPLUM_RECAPTCHA_SECRET_KEY: ''
      # Bootstrapping takes several /auth calls in a row and the default
      # limiter returns 429 partway through.
      MEDPLUM_DEFAULT_AUTH_RATE_LIMIT: '600'
```

```bash
docker compose -f docker-compose.full-stack.yml -f docker-compose.override.yml \
  -p medplum-selfhost up -d
```

Wait for `http://localhost:8103/healthcheck` to return 200. Roughly a minute
on a cold pull.

**The rate limiter lives in Redis**, so restarting the server does not reset
it. If a bootstrap run gets throttled, clear the counters:

```bash
docker compose -p medplum-selfhost exec redis redis-cli -a medplum FLUSHALL
```

## 2. Bootstrap a project and a client

Three steps, and the third needs PKCE — Medplum refuses the code exchange with
"Missing verification context" otherwise, and the error does not say PKCE.

1. `POST /auth/newuser` with `codeChallenge` (S256) and `codeChallengeMethod`
2. `POST /auth/newproject` with the returned `login`
3. `POST /oauth2/token` with `grant_type=authorization_code`, the returned
   `code`, and `code_verifier`

A new project is created with a `ClientApplication` already in it, so read it
rather than creating one:

```
GET /fhir/R4/ClientApplication      (Bearer, the token from step 3)
```

Its `id` and `secret` are `MEDPLUM_CLIENT_ID` and `MEDPLUM_CLIENT_SECRET`.

## 3. Point HealthClaw at it

```bash
export MEDPLUM_BASE_URL=http://localhost:8103/fhir/R4
export MEDPLUM_CLIENT_ID=...  MEDPLUM_CLIENT_SECRET=...
export PORT=5098 STEP_UP_SECRET=medplum-qa-secret APP_ENV=development
uv run flask --app main init-db && uv run python main.py
```

`GET /r6/fhir/health` should report the upstream connected and name it:

```json
{"mode": "upstream",
 "checks": {"upstream": {"software": "medplum", "status": "connected",
                         "upstream_url": "http://localhost:8103/fhir/R4"}}}
```

`software: medplum` is the line worth reading. It comes from the upstream's
own CapabilityStatement, so it is the server confirming what it is rather
than us restating our configuration.

## 4. Run the QA

```bash
TOKEN=$(curl -s -X POST -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: medplum-qa' -d '{"tenant_id":"medplum-qa"}' \
  http://127.0.0.1:5098/r6/fhir/internal/step-up-token | jq -r .token)

uv run python scripts/smoke_medplum.py --base-url http://127.0.0.1:5098 \
  --tenant-id medplum-qa --step-up-token "$TOKEN"
```

Expected: `8/8 guardrail checks passed`.

## What this run found

- The derived token endpoint resolves to `http://localhost:8103/oauth2/token`
  and a client-credentials grant against it succeeds. The previous code posted
  to `https://api.medplum.com/oauth2/token` regardless, so this configuration
  could never have authenticated.
- The smoke script's first check, `write blocked without step-up (401)`, was
  asserting on a 400. It sent no `Content-Type`, the body never parsed, and the
  request was refused as malformed by the depth-bounded parse that runs ahead
  of the auth gate on purpose (#312). The gate it is named after was never
  reached. It failed rather than passed, which is the only reason anyone saw
  it; written as "not 201" it would have passed forever.
