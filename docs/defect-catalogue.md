# Defect catalogue — the shapes that keep coming back

**Status: the review instrument.** `.github/REVIEW_STANDARDS.md` §27 requires
every PR to be read against this file. It is not history for its own sake; it
is the list of mistakes this codebase has already paid for, written so the
same mistake is recognisable the next time it arrives wearing different code.

Every entry carries **evidence** — a PR, a date, and what it actually cost. An
entry with no evidence does not belong here, because a catalogue of imagined
failures trains a reviewer to look in the wrong place.

Read order for a reviewer: §0 first (it is the one that catches the others),
then scan the shape headings against the diff.

---

## 0. The reviewer's own failure mode, first

Most of this repository's code is written by an AI agent, and so is most of
its review. That makes one class of error structural rather than incidental:
**a confident report from a check that did not run.** It has produced wrong
answers here more often than any bug in the product.

Observed, all in this repository:

| what was reported | what was true |
|---|---|
| `table stakes: clean` | the tool examined **zero** files; the diff range was empty. CI then found 3 real findings |
| "backup complete" | the backup file was **empty** — Python's cert store had failed and every request errored |
| "nothing is running on the mini" | `ps` was returning **782 processes**; the grep was mis-quoted |
| "DNS has not propagated" (×2) | the LAN forges port-53 answers. One nearly became a support ticket against a blameless provider |
| "seeded on every deploy" | seeding had not run since **2026-07-08**; the duplicates predated that |
| "5 of 5 mutations red" | two of the five were **green**, and the guards were blind |

**The rule this produces:** a check must prove it examined something before it
is allowed to report success. "Found nothing" and "looked at nothing" print the
same word unless you make them print different ones.

```python
if checked == 0:
    print("NOTHING TO CHECK — this is not a pass.")
    return 0
print(f"clean ({checked} file(s) checked)")
```

**Reviewer question:** for every new guard, scan, or verification step in this
PR — if it silently examined nothing, would it say so? If not, that is a
finding, not a nit.

---

## 1. A reassuring word doing a check's job

**The most frequent shape in this codebase.** Prose asserts a property; nothing
enforces it; the word is what people read.

| evidence | the word | the truth |
|---|---|---|
| #456 | `"passed": true` | printed beside `"detail": "PHI leaked into audit"` on 15 checks. Two independent readers concluded the deployment was leaking. The second was the physician advisor, ~30 hours before a launch recording |
| #457 | `railway.toml`: "a separate **idempotent** pre-deploy command" | the seed inserted unconditionally. Production reached **19 Patients** against a seed set of one |
| #464 | docstring: "**Idempotent** — re-seeding appends new resources (IDs generated fresh each call)" | the guarantee and its violation in a single sentence. Nobody read past the first word |
| #465 | response note: "Re-seed anytime to **add more resources**" | still shipping to live callers a day after the docstring above it was corrected |
| #471 | "these cap memory and keep a long-running process from degrading (#218)" | above two constants nothing reads. The bound does not exist |
| #460 | `"Structural validation passed"` | two fields checked (`status`, `code`). No date, category, performer or subject |

**Reviewer question:** does this PR add a comment, docstring, response field or
config note asserting *idempotent, always, never, validated, safe, sanitized,
isolated, audited*? If yes: which test fails when it stops being true? No test,
no word.

---

## 2. A control that looks like one thing and quietly does two

The founding shape (`docs/2026-08-02-retro.md`) — six defects in one week.

The 08-02 audit found the cause: the four guarantees are **per-route
conventions, not enforced invariants**. `r6/routes.py` alone held 32 tenant
reads with four defaulting strategies, 41 audit calls with two transaction
semantics, 7 step-up gates answering 401 at four sites and 403 at three.

Recent instances:

- **#450/#326** — the connect page branched on `widget.error` and `error`.
  Fasten emits neither. Every configuration refusal fell through both branches
  and produced nothing, and the page read that as *no news*.
- **#466** — the approval page rendered `res.b.error || 'Submission rejected.'`
  The 502 body carries `message`, not `error`. So `confirmed: null` — the one
  outcome two issues had been spent modelling honestly — reached the patient
  as a definite rejection, inviting the double-send #416 exists to prevent.

**Reviewer question:** does this control have one job, or does its behaviour
depend on a field, flag or event name it does not itself define? If the latter,
where is the test that the name is still right?

---

## 3. Two owners for one fact

