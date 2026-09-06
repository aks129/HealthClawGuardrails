# Runbook: enabling the MCP connector on real records

The order of operations that takes the locked MCP server from "token-locked,
unreachable from hosted connectors" to "an ordinary claude.ai connector on a
consented tenant", without ever leaving the endpoint open. Design:
`docs/specs/2026-08-16-mcp-authorization.md` §7 and §13. Every step here is
the owner's; nothing in it is run by an agent.

The invariant that holds at every step: an unauthenticated `initialize`
against the production origin returns 401, and nothing else.

## 0. What has to be on `main` first

| Piece | Where | Live on merge? |
|---|---|---|
| Authorization-server conformance (302, `aud`, client auth, root discovery) | #669 | **Yes.** The Flask app auto-deploys from `main`. From the merge on, a client that registers without a method is confidential and must send its secret; claude.ai does. |
| Introspection and the read-only mint | held branch, opens after #669 | Yes, inert: introspection refuses everybody until its client credential is set |
| Consent handoff, audit, revoke | held branch, opens after the one above | Yes, inert: without `CAREAGENTS_CONSENT_URL` the MCP audience keeps the demo policy |
| Rotating refresh tokens | held branch, opens after the one above | Yes, live: every code grant returns a refresh token from then on |
| CareAgents consent page | #670 | No: CareAgents is a manual deploy (`docs/runbooks/careagents-durable-worker.md`) |
| MCP server OAuth path | #671 | No: the MCP server is a manual deploy, and off until `MCP_OAUTH_ENABLED=true` |
| Walkthrough | #672 | n/a |

Merge in that order. The held branches open from `main` one at a time; do
not let anyone stack them on GitHub, because CI does not run on stacked PRs.

## 1. DNS and domain (#522), before anything on the MCP server deploys

The canonical resource is `https://mcp.healthclaw.io/mcp` (council ruling
D4). Today that hostname points at Vercel and answers `DEPLOYMENT_NOT_FOUND`,
which is also a subdomain-takeover surface.

1. In Railway, on the `mcp-server` service, add the custom domain
   `mcp.healthclaw.io` and note the target it gives you.
2. Repoint the DNS record for `mcp.healthclaw.io` from Vercel to that target.
   Remove the Vercel side.
3. Verify over DoH, not `dig`, because the office network forges port 53:
   `curl 'https://dns.google/resolve?name=mcp.healthclaw.io&type=CNAME'`.
4. Check that `healthclaw.io` is verified in the Vercel account so nobody else
   can claim a subdomain there.
5. Confirm `https://mcp.healthclaw.io/health` answers from Railway (the JSON
   names `healthclaw-guardrails`).

## 2. Variables, per service

Set these before the deploys in §3. The same secret appears in three places
on purpose; they must be byte-identical.

**Flask (`HealthClawGuardrails` service)**

| Variable | Value | Why |
|---|---|---|
| `OAUTH_ISSUER` | `https://app.healthclaw.io` | pins every published OAuth URL and the FHIR audience string |
| `MCP_CANONICAL_RESOURCE` | `https://mcp.healthclaw.io/mcp` | the MCP audience the authorization server will mint for |
| `MCP_OAUTH_DEMO_TENANT` | the synthetic demo tenant, which must be in `PUBLIC_TENANTS` | what the MCP audience binds when no consent surface is configured |
| `MCP_INTROSPECTION_CLIENT_ID`, `MCP_INTROSPECTION_CLIENT_SECRET` | new, random | the MCP server's credential for `POST /r6/fhir/oauth/introspect` |
| `CAREAGENTS_CONSENT_URL` | `https://careagents.cloud/authorize` | **set last**, after #670 is deployed; switches the MCP audience from the demo policy to the consent surface |
| `INTERNAL_TOKEN_MINT_SECRET` | already set | also keys the consent handoff |
| `READ_AUTH_ENABLED` | already `true`; confirm by observation, not by reading the config | spec §10.2 |

