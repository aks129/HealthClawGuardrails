# Ecosystem contribution plan — visibility through substance

**Goal:** put HealthClaw's name into the projects our future partners and users
already read, by contributing things *only we can credibly contribute* — real
fixes, real implementer feedback, real production-usage reports. No drive-by
listings without substance behind them.

## LIVE STATUS (2026-07-05)

| Touchpoint | Type | State |
| --- | --- | --- |
| MCP Registry `io.github.aks129/healthclaw-guardrails` | listing | ✅ published + verified |
| Gemini CLI extension (`gemini-extension.json`) | self-serve | ✅ live in repo, one-command install |
| medplum/medplum #9746 (bot subscription AuditEvents) | PR | 🕓 open, CI clean, no maintainer reply |
| medplum/medplum #9616 (MCP search/fetch) | comment+offer | 🕓 open, deferred to @aanishs |
| jmandel/kill-the-clipboard #3 (packageable lib) | PR | 🕓 open, no reply |
| jmandel/shlep #1 (early-adopter integration report) | issue | 🕓 open (we are issue #1), no reply |
| fastenhealth/fasten-onprem #207 (export mapping) | comment | 🕓 open, no reply |
| the-momentum/open-wearables #1264 (.env drift) | PR | 🕓 open, CodeRabbit "no actionable comments" |
| kakoni/awesome-healthcare #156 | listing PR | 🕓 open |
| rdmgator12/awesome-healthcare-mcp-servers #5 | listing PR | 🕓 open |
| **HL7 Jira FHIR-57806** (SDC definition-based extraction feedback) | standards-body issue | ✅ submitted |

