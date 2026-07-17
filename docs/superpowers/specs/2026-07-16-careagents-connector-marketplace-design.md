# CareAgents Connector Marketplace — Design

**Status:** design (approved direction; supersedes the ad-hoc sample+Fasten
connection step). **Date:** 2026-07-16.

**Goal:** Turn CareAgents' single connection step into a **universal front
door** — connect health records from *any* supported service, plus wearables,
through one pluggable menu. Every connector lands in the same guarded FHIR
tenant space; agents sit on top of that unified space, framework-agnostic.

**One-line architecture:** a **connector registry** where each connector is a
small descriptor + a `start(account, tenant)` that returns one of
`{connect_url}` (route the browser to HealthClaw's own wired page/OAuth),
`{import}` (paste/upload flow), or `{coming_soon, waitlist}`. HealthClaw keeps
the policy; CareAgents adds the menu.

---

## Why this shape

HealthClaw is *already* a multi-connector aggregation hub — the record sources
we want mostly exist server-side today, each normalizing into a tenant's
redacted/audited FHIR space:

| Source | HealthClaw surface | Connect flow readiness |
|---|---|---|
| Verified providers / TEFCA (Fasten) | `r6/fasten`, `GET /connect/<tenant>` (Stitch widget) | **Live** — routed today |
| Wearables (incl. **Apple Health / Health Connect**) | `r6/wearables` → Open Wearables sidecar (`/api/v1/*`) | **Live-able** (sidecar-gated) |
| SMART Health Links (Josh Mandel's world) | `r6/shc` (`/shc`) | Import-style (paste/scan) |
| HealthEx | audit-heuristic only; no server connect page | Coming soon |
| Health Bank One | MCP partner (auth handshake) | Coming soon |
| Direct EMR / raw FHIR (MEDENT-style) | FHIR ingest path | Import-style (upload) |

CareAgents today hardcodes only `sample` and `fasten`. The registry generalizes
that so a new source is one class, not UX surgery.

---

## Connector tiers (v1 — no dead ends)

Every tile reflects its *real* readiness. We never ship a button that leads
nowhere; not-yet-live sources are honest "coming soon" tiles with a one-tap
"notify me" that records intent.

**Live now**
- **Sample** — instant synthetic tenant (already built).
- **Verified providers (Fasten)** — routes to HealthClaw `GET /connect/<tenant>`
  (already built; the connect flow lives on HealthClaw where the verified key +
  HMAC webhook are).
- **Wearables (Open Wearables)** — OAuth per provider (Oura, Whoop, Garmin,
  Fitbit, Strava, …) via HealthClaw `/wearables/oauth/start`, gated per-provider
  on `{PROVIDER}_CLIENT_ID` being configured on the sidecar. CareAgents mints
  the tenant token and routes the browser to the kickoff URL; the pending card
  polls to active as samples land.
  - **Apple Health / Health Connect is included here, not a separate "coming
    soon" tile.** As of Open Wearables 0.6.3, the sidecar ingests Apple +
    Health Connect data (nap detection, glucose). Open Wearables owns the phone
    bridge; our connector already reads its `/api/v1/samples`. So Apple Health
    is a live path **whenever the sidecar has those providers enabled** — we
    build no native code. (Before 0.6.3 this was native-gated and correctly a
    "coming soon" tile; 0.6.3 promotes it.)

**Coming soon (honest tiles, "notify me" records intent)**
- **SMART Health Link import** — paste/scan a SHL; needs a small import flow
  over `r6/shc`.
- **Direct EMR / FHIR upload** — upload a FHIR bundle or SMART Health Card.
- **HealthEx** — needs a server-side connect page (none today).
- **Health Bank One** — partner auth handshake.

---

## Data model (careagents)

`Connection.kind` is already generic. Extend the connector metadata rather than
adding a new table:

- `Connection.connector` — stable connector id (`sample`, `fasten`,
  `wearable`, `shl`, `direct`, `healthex`, `hbo`).
- `Connection.meta` (JSON/text) — connector-specific detail, e.g.
  `{"provider": "oura"}` for a wearable, `{"org": "…"}` for Fasten.
- Reuse the existing `pending → active | error` status + the poll endpoint for
  every connector (Fasten and wearables already poll; import connectors flip to
  active on successful upload).

No PHI in careagents — a `Connection` stays a pointer to a HealthClaw tenant.

---

## Connector registry (careagents/connectors/)

```
careagents/connectors/
  __init__.py     REGISTRY: dict[id -> Connector]; register_all()
  base.py         Connector protocol + StartResult union
  sample.py       instant synthetic tenant
  fasten.py       -> {connect_url: healthclaw /connect/<tenant>}
  wearable.py     provider list + -> {connect_url: /wearables/oauth/start ...}
  shl.py          import-style (coming soon in v1)
  direct.py       FHIR upload (coming soon in v1)
  comingsoon.py   healthex, hbo — waitlist descriptors
```

`Connector` protocol:
- `id`, `label`, `blurb`, `icon`, `tier` (`live | import | soon`)
- `providers(cfg) -> list[dict] | None` — for multi-provider connectors
  (wearables); None otherwise.
- `start(cfg, client, account, tenant, **opts) -> StartResult` where
  `StartResult` is one of `Connect(url)`, `Import(kind)`, `Soon(waitlist=bool)`.
- `status(cfg, client, tenant) -> "pending"|"active"|"error"` (default polls
  `tenant_has_records`).

The hub renders the marketplace grid from `REGISTRY`; `POST /api/connections/
<connector>` dispatches to `start`. Adding a source = one file + one registry
line, no template change.

---

## Hub UX

Replace the two hardcoded buttons with a **marketplace grid**: live connectors
first (Sample, Verified providers, Wearables), then import tiles, then
"coming soon" with a subtle notify-me. Wearables expands to a provider picker
(Apple Health, Oura, Whoop, … — greyed with "needs setup" when the sidecar
lacks that provider's client id). Keeps the existing warm editorial look; the
onboarding gating from the accounts work (Step 1 = connect) still applies.

---

## Agents — explicitly Phase 2 (out of scope here)

Agents already sit on top of the unified tenant. "Agents on any framework"
(Vercel AI SDK / Claude Agent SDK / LangChain over the HealthClaw MCP, plus the
in-house loop) is a separate increment — see the Vercel-agents feasibility
brief. This spec is only the connection marketplace.

---

## Operational notes

- **Open Wearables sidecar (0.6.3):** `ACCESS_LOG_LEVEL` now defaults to a
  near-silent level when `ENVIRONMENT=production`. If we self-host the sidecar,
  set `ACCESS_LOG_LEVEL=all` (or `errors`) explicitly. Our client code is
  unaffected.
- **Naps:** `r6/wearables/mapper.py` maps `sleep_duration` (LOINC 93832-4) but
  not a nap distinction; 0.6.3 flags naps (Apple/Oura). Surfacing naps as a
  distinct FHIR observation is a small mapper follow-up (tracked separately).

---

## Acceptance criteria

1. A connector registry drives the hub; Sample/Fasten/Wearables behave exactly
   as today through it (no regression), with per-provider wearable gating.
2. Apple Health appears as a live wearable provider when the sidecar has it
   enabled; greyed with a clear reason when not.
3. "Coming soon" tiles never dead-end: they record notify-me intent and say so.
4. Every connector's records land in the tenant's guarded FHIR space; careagents
   stores only the pointer. No PHI in careagents.
5. Adding a new connector requires one `connectors/*.py` + one registry entry,
   no template edits — proven by adding one in a test.