Two places state the same thing; one goes stale; the stale one is usually the
one users read.

- The CSP was defined in `app.py` and described in `design.md` (#453 — resolved
  by making `app.py` the only definition and having the doc point at it).
- The landing page claimed 1,665 tests while the suite passed 2,780, **and a
  card on the same page said "950+"**. A reader who sees two totals in one
  scroll stops believing every other number on it.
- #464 → #465: fixing the docstring left the endpoint's response note stating
  the opposite. Fixing one owner moved the wrong claim rather than removing it.

**Reviewer question:** is any fact in this diff also stated somewhere else?
Derive it, or add the drift test. `tests/test_site_version_sync.py` and
`tests/test_ratchets.py` are the existing pattern.

---

## 4. Guards blind to their own subject

**AI-specific and very common.** A guard is written alongside an explanation of
the bug, and the explanation contains the pattern the guard searches for — so
the guard matches its own prose and passes forever.

Four instances on **2026-08-10**, three of them inside #466 and one in #465:

- **#466** — a test banning the string `'Submission rejected.'` went green
  because the comment *explaining why it was removed* quoted it.
- **#466** — a guard asserting `res.b.message` appears in the failure branch
  passed when that branch was reverted, because `res.b.message` also appears in
  a different branch. Presence anywhere proved nothing about the branch that
  mattered.
- **#466** — a guard for `confirmed === null` matched the **comment**
  describing the branch, not the branch.
- **#465** — a guard banning the pre-fix wording searched for one exact phrase,
  so the endpoint's *response note* kept shipping the same claim for a day
  after the docstring above it was corrected.
- **#469** — a mutation harness reported two guards as blind when the real
  defect was that its own test list omitted the file holding the new pins.

Related: a check whose word list was too blunt flagged the check *named* "the
upstream display did not survive" — where "did not" is the desired outcome.

**Reviewer question:** does the guard read *code* or *text*? Was it
mutation-tested — actually run against the reverted behaviour — or only
observed to pass?

---

## 5. Silence that cannot be told from success

An absent signal and a healthy one look identical to everyone downstream.

- **#457** — `openclaw/bot.py` registered one `MessageHandler`, on
  `filters.COMMAND`. A typed sentence reached nothing. The physician advisor
  read the silence as a phrasing problem and spent her testing time guessing at
  wording. Silence cannot tell you which.
- **#462** — the Fasten refusal payload went to `console.log(...).slice(0,500)`
  and nowhere else. When the vendor asked for a request id on an open support
  thread, there was **no server-side record that any connect had ever failed**.
- **#470** — three step-up-gated writes emitted zero AuditEvents. Privilege
  without evidence.

**Reviewer question:** if this path fails at 3am, what does the system *say*,
and to whom? If the answer is "nothing", that is the finding.

---

## 6. Claims the code did not earn

Output asserts an outcome the system did not observe. Worse than silence,
because it is actioned.

- **#466** — `'Submission rejected.'` for an approval that may already have
  executed.
- Caught in review of my own work in the same PR: *"and nothing was sent
  twice"* rendered after a status of `completed` — which says the action
  completed, not how many times it ran. And *"Nothing was sent."* in a fallback
  that fires precisely when the server said nothing at all.

**Reviewer question:** for each user-facing string added, what observation
licenses it? An error path that fires when the server was silent must not
describe what the server did.

---

## 7. A characterization pin that locks in the defect

The pin is right about the mechanism and wrong about the consequence, so it
holds the bug in place and makes the cost invisible.

- **#457** — `test_seeded_patient_still_gets_generated_id` asserted the Patient
  keeps a generated UUID, with the reason "the fix must not force ids onto
  id-less resources". Correct about the mechanism. That UUID is exactly why
  every deploy created a new patient.

The repo rule (`docs/agent-task-guide.md` §6) is that **a fix PR moves its own
pin in the same PR, with the reason recorded**. Both halves matter: moving it
silently is how behaviour drifts; not moving it is how a fix gets reverted by
CI and abandoned.

**Reviewer question:** does this PR change a pinned behaviour? Is the pin moved
here, and does the new comment say why the old one was wrong?

---

## 8. Prose that contradicts the code beside it

- **#469** — `_require_step_up`'s docstring described a *"tenant-bound"* token
  while `validate_step_up_token`'s `require_scope` default made it demand a
  **write-capable** one. The code had been stricter than its description for as
  long as it existed. A reader trusting the docstring would have widened the
  gate believing it was a no-op.

