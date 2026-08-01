# Development guide

Everything a contributor needs, regardless of editor or AI tooling.

## Build & test

```bash
# Python (Flask app) — deps via uv
uv sync
uv run flask --app main init-db                   # REQUIRED once — see traps below
STEP_UP_SECRET=dev-secret python main.py          # http://localhost:5000

# All Python tests / one file / one test
uv run python -m pytest tests/ -q
uv run python -m pytest tests/test_r6_routes.py::test_name -v

# Lint (CI-gated) — `uv run`, not `uvx`/`pipx`; see traps below
uv run ruff check .

# Node MCP server
cd services/agent-orchestrator && npm ci && npx tsc --noEmit && npm test

# Playwright e2e (requires Flask on :5000)
cd e2e && npm ci && npx playwright install --with-deps chromium && npm test

# Full stack
docker-compose up -d --build
```

`.env` is **not auto-loaded** — export vars in your shell (or the platform sets
them). A key present only in `.env` behaves as unset.

Local dev works on Python 3.13, but **CI runs 3.11** — avoid 3.12+-only syntax
(e.g. backslash escapes inside f-string expressions).

### Three setup traps

Each of these presents as something other than itself, so they cost more time
than they should.

- **Lint with `uv run ruff`, never `uvx ruff` or `pipx run ruff`.** CI uses the
  version locked in the dev group (`ruff>=0.15,<1`); `uvx`/`pipx` fetch the
  newest published release. A newer ruff reports hundreds of findings CI does
  not — none of them caused by your change, and all of them a waste to chase.
- **`init-db` is required before the server is usable.** Nothing auto-creates
  tables. Skip it and the first request fails with `AuditWriteError:
  OperationalError`, which reads like a bug in the audit layer but is a missing
  schema. That the request fails at all is correct behaviour: the audit trail is
  fail-loud, so anything that cannot be recorded is not served.
- **Port 5000 collides with AirPlay Receiver on macOS**, surfacing only as
  `Address already in use`. Use `PORT=5099 …`, or turn AirPlay off in System
  Settings.

### Flask lifecycle commands

Importing `main` and calling `create_app()` configure routes and extensions but
do not mutate the database or start background threads. Run lifecycle work
explicitly when provisioning or recovering a deployment:

```bash
uv run flask --app main init-db
uv run flask --app main seed-demo --tenant-id desktop-demo
uv run flask --app main recover-zombies
```

`init-db` runs `alembic upgrade head` against the same SQLAlchemy engine as the
application. New revisions belong in `migrations/versions/`; they must contain
explicit reversible DDL and must not call `db.create_all()` or
`metadata.create_all()`. Check model/schema drift locally with:

```bash
uv run alembic upgrade head
uv run alembic check
```

Do not run `alembic stamp` on an existing database until completing the schema
checks in the [database migration runbook](runbooks/database-migrations.md).

`initialize_database(app)`, `seed_demo_tenant(app)`,
`recover_zombie_jobs(app)`, and `start_wearables_poller(app)` are also available
for process supervisors and deployment scripts. Environment flags cannot run
these operations during application construction; each WSGI worker imports
`main` independently, so deploy hooks or the explicit CLI commands own all
mutable lifecycle work.

## Architecture map

```text
Flask (Python)                          Node (TypeScript)
  /r6/fhir/*    FHIR facade + guardrails  services/agent-orchestrator
  /r6/actions/* real-world actions          /mcp        Streamable HTTP MCP
  /r6/smbp/*    BP monitoring               /mcp/rpc    JSON-RPC bridge
  /fasten, /shc connectors                  27 tools (read/write tiers)
  r6/quality, r6/labs, r6/sdc,
  r6/conformance — pure engines + register_*_routes(blueprint, deps)
```

New feature modules follow the `r6/quality` pattern: a pure engine (no
Flask/DB), report builders, and a `register_*_routes` function wired in
`r6/routes.py`. Tests live in `tests/` (pytest, fixtures in `conftest.py`:
`client`, `tenant_id`, `auth_headers`, `tenant_headers`, sample resources).

