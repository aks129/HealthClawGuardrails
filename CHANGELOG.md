# Changelog

All notable changes to HealthClaw Guardrails are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **License: MIT → FSL-1.1-MIT (Fair Source).** Effective from the next release: free for
  everything — internal use at any organization (including commercial ones), education, research,
  building products ON TOP of HealthClaw — except offering HealthClaw itself (or a substitute) as
  a competing commercial product. Each release automatically becomes MIT two years after
  publication. Versions **v1.7.0 and earlier remain MIT** (`LICENSE-MIT-v1.7-and-earlier`).
  Enterprise capabilities beyond the core are offered commercially (open core) — see
  `COMMERCIAL-LICENSE.md`. Contributions now use DCO sign-off instead of no-CLA.

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