**MCP server (`mcp-server` service)**

| Variable | Value |
|---|---|
| `MCP_CANONICAL_RESOURCE` | the same string as Flask's, character for character |
| `MCP_INTROSPECTION_CLIENT_ID`, `MCP_INTROSPECTION_CLIENT_SECRET` | the same pair as Flask's |
| `INTERNAL_TOKEN_MINT_SECRET` | the same value as Flask's (it may already be set for the automint) |
| `FHIR_BASE_URL` | Flask's internal base ending in `/r6/fhir` (already set) |
| `MCP_AUTH_TOKEN` | already set; stays |
| `MCP_OAUTH_ENABLED` | **unset until §4** |

The server refuses to start with `MCP_OAUTH_ENABLED=true` and any of the
first three missing, or with a non-canonical resource string.

**CareAgents (web and worker, from the same stage)**

| Variable | Value |
|---|---|
| `HEALTHCLAW_MINT_SECRET` | the same value as Flask's `INTERNAL_TOKEN_MINT_SECRET` (already set) |
| `HEALTHCLAW_PUBLIC_BASE` | `https://app.healthclaw.io` |
| `CARE_REAL_RECORDS` | `allowlist` with the first clinician's account email, or `on` |

## 3. Deploys, in order

1. Flask: lands with each merge. After the last held branch merges, run the
   four curls from spec §3: the root discovery document, the PRM once
   `MCP_CANONICAL_RESOURCE` is set on the MCP server, and the two endpoints.
2. MCP server: stage and `railway up` per `reference` notes in the deploy
   runbook, with `MCP_OAUTH_ENABLED` still unset. Confirm `/health` reports
   `oauth.enabled: false` and that the unauthenticated `initialize` is 401
   with `resource_metadata` in the challenge (phase 1 is now live because the
   resource is set and DNS answers from Railway).
3. CareAgents web and worker from one stage (#644 is the stale build this
   also clears). Confirm `/authorize?req=x` answers the "not valid" page and
   not a 500.

## 4. Enable, staging first

1. Set `MCP_OAUTH_ENABLED=true` on the MCP server. `/health` shows
   `oauth.enabled: true`.
2. Run the walkthrough against production with the demo policy
   (`CAREAGENTS_CONSENT_URL` still unset on Flask):
   `MCP_URL=https://mcp.healthclaw.io/mcp MCP_AUTH_TOKEN=<static> bash services/agent-orchestrator/qa/oauth-walkthrough.sh`.
   Exit 0 is the gate. Exit 1 names the guarantee that broke; stop there.
3. Set `CAREAGENTS_CONSENT_URL` on Flask. From now on the MCP audience sends
   a browser to careagents.cloud to decide.
4. The end-user run (spec §8.4): from claude.ai, Settings, Connectors, Add
   custom connector, URL `https://mcp.healthclaw.io/mcp`, no client id or
   secret. Sign in on the consent page, choose the synthetic connection
   first, approve with the passkey, and ask Claude for the demo patient's
   records. Then the same with a real connection, on the owner's own account
   only. Record what Claude did at each step; that transcript closes #568.

## 5. Rollback

`MCP_OAUTH_ENABLED=false` on the MCP server, then redeploy or restart. Every
connector token is refused again within the five-minute cache window; the
static token path never changed. Unsetting `CAREAGENTS_CONSENT_URL` on Flask
returns the MCP audience to the demo tenant. Neither step opens anything.

## 6. Things that will look like bugs and are not

- The PRM and the enriched challenge are served only when the request `Host`
  is `mcp.healthclaw.io`. On the platform hostname the server answers exactly
  as before. That is RFC 9728 conformance, not a missing feature.
- A token minted for the FHIR surface is refused at the MCP server with 401,
  never 403. That is the audience check working.
- The first hour after enabling, an old connector session that cached a
  refusal keeps refusing until its cache expires.
- CareAgents' consent page shows only connection labels, provider names and
  the client's name. It never shows records.
