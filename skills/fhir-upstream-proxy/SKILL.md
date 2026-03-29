---
name: fhir-upstream-proxy
description: >
  Connect to real FHIR servers through the MCP guardrail proxy. Use when:
  (1) Connecting to HAPI FHIR, SMART Health IT, or Epic sandbox servers,
  (2) Proxying AI agent requests to production EHR systems with guardrails,
  (3) Ensuring upstream server URLs never leak to clients,
  (4) Understanding how redaction, audit, and step-up auth apply to upstream data.
disable-model-invocation: true
---

# FHIR Upstream Server Proxy

Connect to real FHIR servers while keeping the full MCP guardrail stack active.

```
Client -> MCP Server -> Flask (guardrails) -> Upstream FHIR Server
                             |
               redaction, audit, step-up,
               tenant isolation, disclaimers,
               URL rewriting
```

## When to Use This Skill

- You need to connect an AI agent to a real FHIR server (HAPI, SMART, Epic)
- You want automatic PHI redaction on upstream server responses
- You need audit trails for agent access to production clinical data
- You want URL rewriting so upstream server details never leak to clients

## Configuration

Set the `FHIR_UPSTREAM_URL` environment variable to enable proxy mode:

```bash
# HAPI FHIR R4 (open, no auth)
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 python main.py

# SMART Health IT (open, no auth)
FHIR_UPSTREAM_URL=https://r4.smarthealthit.org python main.py

# HAPI FHIR R5 (open, no auth)
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR5 python main.py

# Local HAPI instance
FHIR_UPSTREAM_URL=http://localhost:8080/fhir python main.py

# Docker Compose with upstream
FHIR_UPSTREAM_URL=https://hapi.fhir.org/baseR4 docker-compose up -d --build
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FHIR_UPSTREAM_URL` | (empty) | Upstream FHIR server base URL. Enables proxy when set. |
| `FHIR_UPSTREAM_TIMEOUT` | `15` | HTTP timeout for upstream requests (seconds) |
| `FHIR_LOCAL_BASE_URL` | (empty) | Local server URL for URL rewriting in responses |

## What the Proxy Does

### Reads
Fetched from upstream, then redacted + audited + disclaimers added. The agent
never sees unredacted upstream data.

### Searches
All query parameters forwarded to upstream. Results redacted per entry.
Upstream's full search capabilities are available (chaining, _include, etc.).

### Writes
Validated locally first (structural checks), then forwarded to upstream with
step-up auth verification. Both local and upstream audit records created.

### URL Rewriting
All upstream server URLs in responses are replaced with local proxy URLs.
The agent and client never see the upstream server's hostname or paths.

### Health Check
`/r6/fhir/health` reports upstream connection status including FHIR version
and server software name.

### Graceful Fallback
Network errors return proper FHIR OperationOutcome responses, not stack traces.

## What the Proxy Does NOT Do

- **No caching** — every request hits the upstream server
- **No SMART-on-FHIR auth forwarding** — uses upstream's native auth model
- **No cross-version translation** — R4 responses stay R4
- **No tenant isolation on upstream** — enforced locally only
- **No response transformation** — upstream resources pass through as-is (after redaction)

## Tested Upstream Servers

| Server | URL | Auth | Status |
|--------|-----|------|--------|
| HAPI FHIR R4 | `https://hapi.fhir.org/baseR4` | None | Tested |
| SMART Health IT | `https://r4.smarthealthit.org` | None | Tested |
| HAPI FHIR R5 | `https://hapi.fhir.org/baseR5` | None | Tested |
| Local HAPI | `http://localhost:8080/fhir` | None | Tested |
| Epic Sandbox | `https://open.epic.com/Interface/FHIR` | OAuth 2.0 | Limited |

## Proxy Implementation

The proxy uses `httpx` for HTTP client operations with:
- Configurable timeout (default 15 seconds)
- Automatic redirect following
- `application/fhir+json` accept header
- User-Agent identification: `HealthClaw-Guardrails/1.0.0`

URL rewriting is recursive — it traverses the entire response JSON tree and
replaces all occurrences of the upstream URL with the local proxy URL.
