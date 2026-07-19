# Changelog

All notable changes to HealthClaw Guardrails are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.9.0] — 2026-07-19 — CareAgents Consumer App + Forms Rail End-to-End + Grade A (7/7)

### Added
- **CareAgents — the hosted consumer experience** (`careagents/`, live at
  [careagents.cloud](https://careagents.cloud)): a non-developer signs up (email code +
  WebAuthn passkey), connects records, and spins up a guardrailed health agent in about a
  minute. Its *only* data path is HealthClaw's HTTP API — it stores no PHI, only accounts,
  tenant pointers, and connection metadata. Shipped across the release:
  - **Connector marketplace** — pluggable registry over the sources HealthClaw already
    brokers (Fasten, wearables via Open Wearables, Apple Health, sample records), with
    honest "coming soon" tiles for everything not yet wired.
  - **iMessage surface** — bind an agent from the hub and text it over a relay; same
    guardrailed turn loop as web and Telegram.
  - **Advisor registry** — specialties ported from SmartHealthConnect as prompt-blocks over
    the guarded tool set: healthy-habits 📊, care-completion ✅, medication-refills 💊
    (read-side only, and says so), diet-exercise 🏃. Kids-health and research-monitor are
    listed but honestly deferred (missing caregiver identity / missing tools).
  - **Informed consent at the connect-real-records moment** — versioned consent
    (`CONSENT_VERSION`) enforced server-side (HTTP 428 without an explicit `consent: true`),
    CARIN-style plain-language copy, recorded per connection. Sample records skip it by
    design (synthetic data).
  - **Anthropic OAuth token support** — run the agent loop on a Claude subscription
    (`ANTHROPIC_OAUTH_TOKEN`) instead of a metered API key.
- **Forms rail end-to-end — the first real action ships whole.** `$populate` fills the
  canonical intake questionnaire from the patient's record → structured per-item review
  (every med/allergy confirmed individually; "no known allergies" rejected unless explicitly
  attested; the item list is re-derived server-side so a crafted request can't skip a row) →
  reviewed `QuestionnaireResponse` → provenance-stamped PDF persisted as a FHIR
  `DocumentReference` → signed, expiring download link. Fails loud (`needs_review` /
  `provider_not_configured` / `stale_source_data`).
- **Error fidelity is guardrail property seven — Grade A is now 7/7.** `$conformance`
  grades whether unknown parameters and unsupported modifiers are rejected or flagged,
  never silently swallowed. Hardened across the stack: local search emits value-free
  corrections with truthful totals, backend `OperationOutcome`s survive both MCP transports
  through a PHI-safe sanitizer (thanks @ashish-b-work), and a drift guard pins
  `SAFE_MODIFIER_TOKENS` to byte-identical values across the Python and TypeScript copies.
- **MCP Apps — embedded UIs served by the engine.** `care_gaps` results now carry a
  `_meta.ui.resourceUri` rendering an engine-served care-gaps view
  (`/r6/fhir/mcp-apps/care-gaps/`, `text/html; profile=mcp-app`), joining the wearables
  view. The page's only fetch target is the guarded `$care-gaps` operation — the UI
  inherits the guardrails by construction.
- **Open Wearables, for real:** client reconciled to the actual 0.6.3 API (the old one
  targeted endpoints that don't exist), and sleep sessions + naps now map to FHIR.
- **Security hardening pass:** fail-closed production config, authenticated tenant reads,
  MCP transport auth, Alembic migrations, PHI minimization; Fasten download hosts validated
  by parsed URL (not substring); CI workflow permissions tightened and email-error echo
  removed (CodeQL).
- Docs: public advisors-system roadmap + agent task guide, beta program + tester guide,
  ActionExecutor cookbook example, Aidbox (Health Samurai) recipe, per-surface quickstart
  CTA + site SEO.

### Changed
- **Licensing posture documented (staying MIT).** After evaluating Fair Source (FSL-1.1-MIT)
  and open-core models, the project remains MIT while adoption grows. `LICENSING.md` records the
  posture: future releases may adopt FSL-1.1-MIT and/or open core based on adoption, any
  MIT-released version stays MIT forever, and the guardrail core stays freely available.
  Contributions now use DCO sign-off (`git commit -s`) to keep future licensing options clean.
- **SmartHealthConnect archived** (2026-07-19). Its value was ported first: skills →
  CareAgents advisors, MCP-App views → engine-served pages. The Claude plugin skills stay
  installable, frozen at v1.2.0. CareAgents is now the declared consumer surface in
  `.health-context.yaml`.
- CI lints the whole repo (`ruff check .`), not an allowlist of directories.
- Playwright e2e un-broken on `main`: stale CTA locator fixed, an HTML report actually
  uploads on failure, and the port is overridable (`E2E_PORT`) for macOS AirPlay collisions.

### Fixed
- Stopped advertising an unimplemented "summary-only mode" privacy control (site copy +
  OpenClaw bot); a metadata-security test now guards against re-introducing it. Rule of the
  house: ship the mechanism, then the copy.
- Railway production deploys unblocked (single-element `preDeployCommand`; legacy
  `create_all` databases adopted by init-db).

## [1.8.0] — 2026-07-12 — Real-Actions Foundation + Reliability

*(Backfilled — this release shipped with GitHub release notes only.)*

### Added
- **Provably out-of-band human gate:** `commit` only *submits* an action (HTTP 202);
  execution happens exclusively through a separate approval endpoint requiring a single-use
  step-up credential and an expiry-guarded atomic claim. The spoofable `X-Human-Confirmed`
  header is gone at both the Flask and MCP layers.
- **`ActionExecutor` plugin registry** — add a real-world capability behind the full
  guardrail rail (validation → human gate → audit → reconciliation) in ~50 lines, no core
  changes. Mandatory red-flag emergency screen; fail-loud rails (no silent simulation).
- **Durable execution:** attempt ledger, provider `reconcile()`, external-tick reaper,
  append-only action-event log.
- **Reliability floor:** config preflight (`GET /r6/ops/preflight`), Postgres CI lane,
  MCP fetch timeouts, poller 409-storm detection, source-aware resource identity
  `(tenant, resource_type, id)`, Fasten hardening + zombie-job boot reaper.
- Public [ROADMAP.md](ROADMAP.md) (Now/Next/Later) + contributor on-ramp.

### Fixed
- Upstream FHIR errors surfaced through a PHI-safe sanitizer instead of collapsing into
  empty bundles / fake 404s (thanks @aanishs). Quality measures default to the current
  calendar year (thanks @leemeo3).

## [1.7.0] — 2026-07-08 — Own-Data Onboarding + Care Gaps + Rx Transfer

### Added
- **Preventive care-gaps engine** — `POST /r6/fhir/Patient/$care-gaps` + `care_gaps` MCP tool:
  seven USPSTF/ACIP/ADA-sourced rules (BP, lipids, diabetes A1c, colorectal/cervical/breast
  screening, flu) with per-rule `related_ecqm` crosswalk (CMS130/124/125/147/22/122), honest
  `indeterminate` for unknown age/sex, FHIR partial-birthDate support, SNOMED + ICD diabetes
  detection, future-dated records rejected. Deliberately a lightweight consumer variant — NOT the
  Da Vinci DEQM `$care-gaps` operation (the disclaimer says so).
- **Patient connect flow (own data, no portal account):** the identity-verified Fasten Stitch
  onboarding (`/connect/<tenant>`, CLEAR/ID.me via TEFCA) now ends with a one-time
  "Connect your AI assistant" card — a **read-scoped, 30-day patient connect token** minted only
  after Fasten's HMAC-signed `connection_success` webhook verifies the `org_connection_id`
  (`GET /fasten/connections/<id>/agent-access`; first-connection-only, mint-once, tenant-bound).
  Token scope claim: read-scoped tokens are rejected by every write path (H4 intact).
- **EHI export trigger** (`r6/fasten/api.py`): Fasten does not export records automatically —
  the server now requests `POST /v1/bridge/fhir/ehi-export` (idempotent) the moment a connection
  verifies. Requires `FASTEN_PRIVATE_KEY`.
- **Prescription transfer requests** — `rx_transfer_request` MCP tool (**29 tools**) +
  `POST /r6/actions/rx-transfer/propose`: assembles active medications and stages one
  human-confirmed phone call to the receiving pharmacy (how US transfers actually work).
  Schedule II is refused with an explanation (never transferable).
- **Per-agent quickstarts** (`docs/quickstarts/`): Claude (web/desktop/mobile), Perplexity,
  ChatGPT Developer Mode, Telegram (OpenClaw), generic MCP — plus a 10-minute demo script and
  the own-data onboarding walkthrough.
- **Health Bank One converter** (`scripts/convert_hbo_export.py`): HBO's columns/rows tables →
  FHIR R4, Patient redacted-by-construction; in-process redactor now scrubs PHI-bearing
  embedded XML tags in string payloads (`embedded_tags_masked`).
- **CMS-0057-F / Da Vinci DTR design doc** (`docs/design/cms-0057-prior-auth-dtr.md`).
- **Contract tests for live integration paths:** Fasten Standard-Webhooks signatures
  (valid/tampered/expired/fail-closed), `/shc/ingest` auth+shape, MEDENT/HBO OAuth broker
  round-trips, EHI export trigger, OpenClaw bot fixes.

### Changed
- **Fasten webhook envelope:** events nest fields under `data` — handlers now unwrap it
  (previously every live event was silently dropped with a 200).
- **Webhook verification is fail-closed:** unsigned events rejected unless
  `FASTEN_ALLOW_UNSIGNED_WEBHOOKS=true` (dev only).
- **Vercel serverless copy refuses stateful writes** (405 → app.healthclaw.io) — its SQLite is
  ephemeral, so accepted writes were silently lost.
- **Lab interpreter honesty:** panic thresholds are inclusive (K of exactly 6.5 → `HH`), and a
  one-sided lab reference range can no longer yield a false "normal" when the value crosses the
  population bound on the uncovered side (→ indeterminate).
- CSP now allows the Fasten Stitch widget + CLEAR/ID.me identity frames (`frame-src`);
  `frame-ancestors 'none'` unchanged.
- OpenClaw bot: server-mint bind fallback (no shared secret needed for public tenants);
  `/curatr` targets a real resource per the current tool schema.

## [1.6.0] — 2026-07-02 — Clinical Intelligence

_Retroactive entry (release shipped without a changelog update)._

### Added
- Lab reference-range interpreter (`Observation/$interpret`, `fhir_interpret_labs`) — LOINC/UCUM,
  resource-range-wins, indeterminate-over-false-normal.
- NQF 0018 / CMS165 quality-measure calculator (`Measure/$evaluate-measure`) — honestly scoped
  as a calculator, not a certified eCQM.
- Guardrail conformance harness (`GET /r6/fhir/$conformance`) grading deployments A–F; CI gate.
- Any-agent-framework adapters (OpenAI/Gemini), Medplum-in-front recipe, SMBP triage aligned to
  the 2025 AHA/ACC guideline, ruff lint gate, dependency advisories remediated.

## [1.5.0] — 2026-06-18 — Security Hardening + SDC Forms

### Added
- **HL7 SDC form round-trip.** `POST /r6/fhir/Questionnaire[/<id>]/$populate` pre-fills a
  `QuestionnaireResponse` from a subject; `POST /r6/fhir/QuestionnaireResponse/$extract` turns a
  completed response into a transaction `Bundle`.
  - `$populate` mechanisms: expression-based (`initialExpression` FHIRPath via `fhirpathpy`) and
    observation-based (`item.code` LOINC matched against the subject's Observations).
  - `$extract` mechanisms: observation-based (`observationExtract`) and definition-based
    (`definitionExtract` + item `definition` element paths). `?dryRun=true` previews the Bundle
    without committing.
  - Pure, Flask-free transform engines in `r6/sdc/` (`expressions.py`, `populate.py`, `extract.py`);
    the route layer owns auth, audit, step-up, and store I/O.
- **MCP tools** `questionnaire_populate` (read) and `questionnaire_extract` (write) — 23 tools total.
- **Seeded `healthclaw-intake` demo Questionnaire** showing the populate → complete → extract loop.
- **CI compliance gate `H-SDC`** asserting `$extract` requires a step-up token.
- Community scaffolding: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `LICENSE` (MIT).

### Changed
- **Reads of non-public tenants are now authenticated**, not just tenant-scoped: a bare
  `X-Tenant-Id` only works for `PUBLIC_TENANTS` or SHARP-on-MCP requests; every other tenant must
  present a tenant-bound `X-Step-Up-Token` or a matching SMART bearer, else `401`. The read-auth
  check was refactored into a reusable `authenticate_tenant_read` helper.
- `/metadata` CapabilityStatement now advertises the SMART OAuth service in its `rest.security` block.
- Dependency security bumps: PyJWT (CVE-2026-48526), npm advisories (form-data, minimatch).

### Security / compliance postures (deliberate, documented)
- `$populate` returns **unredacted** PHI by design — a form must hold real data, and the read-auth
  gate is the compensating control. An optional `?redaction=` opt-in is a tracked follow-up.
- `$extract` commit is treated as an ingest-class operation (like `Bundle/$ingest-context`):
  step-up + `$validate` gate the write; it is exempt from the per-resource `X-Human-Confirmed` gate.

## [1.4.0] — 2026-06-11 — Multi-Connector Health Data Pipeline

### Added
- Five distinct health-data pipelines wired in behind the guardrail stack, surfaced as Telegram
  slash commands: **Fasten TEFCA** (`/connect`), **HealthEx** (`/export`), **Health Bank One**
  (`/hbo-connect`, `/hbo-pull`), **Flexpa** (`/flexpa-connect`), **Health Skillz / Epic**
  (`/epic-connect`), and **MEDENT** (`/medent-connect`, `/medent-pull`).
- `/shc/ingest` SmartHealthConnect bridge endpoint; `/shc/medent/callback` OAuth broker.
- `scripts/medent_oauth.py` (SMART on FHIR DCR + PKCE) and `scripts/export_medent_fhir.py`.

## [1.3.0] — 2026-04-15 — Wearables

### Added
- Wearable device sync (Garmin, Oura, Polar, Suunto, Whoop, Fitbit, Strava, Ultrahuman) into FHIR
  Observations with LOINC/UCUM codes and device Provenance, via the Open Wearables sidecar.
- `r6/wearables/mapper.py`, a daemon poller through `/Bundle/$ingest-context`, the
  `wearables_sync_status` MCP tool, and a Connection Manager MCP App.

## [1.2.0] — 2026-04-15 — Compiled Truth

### Added
- `GET /<type>/<id>/$compiled-truth` and the `fhir_compiled_truth` MCP tool — current redacted
  resource + curation state + quality score + full Provenance timeline.
- Activated `curation_state` and `quality_score` on every resource; `.health-context.yaml`.

## [1.0.0] — 2026-03-28 — Curatr Data Quality Skills

### Added
- Curatr patient-owned data-quality engine: terminology checks against live APIs, patient-approved
  fixes with Provenance tracking, and the `curatr_evaluate` / `curatr_apply_fix` MCP tools.

[1.5.0]: https://github.com/aks129/HealthClawGuardrails/releases/tag/v1.5.0
[1.4.0]: https://github.com/aks129/HealthClawGuardrails/releases/tag/v1.4.0
[1.3.0]: https://github.com/aks129/HealthClawGuardrails/releases/tag/v1.3.0
[1.2.0]: https://github.com/aks129/HealthClawGuardrails/releases/tag/v1.2.0
[1.0.0]: https://github.com/aks129/HealthClawGuardrails/releases/tag/v1.0.0