**10 external touchpoints across 7 orgs in ~24h. PACING RULE NOW BINDS:** do NOT
open new external contributions into cold trackers until some of these get a
maintainer response — first responsiveness converts a PR into a relationship;
saturation reads as spam. Next external moves (Tuva BP-measure, wso2 design-input
issue, Medplum #8812 PHI-in-logs) are PREPPED-when-ready, fired on a response
signal or after a few days of quiet, one per day max.

**Aggregators** (Glama/PulseMCP/Smithery) have not yet crawled the registry entry
— re-check; manual submit only if still absent after ~a week.

**House rules for every external contribution:**

- Substance first. If we wouldn't want the PR without the visibility, don't send it.
- Disclose plainly: "we build HealthClaw Guardrails and hit this integrating X."
- Issue-first when the repo has no existing ask; PR-first when an open issue invites it.
- Medplum requires **DCO** (`Signed-off-by:` every commit). Tuva/Fasten/jmandel: none found.
- Frame peer-project input (wso2, the-momentum) as design input, never as promotion.

---

## Wave 1 — this week (small, high-certainty, compounding)

| # | Target | Contribution | Effort | Why us |
| --- | --- | --- | --- | --- |
| 1 | **Official MCP Registry** (registry.modelcontextprotocol.io) | Publish `server.json` via `mcp-publisher` (namespace `io.github.aks129`). Zero healthcare-guardrail servers listed today; "fhir" returns only a reference-data server. Self-serve — no gatekeeper. | S | Our own artifact; instant discoverability in every registry-consuming client |
| 2 | **jmandel/kill-the-clipboard-skill** — PR: make `lib/` consumable without vendoring | ESM `.js` import extensions + minimal `package.json` exports map. We vendored `encoding/hkdf/jwe/shlink` at `fa0020d` *because* it isn't packaged; our local import-extension diff is the PR. Zero open issues; Josh merges external PRs. | S–M | We run his server in production and vendored his crypto. Josh Mandel = SMART on FHIR lead — highest-leverage merged PR available |
| 3 | **medplum/medplum #9385** — Bot subscriptions emit no AuditEvents | Small fix + tests (`execBot` never calls `createSubscriptionAuditEvent`; root cause already pinpointed in the issue). DCO sign-off. | S | Immutable audit is our house rule; "agent action must leave a trail" is our exact thesis |
| 4 | **kakoni/awesome-healthcare** (3.8k★, actively merging) + **rdmgator12/awesome-healthcare-mcp-servers** (fresh, compliance-tier taxonomy) | One-line listing PRs. Legitimate: kakoni merged a comparable OpenWearables listing in Jan; the MCP list already rates servers on HIPAA-guardrail tiers — our category. | S | — |

## Wave 2 — anchor contributions (the ones people remember)

| # | Target | Contribution | Effort | Why us |
| --- | --- | --- | --- | --- |
| 5 | **medplum/medplum #9616** — MCP `search`/`fetch` are dummy stubs | Implement real, token-bounded search/fetch for their MCP server. Their AI docs' thesis is literally ours ("AI must operate within explicit guardrails") and their MCP surface is what every AI-curious Medplum user touches. | M–L | Our 24-tool production MCP server does exactly this; we've already tested against Medplum's API |
| 6 | **the-momentum/open-wearables** (2.0k★, very active) | (a) PR for good-first-issue **#1222** (.env.example drift); (b) real-usage bug reports from our prod deployment (their tracker values these — several `needs-verification`); (c) well-specified issue proposing FHIR R4 Observation export, citing our working LOINC mapping as prior art. | S→L | We run it in production docker-compose — strongest credibility on this list |
| 7 | **jmandel/shlep** (brand-new successor to the SHL storage server — blind, revocable SHLs on object storage) | Early-adopter integration report as a substantive issue (+ small deployment-recipe PR). Repos this young remember their first outside contributor. | S | Adopter #1 standing with the standards community's most-watched author |
| 8 | **fastenhealth/fasten-onprem #207** — Patient/$everything + $export ask | Comment first with the resource-mapping knowledge from `scripts/convert_fasten.py` (where Fasten's export diverges from FHIR); PR the export path (Go) only after maintainer engagement. They merged an outsider's IPS-export PR — precedent exists. Alt venue if quiet: `fasten-toolbox` (more active). | M–L | We maintain the converter their users need; partner-friendly founder |

## Wave 3 — strategic / slower-burn

| # | Target | Contribution | Effort |
| --- | --- | --- | --- |
| 9 | **tuva-health/tuva** — BP-control measure is missing from the quality_measures mart (existed once, absent from main; only 8 measures ship, no CBP) | Issue first (why was it dropped?), then dbt intermediate models for NQF 0018/CMS165 — we've already worked out the exclusion logic and 140/90-vs-130/80 nuances. Also #792 (panel observation linking = our SMBP/labs domain) and #1334 (trivial NPPES door-opener). | M–L |
| 10 | **medplum/medplum #8812** (PHI in structured logs) + **#8508** ("Medplum.md for AI agents", unclaimed since Feb) + a guardrailed-agent docs page under their `docs/ai/` — our Medplum recipe, generalized | S–M each |
| 11 | **wso2/fhir-mcp-server** (126★, near-empty tracker) | Substantive design-input issue: "PHI redaction / audit layer for agent-mediated FHIR access" — threat model + the pattern we use. Corporate CLA makes code PRs uncertain; the conversation is the win. | S–M |
| 12 | **HL7 SDC IG** — implementer feedback on `$populate`/`$extract` via **HL7 Jira** (GitHub issues disabled) + `#questionnaire` on chat.fhir.org | Implementer feedback is how you get named in IG credits. | S–M |

**Skipped (researched, not worth it):** Flexpa (MCP repo no longer public, org quiet — issue-first on `quickstart` only if they engage elsewhere), HAPI FHIR (no guardrails-shaped open issues), smart-on-fhir org (maintenance mode), fhir-fuel/awesome-FHIR + Cicatriiz lists (dormant).

---

## Internal follow-ups surfaced by this research (our repo)

1. **ktc vendored code drifted from upstream** — 4 commits since `fa0020d`: JWE compression default flipped to *uncompressed* (`zip: DEF` now opt-in — check `shl_generate`'s default posture), first-class never-expiring links (type contract changed), unauthenticated `/health` endpoint.
2. **Use upstream `/health` for the `shl-server` docker-compose healthcheck.**
3. **QR rendering + revocation already exist upstream** — our CLAUDE.md "PLANNED" items are *adoption* tasks (sync/wire), not build tasks. Update CLAUDE.md when adopted.

## Cadence

One wave-1 item per day this week; hold each wave-2 anchor until the preceding
touch in that community lands (e.g., open-wearables bug reports before the FHIR
proposal; Fasten comment before the export PR). Track everything in a
`community` label on our own tracker. Every merged PR gets one line in the next
release notes — visibility compounds both directions.