### CareAgents (`careagents/`) — the consumer app, not the engine

A second Flask app (`create_app()` factory, `CARE_*` env in `careagents/
config.py`, fail-closed in production). Two boundaries hold the design together:

- **Its only data path to PHI is HealthClaw's HTTP API** (`careagents/
  healthclaw.py`). CareAgents stores **no PHI** — accounts, tenant ids and
  connection metadata only. The live schema is the check: if a change would add
  a table holding clinical text, that data belongs in HealthClaw instead.

  Chat history is the worked example. Transcripts are PHI-adjacent, so they live
  in HealthClaw's `ConversationMessage` per tenant — which `purge_tenant`
  already covers, so "delete my records" removes conversations for free. What
  CareAgents keeps in memory is only a cache, rebuilt on demand.

- **Ids do not transfer across the boundary.** A CareAgents `agent_id` is not a
  HealthClaw command-center agent id; sending one where the other is expected is
  rejected (the conversation endpoint 400s on it). Because those writes are
  best-effort by design, the rejection is silent — the feature simply does
  nothing. Carry CareAgents identifiers in an unvalidated field such as
  `metadata`.

  CareAgents unit tests fake the HealthClaw client, so they prove a call is
  *made*, not that the wire format is *accepted*. Exercise new cross-boundary
  calls against a running HealthClaw before trusting them.

## Security invariants (do not regress)

- `validate_step_up_token` returns `(bool, str)` — **destructure both**; never
  truthiness-test the tuple.
- Every FHIR resource access emits an AuditEvent; audit `detail` is PHI-free.
- Writes require a step-up token; **clinical** writes additionally require a
  human confirmation, via one of two mechanisms:
  - **Action rail** (`r6/actions/`): a separate approval endpoint consuming a
    single-use step-up credential — genuinely out-of-band.
  - **Direct clinical FHIR writes**: `X-Human-Confirmed: true` (HTTP 428
    otherwise). The header is client-supplied and therefore spoofable by an
    agent that already holds a write token — a known gap tracked in #214.
    Prefer the action rail for anything new.
- Redaction imports: `from r6.redaction import apply_redaction` (Safe Harbor)
  or `apply_patient_controlled_redaction(resource, patient_id)`.
- **Never preserve an upstream `display` or `CodeableConcept.text` to make
  records readable.** Real feeds put patient names in those fields — a LOINC
  coding in `tests/test_recursive_redaction.py` carries `"Glucose for Jane
  Secret"` (synthetic, but modelled on what upstream systems actually send).
  That test exists because preserving `display` looks correct, passes
  hand-written tests, and leaks PHI.

  Readability comes from `r6/terminology.py` instead, and the order is the
  point: `apply_redaction` strips everything the upstream sent, *then*
  re-attaches labels from a table keyed by code. A code is safe precisely
  because its meaning belongs to the code rather than to the patient — "E11.9
  means type 2 diabetes" is true for everyone ever assigned E11.9. Unknown
  codes stay unlabelled on purpose: an agent saying "a record is here I could
  not read" is honest, and `unlabelled_codes()` reports misses so the table
  grows from evidence rather than guesswork.
- **"No known allergies" is never inferred** — the SDC populate/review flow
  asserts NKA only from an explicit human attestation.
- The whole set is enforced by the **conformance harness**:
  `tests/test_guardrail_conformance.py` pins the measured CI baseline, and
  `GET /r6/fhir/$conformance` grades any live deployment. The in-process local
  FHIR profile is Grade A (7/7). The optional CLI MCP profile remains a separate
  grade until its transport follow-up lands; enabling it can therefore lower the
  combined result without changing the local profile.

## Deploy notes (maintainers)

- Pushing `main` auto-deploys the Flask app (Railway) and the marketing site
  (Vercel).
