# Sprint 2 retro and QA sign-off — "the gates"

Follows `docs/2026-08-05-sprint-1-retro.md`. Written before Sprint 3 lands.

## 1. What shipped

| PR | Issue | What it closed |
|---|---|---|
| #393 | #389 (half one) | `$care-gaps` answered every patient with silence |
| #394 | #305, #310a | The fabricated import animation, and the route behind it |
| #397 | #390 | "No allergies found" sat above the attestation that must never be inferred |

## 2. QA sign-off

**Conditional PASS on the three PRs. NO sign-off on the demo path.**

Signed off:

- Each fix is mutation-verified, several in both directions. #397's six
  mutations include one that makes the absence line *unreachable*, which
  guards against trading a false claim for no information — a fix that
  removes a wrong sentence and says nothing instead has not helped.
- #394 removed a route, its tests, its matrix row and a strict xfail in one
  change, and proved each with its own mutation.
- Full suite, ruff, mypy and table stakes clean on every branch. Conformance
  held Grade A.

Not signed off, and the distinction is the point:

- **#393 leaves the screening button non-functional.** It is now honest and
  still cannot answer its own question, because the evaluator still receives
  `patient=None`. That is stated at the top of the PR rather than buried, and
  it is a legitimate reason for the maintainer to hold it and ship both
  halves together after clinical review.
- **Nothing in Sprint 2 was exercised against a live pair.** #397's template
  branches are proven on the HealthClaw side only; the relay's tests fake the
  page with a one-line HTML string.
- **The demo path now has two open P0s of its own** (#395, #396), both on the
  page beat 7 depends on.

## 3. What Sprint 2 found

Three fixes, four new issues — two of them P0, and both security- or
credibility-critical rather than cosmetic:

- **#395 — the step-up token is rendered into the patient's browser** as a
  JS literal and served cross-origin from careagents.cloud, contradicting
  `careagents/healthclaw.py`'s documented claim that the browser never sees
  one. It is a write credential, it crosses an origin boundary, and on the
  relay path it is **not even used**.
- **#396 — the relayed page loads HealthClaw's nav and CSS** against
  careagents.cloud, which does not have them. An unstyled page carrying a
  nav bar of dead links, shown to a patient mid-approval.
- #391 — `r6/brief/routes.py` reads an attribute the model does not have, so
  any tenant with data returns 500.
- The care-gaps MCP App ignores the new resolution state.

## 4. Lessons

### 4.1 A ruling can collapse a P0 rather than schedule it

#305 was ranked P0 and scoped as an auth question. Product's #310 ruling —
the dashboard is out of the demo — turned it into a deletion, because the
route's only consumers were the animation's two `fetch` calls.

**Change:** before scoping security work on a feature surface, get the
product ruling on whether the surface survives. The cheapest fix for an
unauthenticated route is often that nothing needs to call it.

### 4.2 My third wrong claim of the night, and the most instructive

I briefed that leaving a strict xfail in place after its defect closes fails
loudly as `XPASS(strict)`. It does not, here: the route now 404s,
`404 ∉ (401, 403)`, so the test still *fails* and the xfail still catches it
— **the suite stays green while asserting a closed defect is open.** Quieter
than an XPASS, not louder.

**Change:** the rule is not "a strict xfail fails loudly when fixed". It is
"a strict xfail asserts a specific failure, and when the code changes shape
the assertion can keep passing for a new reason." Removing the row was right;
my reason for it was wrong.

### 4.3 The pattern held in a fourth and fifth place

`_indeterminate()`'s three-state shape has now been applied in #381
(care-gaps in the brief), #389 (care-gaps on the agent path), #390 (the
intake form) and #379 (medications). Four call sites, one pattern, one week.

**Change:** this is now enough evidence to solve it once at the boundary
rather than a fifth time at a call site. That is the CTO's type-level
proposal, deferred past the webinar — the deferral should be revisited on
Aug 19, not silently.

### 4.4 Agents keep correcting me on facts about our own system

Three tonight: `apply_redaction` does not mutate in place; `pool_pre_ping`
pings every checkout; a strict xfail does not always XPASS. Every one was a
claim I reasoned to rather than ran.

**Change:** already in the Sprint 1 retro as a rule. Recording that it
recurred *after* the rule was written, which means the rule needs a
mechanism, not another mention.

## 5. Carried into Sprint 3

Dispatched: #220 + #213 (report-success-on-failure, and a grade earnable with
the guardrails off), #341 + #375 (worker backoff per the approved design, and
the access log that eats retention), #378 + #294 (scorecard tenant discovery,
transport-failure coverage).

Held deliberately: the **brief cluster** — #386, #387, #391, #382 — as one
gated unit. Fixing any single one publishes a twelve-field leak in the same
deploy, and the redaction half needs two product decisions first (dosage
text, and the `effectiveDateTime` residue).

Also open and owner-gated: #395 and #396 need a scoping call, because the
review template is shared between the direct and relayed paths and only the
relayed one can be fixed by deletion.
