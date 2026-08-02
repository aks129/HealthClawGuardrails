# Development guide

Everything a contributor needs, regardless of editor or AI tooling.

## Build & test

```bash
# Python (Flask app) — deps via uv
uv sync
STEP_UP_SECRET=dev-secret python main.py          # http://localhost:5000

# All Python tests / one file / one test
uv run python -m pytest tests/ -q
uv run python -m pytest tests/test_r6_routes.py::test_name -v

# Lint (CI-gated)
pipx run ruff check .

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

- **CareAgents does NOT auto-deploy either.** Neither path is wired to a push:
  the VPS path rsyncs the tree (`./deploy/careagents/deploy.sh`) and the Railway
  path is `railway up` from a staging dir. Merged work therefore sits unshipped
  until a human runs one of them — in #258 both deployments were serving code
  months older than `main` while `scripts/prod_watch.py` reported 9/9 green,
  because nothing it checked could tell the two apart.

  Every deploy must stamp `careagents/BUILD_SHA`, the two-line marker
  (`<sha12>` / `<unix commit time>`) that `careagents/_build.py` reads once at
  import and `/healthz` reports as `build` / `built_at`. It is gitignored on
  purpose: a committed marker goes stale silently, which is the failure it
  exists to catch. `deploy.sh` writes it for you; before `railway up`, write it
  into the staging dir yourself:

  ```bash
  printf '%s\n%s\n' "$(git rev-parse --short=12 HEAD)" "$(git log -1 --format=%ct)" \
    > careagents/BUILD_SHA
  railway up --service careagents --detach
  ```

  Verify with `curl -s <deployment>/healthz` — `build` must be the commit you
  deployed. `scripts/prod_watch.py --expect-sha <sha>` asserts the same thing,
  and the scheduled prod-watch accepts any commit merged to `main` in the last
  24h, exiting `2` (a separate, less urgent alarm than `1`) when the deployed
  build matches none of them. The marker is telemetry only — nothing branches
  on it, and a missing one reports `unknown` and still serves normally.

- **CareAgents: migrating from SQLite to Postgres.** CareAgents keeps its own
  engine and metadata (`careagents/models.py`), separate from the Flask app's.
  Production still defaults to SQLite, which is single-writer, host-local, and
  lost with the host — fine for one tester, wrong for real accounts. It logs a
  warning at boot until `CARE_DATABASE_URL` points at Postgres.

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
