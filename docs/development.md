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
pipx run ruff check r6/ tests/ scripts/ main.py app.py

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
- Writes require a step-up token; **clinical** writes additionally require
  `X-Human-Confirmed: true` (HTTP 428 otherwise).
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