- **The MCP server does NOT auto-deploy.** Deploy it from a staging dir so the
  repo-root `railway.toml` (Flask Dockerfile) isn't picked up:

  ```bash
  mkdir /tmp/mcp-deploy && cd services/agent-orchestrator \
    && cp -R Dockerfile package.json package-lock.json tsconfig.json src /tmp/mcp-deploy/ \
    && cd /tmp/mcp-deploy \
    && railway link --project <project-id> --service mcp-server --environment production \
    && railway up --service mcp-server --detach
  ```

- **CareAgents runs on Railway** (`deploy/careagents/Dockerfile`, one gunicorn
  worker) against its own private Postgres, and does **not** auto-deploy with
  the main Flask app. `deploy/careagents/careagents.service` and `deploy.sh` are
  the superseded VPS path, kept until the DNS cutover completes.

  One worker is deliberate: the in-process conversation cache is per-process, so
  a second worker would serve some turns from an empty one. Threads give
  concurrency without splitting that state.

- **CareAgents: migrating from SQLite to Postgres.** CareAgents keeps its own
  engine and metadata (`careagents/models.py`), separate from the Flask app's.
  SQLite is single-writer, host-local, and lost with the host — fine for one
  tester, wrong for real accounts. It logs a warning at boot until
  `CARE_DATABASE_URL` points at Postgres.

  ```bash
  # 1. Provision Postgres and set the URL on the careagents.cloud host
  CARE_DATABASE_URL=postgresql://user:pass@host:5432/careagents

  # 2. Tables are created on first boot (create_all + _ensure_columns);
  #    no Alembic chain here, unlike the Flask app.
  # 3. Copy existing rows if the SQLite file has real accounts — dump
  #    ca_accounts / ca_passkeys / ca_connections / ca_agents / ca_surfaces
  #    BEFORE cutover; passkeys are binary columns, so use a real client
  #    rather than a CSV round-trip.
  ```

  Once cut over, change the SQLite warning in `careagents/config.py` to a
  `_require` so a regression fails closed instead of warning. CI runs the
  CareAgents suite against real Postgres (`CARE_TEST_DATABASE_URL` in the
  `postgres-tests` lane), so schema incompatibilities surface before deploy.

  Two things that lane exists to catch, both invisible on SQLite: varchar length
  limits, and **foreign keys, which SQLite does not enforce by default** — an
  invented id passes locally and fails only there. Note the lane runs a
  **hardcoded allowlist of test paths** in `ci.yml`, so a new DB-touching test
  file runs SQLite-only until someone adds it.

- **Production is watched on a schedule.** `scripts/prod_watch.py` runs every
  six hours (`.github/workflows/prod-watch.yml`) and checks that the engine is
  alive and still grading A, that records come back readable (a regression of
  the terminology labels would otherwise be silent), that CareAgents can reach
  its database, that the sign-in page still accepts an 8-digit code, and that
  the **token-locked MCP server refuses unauthenticated callers** — a 200 there
  would mean the real tool surface had been left open. There are two MCP
  deployments: that locked one, and a public demo pinned server-side to a
  synthetic tenant.

  Every check is unauthenticated by design, so it needs no credentials and no
  synthetic account — and therefore does **not** cover the signed-in journey.
  That limit is stated in the script rather than implied.

- Release process: [RELEASING.md](../RELEASING.md). Drift guards
  (`tests/test_site_version_sync.py`, `tests/test_gemini_extension.py`) fail
  the suite if versions/tool counts diverge between `pyproject.toml`, the
  manifest, the README, and the site templates — a green suite means they're
  in sync.

## Useful surfaces while developing

- Mint a tenant token: `POST /r6/fhir/internal/step-up-token {"tenant_id": ...}`
- Seed demo data: `POST /r6/fhir/internal/seed`
- Guardrail scorecard: `GET /r6/fhir/$conformance?format=text`
- Skill discovery index: `GET /.well-known/agent-skills/index.json`
