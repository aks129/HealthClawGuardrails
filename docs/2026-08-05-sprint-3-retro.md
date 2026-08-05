# Sprint 3 retro and QA sign-off — "structure and evidence"

Third and final cycle of the plan in `docs/2026-08-05-prioritized-backlog.md`.
Follows `docs/2026-08-05-sprint-1-retro.md` and `-sprint-2-retro.md`.

## 1. What shipped

| PR | Issues | What it closed |
|---|---|---|
| #399 | #341, #375 | Worker idle backoff (11x fewer claims); the access log that ate retention |
| #400 | #213, #220 | Five conformance properties made two-sided; two paths asserting an unobserved failure |
| #402 | #378, #294 | Scorecard tenant discovery; the transport seam characterized |

## 2. The result that reframes the project

PR #400 measured what the conformance grade was actually worth. Against the
**pre-change** harness:

- A deployment where **forged step-up tokens authorize clinical writes** scored
  **Grade A (7/7)**.
- A deployment whose search returns an empty Bundle scored **Grade A**.
- A deployment that refuses everything — 401s, 428s, 404s — scored 2/7, and
  the two it passed were `step_up_enforcement` and `human_in_the_loop`, the
  two properties closest to the product claim.

Five of seven properties were satisfiable by doing nothing, because every
check asserted that something bad was **absent**, and a system that does
nothing satisfies that harder.

The grade is the product claim. It is now two-sided on five properties, our
own deployment still scores A, and no existing check was weakened.

## 3. QA sign-off

**PASS on the three PRs. The demo-path position from Sprint 2 is unchanged —
still no sign-off.**

Signed off:

- 13/13 mutations red for #378; all five fix-groups flip their strict xfails
  for #294; four killed for #399; six behaviour-flip mutations for #400 with
  before/after grades recorded.
- #399 ran the design's own falsification **before** writing code and matched
  it (7.25 req/s measured against 7.2 predicted), so the cap arithmetic is
  measured rather than inherited.
- Presence freshness under backoff is proven over a 40-poll, 217-second ramp —
  more than three readiness windows — not merely touched once.
- 2525 passed across the branches; conformance Grade A intact; no production
  file touched by #402.

Not signed off, unchanged from Sprint 2:

- Nothing was exercised against a live HealthClaw + CareAgents pair.
- #393 still leaves the screening button honest and non-functional.
- The demo path carries #395 and #396, both open.
- **New:** #403 — a HealthClaw outage reaches the patient as "you have no
  records" and a spinner that never ends, on the connect screen.

## 4. Lessons

### 4.1 The tool built to detect the pattern contained the pattern

The `--list-tenants` draft counted `ingest not stranded` as an evaluated row.
That check passes on an empty tenant, so every wrong-tenant run had one PASS,
exited 0 with "all evaluable rows pass (1 checked)", and the new hint was
unreachable. Only driving the real CLI surfaced it.

**Change:** a tool whose job is to distinguish states must be run against the
state it is meant to detect, not only unit-tested. This is the same rule as
"drive the production call shape", applied to our own instruments.

### 4.2 A stale `.pyc` can report a mutation as surviving

One mutation reported STILL GREEN and it was a bytecode artifact, not a weak
test — `.pyc` files are keyed on (mtime, size), and back-to-back rewrites
inside one second can hand pytest a stale one.

**Change:** mutation checks are now the standard evidence in this repo, so
they need `PYTHONDONTWRITEBYTECODE=1`. A false "the mutation survived" reads
as a weak test and would send someone to strengthen a test that was fine.

### 4.3 A good test refuses a wrong fix

A first attempt at `fetch_appointment_brief` widened its `except` clause and
did not flip the test — because that method never raises; it returns the wrong
shape. The test declined to accept a fix that did not address the defect.

**Worth keeping as a criterion:** a test that only checks "the symptom went
away" would have accepted it.

### 4.4 Two stale issues in one week, both caught by the rule that was written this week

#221 and #220 were both fixed by `ccf82fe` on Aug 1. Sprint 1's retro added
"an issue older than a week gets `git log -S` against its claim before it is
assigned", and Sprint 3 used it. In both cases the *inverse* defect was live
and got fixed instead, so neither agent's time was wasted — but neither issue
should have been assigned as written.

## 5. The pattern, counted

One defect shape produced almost everything this week:

> A guardrail produced **nothing**, and the caller read that as **an answer**.

| Instance | Where |
|---|---|
| #376 | RxNorm lookup — three stacked faults, no drug names |
| #379, #388 | Four routes to "the source sent free text" |
| #381 | A crashed care-gaps engine as "no screenings due" |
| #389 | `$care-gaps` silence on the first screen |
| #390 | "No allergies found" above the attestation |
| #400 | Five conformance properties earnable by doing nothing |
| #403 | An outage as "you have no records" |

Seven, in one week, in seven different subsystems. `_indeterminate()`'s
three-state shape was applied at five call sites. That is the argument for
the CTO's type-level fix, deferred past Aug 18 — **revisit it on Aug 19, not
silently**.

## 6. Carried forward

Held as one gated unit: the brief cluster — #386, #387, #391, #382. Fixing any
one alone publishes a twelve-field leak in the same deploy, and the redaction
half needs two product decisions first (dosage text, the `effectiveDateTime`
residue).

Owner-gated: #393's hold-or-ship call; #395 and #396's scoping call (the
review template is shared, and only the relayed path is fixable by deletion);
#401's design (conformance cannot grade read auth); #403's seam fix.
