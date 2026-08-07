# Why it keeps breaking: two generators, three laws

Written at the end of 2026-08-06: eight behaviour PRs merged, one production
incident caused by the person writing this document, and a conformance grader
caught awarding its highest grade to an answer that contained nothing (#443).

The 2026-08-02 retro named the defect species. The 2026-08-05 playbook planned
the structural work. The 2026-08-06 retro counted fifteen instances in nine
days. This document asks the question those three left open: **why does a team
that can name the defect keep shipping it, and what is the smallest system
under which shipping it stops being easy?**

One uncomfortable fact frames everything below. This repo now holds 30
documents in `docs/`, 19 of them process docs, retros, reviews, and rulings.
Three of them correctly name the defect pattern. The pattern recurred anyway,
including in the hands of the person who wrote the retros, on the same night
one was committed. **Naming a failure mode in prose does not prevent it,
because prose does not gate anything.** The one countermeasure that worked on
its first day was not prose: `tests/test_ratchets.py` turned an architecture
rule into a red build, and `_ROUTES_IMPORTERS` went 8 to 4 in the same PR that
lowered the pin. That asymmetry — three retros ignored, one ratchet obeyed —
is the whole argument of this document.

## 1. The evidence, laid flat

Every row is from the last nine days. None is hypothetical.

| layer | the nothing that was produced | what read it as an answer |
|-------|-------------------------------|---------------------------|
| code | HealthClaw unreachable during a records fetch | "Not available from your connected records" (#403, #409, #430) |
| code | care-gaps got no subject to evaluate | "no screenings due", to every patient (#389, #393) |
| code | stale step-up token; conversations returned 401 | an empty chat history; "this form is no longer awaiting review" (#434) |
| code | colorectal rule cannot read FIT results | "you are due for screening", to a screened patient (#425, #428) |
| code | AttributeError before redaction could run | a 500 that was, unnoticed, the only thing preventing a PHI leak (#391, #382) |
| verifier | conformance read returned no Patient | grade A, with "passes trivially on an empty response" in its own evidence (#443) |
| verifier | brief tests asserted their own URL constant | a green suite over a route no client could reach (#386) |
| verifier | mutation harness compared a resolved path to a bare name | a correct Dockerfile failed; its mutation passed (08-06 retro §3) |
| infra | worker health unreachable behind a blocked edge | healthz published `run_workers: false`; the incident read "workers down" (#410) |
| infra | prod-watch check titled "db reachable" | FAIL, while its own detail line printed `accounts=True` |
| infra | a Vercel domain list that omitted the hostname the project served | the serving project was deleted; production went down (08-06 incident) |
| process | six of nine CI jobs not required to merge | the Grade A gate started 95 seconds *after* #431 merged |
| process | merges made with `GITHUB_TOKEN` trigger no push CI | main's HEAD carried no full-suite run at all |
| operator | "I cannot explain how Vercel is serving this hostname" — in the session notes | the project was deleted anyway, hours later |

The species does not care which layer it lives in. The instrument that grades
the product, the monitor that watches it, the settings that gate merges, and
the operator running the session all produced the same failure as the code
they were guarding.

## 2. Two generators, not twenty defects

### Generator A: state collapse at a boundary

Reality at any boundary has at least four states: *answered with data*,
*answered empty*, *could not ask*, *never ran*. Most of our representations
carry two: a bool, a 200-or-not, a present-or-absent key, an empty list.
Whatever reality the representation cannot carry gets collapsed into what it
can — and the collapse always lands on the confident side, because the
confident values are the ones the happy path defined first.

### Generator B: the same fact stored twice

Contradiction is not a mystery. It is duplication plus time. Every
contradiction this week was two copies of one fact drifting:

- The brief's tests held their own copy of the URL; the client held another.
  They drifted, and the suite stayed green over an unreachable route (#386).
- The brief holds its own idea of the care-gaps payload (`due` with
  `measure`/`reason`); the producer emits another (`lines` with
  `rule_id`/`title`/`message`). The seam mismatch is #387/#435.
- The Vercel `healthclaw` project holds an 83-variable copy of the Railway
  production config. One hostname therefore had two plausible owners with
  *opposite* security postures, which is how the 08-06 restore first landed on
  the wrong backend.
- CLAUDE.md holds the rule "never merge your own PR" while branch protection
  holds `required_pull_request_reviews: null`. A rule that claims enforcement
  it does not have is itself a Generator-A instance: the enforcement produced
  nothing, and the reader took the prose as the answer.

### Why fixing instances never converges

1. **Each fix repairs a site, not the generator.** Sixteen sites fixed;
   nothing stops boundary seventeen from being born binary tomorrow.
2. **Each fix mints a new dialect for the missing state.** We now have seven
   vocabularies for "I could not answer": `WORKERS_UNKNOWN`
   (careagents/app.py:816), the caregaps `indeterminate`/`unavailable`
   statuses, `HealthClawError` plus two predicates (healthclaw.py:119,140),
   the `CARE_GAPS_REASON_*` strings (brief/engine.py), `LLMRateLimited`
   (llm.py:49), `require_grant` raising (access.py:308), and
   `ip:unidentified` (rate_limit.py). Seven dialects means six translations,
   and translations are exactly where #387 lives.
3. **Verifiers are derived from producers.** A test that copies the route's
   constant, a grader that trusts its own read, a monitor that asserts a field
   it never fetched — each agrees with the thing it checks, so both can be
   wrong identically. Agreement is only evidence when the two sides are
   independent.
4. **Coupling knowledge lives in prose.** "#391 must ship with #382 because
   the crash is the only thing preventing the leak" was true for weeks and
   written down nowhere; it was found by accident, by a control test. Prose is
   where knowledge goes to be present but not consulted.
5. **Retros are prose too.** See the framing above. A lesson that does not
   land as a mechanism — a ratchet, a required check, a shared type — should
   be assumed forgotten by the next session.

## 3. Three laws

The system is three laws. Everything else in this repo's 19 process documents
is either an instance of one of them or a candidate for deletion.

**Law 1 — every boundary answer carries "could not answer" as a first-class
value, and no consumer may collapse it.** Truthiness is collapse. Identity
comparisons only. A bool at a boundary is a claim that reality has two states;
that claim has been wrong sixteen recorded times.

**Law 2 — every fact has exactly one owner; every other appearance imports it
or derives from it.** URLs, payload shapes, configs, deploy targets, rules.
The corollary for prose: a rule that claims enforcement must name its
mechanism, and the mechanism is the owner — the prose is a comment on it. A
rule with no mechanism gets rewritten to say so ("unenforced; reflex only") or
deleted.

**Law 3 — a verifier that has never been seen to fail proves nothing.** Every
gate, probe, and monitor ships with its demonstrated red: a mutation shown
failing, a control that proves the probe reached its subject, a scan floor
that fails when the walk walks nothing. Green without a demonstrated red is
Generator A wearing a checkmark.

**The operator corollary.** Recorded uncertainty is the human "could not
answer". If the session notes contain "I cannot explain X", then X's domain is
closed to destructive actions — delete, deploy, DNS, merge — until X is
explained or the human explicitly overrides. The 08-06 outage is the proof
case: the sentence existed, the deletion proceeded, production went down.

## 4. Enforcement, not exhortation

Each law gets a mechanism. A law without one is row fifteen of the table.

**Law 1 seed.** A single outcome module — `Answered(value)` / `AnsweredEmpty`
/ `Unasked(reason)`, with `__bool__` raising so truthiness is a crash rather
than a collapse — plus a ratchet in `tests/test_ratchets.py` counting the
seven dialects, which may only go down as boundaries migrate. This is the
access-kernel idea (`r6/access.py` already raises instead of returning a
misreadable tuple) generalised from auth to every boundary. The playbook's
A-series absorbs it.

**Law 2 seeds.**
- Seam contracts are importable, not describable. The producer owns the shape;
  the consumer imports it; the consumer's tests assert against the import. The
  template already exists: #434's fix asserts the *client's* URL against the
  app's real `url_map` instead of a copied constant.
- One deployment per surface. The 83-variable shadow config on the Vercel
  project gets deleted down to the handful `vercel.json` declares (owner
  action — it is production config). After that, a hostname has exactly one
  plausible owner and the 08-06 incident class is unrepresentable.
- This PR moves one config fact to its owner as a demonstration:
  `TRUSTED_PROXY_HOPS=2` lands in `vercel.json`, the file the repo owns,
  rather than in a dashboard where it would be the second copy of a fact.
  (#445 is inert on Vercel until this deploys.)
- The CLAUDE.md self-merge rule gets rewritten to name its mechanism (branch
  protection) once that mechanism is real.

**Law 3 seeds.**
- Already practiced tonight and now the standard: #445 and #447 both carry
  their mutation tables in the PR body. A PR adding any gate without its
  demonstrated red is incomplete.
- #443's fix applies Law 3 to the grader itself: a check whose subject is
  absent is *not evaluated*, a run containing any not-evaluated check cannot
  grade A, and the regression test asserts the negative — an empty tenant must
  NOT grade A. Pinning only the happy path is what let the grader lie.

**Process seeds (applied or pending owner action).**
- Branch protection: all eight substantive checks required
  (`claude-standards-review, python-tests, postgres-tests, node-tests, lint,
  secret-scan, dependency-audit, compliance-gates`), `strict=true`,
  `enforce_admins=true`, no required reviews (GitHub blocks self-approval, so
  for a solo maintainer that setting converts auto-merge into a bypass habit).
- Auto-merge must stop making unverified main: merges performed with
  `GITHUB_TOKEN` fire no push CI, so every auto-merged PR leaves main's HEAD
  without a full-suite run. Fix is an app/PAT token for the merge step (owner
  action: requires creating a secret).

## 5. Worked example: #435/#387 under the laws

The brief's care-gaps section requires a `due` key nothing emits. The tempting
fix is a mapping shim from the producer's `lines` to the brief's `due`. The
laws forbid it, and produce a better fix:

- **Law 2:** the consumer summary from `r6/caregaps/report.py` *is* the
  contract. The brief imports and renders it — `lines`, `note`, and the
  unevaluated markers — rather than maintaining a private translation of it.
- **Law 1:** the brief passes `patient=None`, so demographics-gated rules
  cannot evaluate. Under the shared contract that state arrives explicitly as
  the demographics marker `report.py` already emits, and the section renders
  "we could not check the ones that need your details" instead of an empty
  list that reads as "nothing is due" — which #428 established is a clinical
  claim only ever repeated from a result that made it.

No unilateral clinical decision is needed for the plumbing. The A1c cadence
(#389) remains a separate, clinician-gated question about the rule itself, and
is unchanged by any of this.

## 6. What gets deleted

The system must cost less than the pile it replaces, or it is the pile.

- The seven could-not-answer dialects, as migrations reach them.
- Copied seam constants and shapes (URLs, payload keys) wherever a contract
  import replaces them.
- The Vercel shadow config: 83 variables down to the `vercel.json` set.
- Prose rules claiming enforcement that does not exist, starting with the
  self-merge line, replaced by their mechanisms or marked "reflex only".
- Candidate, owner's call: fold the overlapping process documents into the
  playbook. Nineteen process docs is itself a Generator-B surface — the same
  rule stated twice will eventually be stated differently.

## 7. What this does not fix

Clinical correctness: no law makes the A1c cadence right; only a clinician
can (#389). Owner-gated actions: the merge-token secret, the shadow-config
deletion, the branch-protection write. And the operator: a law is a check,
not a reflex. The honest version of the operator corollary is that it works
only if the check is performed out loud — before any destructive action, name
the contradiction scan's result. Tonight it would have taken one sentence:
"the domain list says this project serves nothing, but something is serving
this hostname, and I have not reconciled those." That sentence was available
for six hours before the deletion. The system above makes such sentences
harder to route around; it cannot make them unnecessary.
