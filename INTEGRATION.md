# INTEGRATION.md

Integration guides for connecting HealthClaw Guardrails to external FHIR servers
and health data platforms.

---

## SmartHealthConnect — HAPI FHIR R4

[SmartHealthConnect](https://github.com/healthconnect/fhir-server) ships a
Maven-based HAPI FHIR R4 server that can run alongside the guardrail stack as
a local upstream.  No auth is required in the default configuration.

### Server details

| Property | Value |
| --- | --- |
| Runtime | Java 17+, Maven 3.8+ |
| Port | 8000 |
| Base path | `/fhir` |
| FHIR version | R4 (4.0.1) |
| Auth | None (open, default) |
| Database | H2 in-memory (default) |

### Start the server

```bash
# Clone and build
git clone https://github.com/healthconnect/fhir-server
cd fhir-server
mvn clean package -DskipTests

# Start (port 8000)
java -jar target/fhir-server-1.0-SNAPSHOT.jar
```

Verify it is running:

```bash
curl http://localhost:8000/fhir/metadata | python -m json.tool | grep fhirVersion
```

### Connect HealthClaw Guardrails to it

Set `FHIR_UPSTREAM_URL` before starting Flask:

```bash
FHIR_UPSTREAM_URL=http://localhost:8000/fhir \
FHIR_LOCAL_BASE_URL=http://localhost:5000/r6/fhir \
uv run python main.py
```

Or add it to `.env`:

```
FHIR_UPSTREAM_URL=http://localhost:8000/fhir
FHIR_LOCAL_BASE_URL=http://localhost:5000/r6/fhir
```

### Verify the connection

```bash
curl http://localhost:5000/r6/fhir/health | python -m json.tool
# "mode": "upstream"
# "checks": {"upstream": {"status": "connected", "software": "HAPI FHIR", ...}}
```

### What the guardrail stack adds

All SmartHealthConnect responses pass through the full guardrail stack:

- **PHI redaction** — names truncated to initials, identifiers masked, birth dates
  truncated to year, addresses stripped to city/state/country, telecom values replaced.
- **Audit trail** — every read and write emits an AuditEvent in the local SQLite store.
- **Step-up auth** — write operations (POST / PUT / DELETE) require a valid
  `X-Step-Up-Token` header.
- **URL rewriting** — `http://localhost:8000/fhir/*` URLs in responses are replaced
  with `http://localhost:5000/r6/fhir/*` so the upstream is never exposed to clients.
- **Medical disclaimers** — clinical resources (Condition, Observation, etc.) get an
  `_mcp_summary.disclaimer` field.

### Limitations with SmartHealthConnect upstream

- Tenant isolation is enforced by the guardrail layer (header check), not by the
  upstream server itself.  All tenants share the same HAPI FHIR instance.
- The guardrail stack does not cache upstream responses — every request hits HAPI.
- Cross-version translation is not supported; the server must serve R4.
- For persistent data use a PostgreSQL-backed HAPI config (`application.yaml`).

---

## Medplum

Medplum is a cloud-hosted FHIR R4 server with OAuth2 client-credentials auth.

### Prerequisites

1. Create a Medplum project at [app.medplum.com](https://app.medplum.com).
2. Create a **Client application** and note the Client ID and Client Secret.
3. Set the base URL to `https://api.medplum.com/fhir/R4`.

### Environment variables

```
MEDPLUM_BASE_URL=https://api.medplum.com/fhir/R4
MEDPLUM_CLIENT_ID=<your-client-id>
MEDPLUM_CLIENT_SECRET=<your-client-secret>
# Optional — speeds up token refresh
REDIS_URL=redis://localhost:6379/0
```

Do **not** set `FHIR_UPSTREAM_URL` — it takes priority over `MEDPLUM_BASE_URL`.

### How token caching works

1. On first request the proxy calls `POST https://api.medplum.com/oauth2/token`
   with `grant_type=client_credentials`.
2. The access token is stored in Redis under the key `medplum:access_token` with
   TTL = `expires_in - 60` seconds (60-second safety buffer).
3. Subsequent requests read from Redis; no HTTP call is made until the token nears
   expiry.
4. When Redis is unavailable the proxy falls back to an in-process dict cache with
   the same TTL logic.

### Verify the connection

```bash
curl http://localhost:5000/r6/fhir/health | python -m json.tool
# "mode": "upstream"
# "checks": {"upstream": {"status": "connected", "software": "Medplum", ...}}
```

---

## Tested upstream servers (no auth required)

| Server | `FHIR_UPSTREAM_URL` |
| --- | --- |
| HAPI FHIR R4 (public) | `https://hapi.fhir.org/baseR4` |
| SMART Health IT | `https://r4.smarthealthit.org` |
| HAPI FHIR R5 (public) | `https://hapi.fhir.org/baseR5` |
| SmartHealthConnect (local) | `http://localhost:8000/fhir` |
| Local HAPI Docker | `http://localhost:8080/fhir` |
