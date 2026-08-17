# PRD 5 — Clinical rails

> Owner brief: `.claude/agents/owner-clinical-rails.md` · Process:
> `docs/2026-08-16-delivery-process.md` · Topology:
> `docs/2026-08-16-system-topology.md`
>
> Measured 2026-08-16. A line that says *unmeasured* means nobody has run it,
> not that it is broken.

## 1. The problem, and whose it is

A patient's records contain the answer to *is anything overdue, and is anything wrong* — and nobody can get it out. The user is **the patient**, and the reviewer is **a clinician** who has to be willing to stand behind what was said.

## 2. What "works" means

> A clinician reads the output and does not have to correct it.

This set's end-user sign-off is a clinician's, and it is the gate. Two clinical rules stand behind it and are not negotiable: **"no known allergies" is never inferred**, and **"could not look" must never arrive as "looked and found none"** — a shape this codebase has now found five separate times.

## 3. How it is proven

- **Run log** — each rail against seeded personas *and* the owner's real records.
- **Recording** — captured with redaction on or scoped to the guardrailed view. A run that cannot be recorded PHI-free is still run; the register says why there is no video.
- **Register** — every rule whose cadence no clinician has passed on. A1c monitoring is patient-visible and unreviewed.
- **Sign-offs** — QA adversarial; **end-user is the physician advisor**.

## 4. Current state, measured

- **Unmeasured end to end.** No pack exists.
- Care gaps: subject resolution reports which resolution happened, and tombstones no longer count (#422, fixed today).
- The brief renders the producer's real output — two copies of one shape drifting is what made it render "an unreadable result" in production (#387/#435, fixed).
- The recording commitment lands around **Aug 25**; deploys freeze once it starts.

## 5. Known gaps — the open issues in this set

| # | Issue | Shape |
|---|---|---|
| 417 | care-gaps tells patients their DOB and sex are missing when they are on file, and hides partial results | **patient-visible** |
| 436 | an indeterminate screening gives the patient no line at all | **patient-visible** |
| 386 | AppointmentBrief registers at `/r6/fhir/fhir/` — the brief page has never populated in production | **never worked** |
| 226 | DocumentReferences are ingested and counted but no tool can read them | dead data |
| 310 | `/r6-dashboard` fabricates a 'Health data imported' animation on a real connection | **honesty** |
| 458 | `/curatr` grades one Observation, so it cannot see the duplicates it exists to catch | does not do its job |
| 368 | SDC populate copies full demographics into the draft QR | decide the bound |
| 64 | SDC `$populate`: optional `?redaction=<profile>` | follow-up |
| 53 | labs: broaden the analyte table | enhancement |
| 54 | labs: unit conversion | enhancement |
| 62 | labs: trend/delta interpretation | enhancement |
| 55 | quality: complete the CMS165v14 exclusion set | enhancement |
| 60 | SHL: QR rendering + link revocation | enhancement |
| 389 | $care-gaps answers every CareAgents patient with silence | verify closed — the fallback landed |

## 6. Specifications

- `r6/caregaps/` rule table — the cadences, in code.
- `r6/terminology.py` — labels are applied *after* redaction, keyed by code. An upstream `display` is never preserved.
- **Missing, and a SOW item:** a clinician-reviewed statement of which rules are released and which are not. #389 asked for exactly this in as many words: *"it wants CTO sign-off and clinical review, not an engineering judgement call."*