**Reviewer question:** does the docstring describe what the function does, or
what someone once intended? Check defaults — the gap usually hides in an
argument nobody passes.

---

## 9. Cross-module coupling through private symbols

- **#468** — `r6/actions/review.py` imports `_tenant_or_none` out of
  `r6/actions/routes.py`. Changing that helper's return type turned **22 tests
  red**, in a module the PR never mentioned.

The 08-02 audit inventoried 34 such imports plus 17 module-path patches in the
suite. They are the reason a "small" refactor is never small here.

**Reviewer question:** does this PR change the signature or return type of any
underscore-prefixed function? Grep for importers before approving.

---

## 10. Deployment truth is not code truth

The repository describes a system; the running system is a separate fact, and
nothing was reconciling them.

- `railway.toml` documents a pre-deploy seed. Its **last audit event is dated
  2026-08-10's investigation: 2026-07-08** — the step has not run in a month
  while the file kept describing it.
- CareAgents silently drifted **13 commits** behind `main` because it does not
  auto-deploy.
- The Telegram bot answered a physician advisor on Aug 9; on Aug 10 its Railway
  service had **no active deployment** and the Mac mini was idle. Where it runs
  could not be established from the repository at all.
- The 2026-08-06 outage (`docs/2026-08-06-two-generators-three-laws.md`) is the
  full version of this shape.

**Reviewer question:** does this PR change something whose *deployed* state can
diverge from `main`? Is there a check that would notice?

---

## 11. Over-building ahead of a caller

From the 08-05 pattern review: *complexity is justified only by a caller that
exists or a risk that is live.*

- `r6/access.py` shipped **11 primitives, 8 with zero callers** — a second
  implementation of every guarantee, which is risk, not safety. The fix is
  adoption, not redesign.
- `r6/curatr.py` — a rich evaluation-and-fix engine whose apply-fix path is
  unreachable in production (#413).
- #471 deleted 293 lines of code documenting promises nothing kept.

**Reviewer question:** what calls this today? If the answer is "nothing yet",
does the PR say when, and is the interface smaller than the thing it hides?

---

## 12. A control that cannot work where it is served

**Shape.** A page ships an interactive control whose backend the serving host
refuses. The control renders, looks live, and fails only when someone presses
it. Nothing in the test suite presses it, so the suite stays green.

**Evidence.**

| What | Where | Cost |
|---|---|---|
| /r6-dashboard shipped 15 panels of buttons POSTing to `/r6/…`; healthclaw.io runs on Vercel, where `api/index.py` answers every mutating request to a stateful path with 405. The flagship "Run 6-Step Guardrail Demo" POSTed to `/r6/demo/agent-loop`. | 2026-08-10, fixed in the dashboard rebuild | Every interactive control on the most-linked public page was dead for anyone who clicked it. Eleven e2e tests and four unit tests asserted the panels were *present* and passed throughout. |

**Why the tests did not catch it.** `expect(page.locator('#patient-panel')).toBeVisible()`
asserts an element exists. Existence and function are different claims, and a
suite that only ever makes the first one will pass through a page of dead
buttons. The e2e suite ran against a local server *with* a database, so even a
test that had clicked would have passed — the failure only exists on the
deployment the public actually reaches.

**Guard.** A control is rendered only where its backend works. On /r6-dashboard
the re-run link is inside `{% if writes_here %}`, and
`tests/test_dashboard_reports_what_it_measured.py::TestNoDeadControls` asserts
both halves: no control at all on the read-only host, and no `<button>`,
`onclick` or `fetch(` anywhere in the page body. Where a host genuinely cannot
do the thing, say so in prose instead of offering a button that will 405.

## How to use this in review

1. Read §0. Ask what in the PR *claims* to have been checked.
2. Scan the shape headings against the diff — most PRs touch one or two.
3. For every new guard: was it mutation-tested? (constitution rule 20)
4. For every new sentence in prose, a comment, or user-facing output: what
   makes it true, and what makes it *stay* true?

A finding from this catalogue is not a style note. Every entry here has already
cost this project a day, a wrong answer to a partner, or a defect in front of a
clinician.

## Adding an entry

When a defect is fixed, ask whether it is an instance of a shape above. If it
is, add the evidence row. If it is not, add a shape — with its PR, its date and
its cost. A catalogue that grows only by category is how it stops being read.
