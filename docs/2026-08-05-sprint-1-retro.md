# Sprint 1 retro — "nothing is not an answer"

Aug 4 evening through Aug 5 early morning. Follows
`docs/2026-08-05-prioritized-backlog.md`. Written before Sprint 2 lands so the
lessons shape it rather than describe it afterwards.

## 1. What shipped

| PR | Issue | What it closed |
|---|---|---|
| #379 | — | The false "source sent free text" sentence, three of four routes; first positive check in the conformance harness |
| #384 | #262, #221 | The resend cooldown reported a send that never happened; pool settings pinned |
| #385 | #381 | A crashed care-gaps engine rendered as "no screenings due" |
| #388 | — | The fourth route into the same false sentence |

## 2. What the sprint actually found

The fixes are the smaller half. Sprint 1 opened six issues, four of them from
work that was not looking for them:

- #386 — the AppointmentBrief endpoint registers at `/r6/fhir/fhir/…` while
  its only client requests `/r6/fhir/…`. **That page has never populated in
  production.**
- #387 — the brief's care-gaps section reads a `due` key its producer never
  emits.
- #389 — `$care-gaps` is called with no subject and no fallback, so "Any
  screenings I'm due for?" always reports nothing due.
- #390 — the intake form claims "No allergies found in your records" directly
  above the no-known-allergies attestation.
- #380 — a CREATE response echoes the caller's upstream display unredacted.
- #221 — stale: the fix had been on `main` since Aug 1. Nothing pinned it.

Two of those (#389, #390) are P0 and were found by Product reviewing the demo
path, not by anyone reading the code they live in.

## 3. Lessons, each with what it changes

### 3.1 Enumerating cases is how you miss the fourth one

#379 closed the routes I had listed. #388 closed the one I had not. #376 had
three stacked faults for the same reason: each fix addressed a named cause
rather than the property.

**Change:** a fix for a "this state was mis-read" defect must assert the
property at the boundary, not enumerate the states that reach it. Where an
enumeration is unavoidable, the test drives the *production* call shape — the
shape every existing `$care-gaps` test omitted, which is exactly why #389
survived.

### 3.2 Tests were green over a hole, and the hole made them greener

Every guardrail check in `probe_phi_redaction` asserted a bad string was
ABSENT. A broken labeller passes those harder, because it shrinks the
response. The RxNorm lookup returned `None` for its entire life while the
conformance grade held A.

**Change:** landed in #379 — the first positive check in that file, as a pair
(a recognised code IS relabelled, AND the upstream display did NOT survive).
Neither half is sufficient alone.

### 3.3 A fixture can encode a contract that never existed

`tests/test_brief_engine.py` built `{"consumer": {"due": items}}`, a shape the
real producer never emits, and passed for as long as it has existed. Same
shape as #376, where every RxNorm test asserted validity and none asserted a
label.

**Change:** when a test constructs the input its subject consumes, one test in
that file must obtain the input from the real producer instead.

### 3.4 Two agents corrected me on facts about our own system, and both were right

- `apply_redaction` does not rewrite in place; it deep-copies via a JSON
  round-trip. My #365 comment said the opposite and was in the repo.
- `pool_pre_ping` pings on *every* checkout, so `pool_recycle` does not reduce
  the ping count. My brief said it did.

**Change:** both corrections are now in the code rather than my wrong
versions. The general rule: a claim about our own behaviour goes in a comment
only after it has been run, not after it has been reasoned about.

### 3.5 A briefing omission published sensitive material to a public repo

The CTO agent wrote a security-posture section into `docs/` and pushed it. The
repo is public. The branch was deleted from the remote and the findings kept
out of git.

**Change:** every brief that asks for written output now states the
destination's visibility. Security findings go to `private/` or a non-public
channel, never `docs/`. This was my omission, not the agent's error.

### 3.6 Stale issues cost real time

#221's fix had been on `main` for four days. The sprint spent an agent on it.

**Change:** an issue older than a week gets `git log -S` against its claim
before it is assigned.

## 4. QA position

Sprint 1's fixes are individually mutation-verified and the suite is green at
2482-2496 across the branches. That is not sign-off on the *product*, and the
distinction matters:

- **Verified:** each fix fails when its guard is removed. Full suite, ruff,
  mypy, table stakes clean on every branch. Conformance Grade A, now with one
  two-sided check.
- **Not verified:** nothing in Sprint 1 was exercised against a live
  HealthClaw + CareAgents pair. CareAgents tests fake the HealthClaw client,
  so they prove a call is made, not accepted — the documented repo trap.
- **Known broken and not addressed:** #386 (brief unreachable), #387
  (care-gaps `due` key), #389, #390, #391. Three are P0 and two are Sprint
  2's first items.

QA returned **FAIL** on the brief path (#382), measured rather than read:
twelve upstream free-text fields reach the response body, and the leak is
latent only because the page has never worked. Two blockers under it, both
verified against a running engine — the route sits one segment too deep
(#386), and `_resources_for` reads an attribute the model does not have, so
any tenant with data returns 500 (#391). Fixing either without the redaction
publishes the leak in the same deploy.

The "would a redact-only fix say Unknown?" question was answered by running
the candidate fix: yes, for every code outside the static table, which has
zero SNOMED entries and covered 1 of 15 ICD-10 codes on the live import. It
also deletes dosage text and leaves a positional `effectiveDateTime` residue.
So the brief fix needs two product decisions before any code lands.

**QA does not sign off on the demo path.** Product's stated biggest risk —
that the intake beat has never been run on the path a phone uses — remains
open, and #390 shows that path already carries an absence claim aimed at the
control the beat exists to showcase.

## 5. Carried into Sprint 2

Ordered by what a patient hits soonest:

1. #389 — `$care-gaps` silence (first screen, four buttons)
2. #390 — the intake absence claim (demo beat 7)
3. #305 + #310a — delete the fabricated import animation, which Product's
   ruling collapsed from a gate decision into a two-line deletion
4. #220, #213 — report-success-on-failure, and a grade earnable with the
   guardrails off

#386 and #387 are deliberately NOT in Sprint 2. Fixing the route turns on a
patient-facing page that has never run; it needs its own gate and a real
tenant to read it against.
