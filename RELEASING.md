# Releasing

**Cadence:** a minor release roughly **every 3–4 weeks** when `main` has accumulated
user-visible features, or immediately for security fixes (patch release). Don't sit
on shipped work — if `main` is meaningfully ahead of the last tag for more than a
month, cut a release.

## Release checklist

Copy this into the release PR/issue and check items off.

### 1. Verify

- [ ] Full Python suite green: `uv run python -m pytest tests/ -q`
- [ ] Node suite green: `cd services/agent-orchestrator && npx tsc --noEmit && npm test`
- [ ] Lint clean: `pipx run ruff check .`
- [ ] Demo gates pass: `./scripts/demo_e2e.sh` (all 11 gates)
- [ ] Dependabot alerts triaged (no open high/critical): repo → Security → Dependabot

### 2. Version bumps (keep in sync)

- [ ] `pyproject.toml` → `version` (then `uv lock`)
- [ ] `services/agent-orchestrator/package.json` → `version` (then `npm install --package-lock-only`)
- [ ] README: release badge, "At a glance" line, Release-highlights table (new row on top)
- [ ] MCP tool count still accurate everywhere (badge, at-a-glance, `## MCP Tools (N)`, adapters manifest `tool_count`)
- [ ] **healthclaw.io templates** (`templates/index.html` stats + copy, `base.html` nav badge,
      `wiki.html`) — `tests/test_site_version_sync.py` fails the suite if these drift, so a
      green suite means the site content is in sync; Vercel redeploys it on push

### 3. Tag + GitHub release

- [ ] Commit the bumps, push `main`, wait for CI green
- [ ] `git tag -a vX.Y.0 -m "vX.Y.0 — <headline>"` and `git push origin vX.Y.0`
- [ ] `gh release create vX.Y.0 --title "vX.Y.0 — <headline>" --notes-file <notes>` —
      notes follow the house style: what shipped, why it matters, honest scope limits,
      breaking changes (if any), upgrade notes

### 4. Deploy

- [ ] Flask + marketing auto-deploy on push (Railway `HealthClawGuardrails`, Vercel) — verify
      `https://app.healthclaw.io/r6/fhir/metadata` returns 200 post-deploy
- [ ] **mcp-server does NOT auto-deploy** — staging-dir `railway up` (see
      [docs/development.md](docs/development.md) deploy notes), then verify `POST /mcp/rpc tools/list` returns the expected tool count
- [ ] Re-seed `desktop-demo` if the release changed seed data: `POST /r6/fhir/internal/seed`

### 5. Announce (within 48h of the release)

- [ ] **GitHub**: release published; pin a Discussion if the release is major
- [ ] **LinkedIn**: short post (hook → 2-3 concrete capabilities → honest limits → repo link);
      tag partners only when the release touches their integration
- [ ] **Substack**: longer-form piece for releases with a story (a build narrative, a
      lesson, a standards deep-dive) — not every release needs one
- [ ] **HTN Slack / communities**: only where the release is genuinely relevant to the channel
- [ ] Update `healthclaw.io` if the release changes the headline capability list

### 6. After

- [ ] Open issues for anything intentionally deferred from the release
- [ ] Check `good first issue` supply — keep at least 3-5 open for newcomers
