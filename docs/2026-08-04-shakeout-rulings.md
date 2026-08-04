# Rulings — 2026-08-04 shakeout questions Q1–Q3

Three design questions were raised by the hard review of the 2026-08-04 work
(PRs #342, #344, #345). All three are now settled. This file is the record;
each ruling also lives next to the code it governs, because a decision that
only exists in a dated document is a decision the next person will re-litigate.

---

## Q1 — `Observation/$interpret` with no input: keep it

**Question.** #342 made "no subject, no body" mean *interpret this tenant's
whole stored record*. Convenient for clients, but implicit: the request does
not say what it reads. Commit to it, or force clients to be explicit and
deprecate the fallback?

**Ruling: KEEP.** It is committed API surface, not an accident of the fix.

**Why this is defensible rather than merely convenient.** The operation is
already tenant-scoped by construction — the caller passed tenant read-auth
before reaching the handler, and the fallback selects `WHERE tenant_id = ?`.
"Everything I am allowed to see" is therefore the *only* thing the empty form
could sensibly mean; there is no wider set for it to accidentally reach. That
is what makes an implicit default safe here and would not make it safe on,
say, a write path or a cross-tenant search.

**What the ruling obliges us to maintain**, now pinned in the handler
docstring and by tests:

1. **Nothing supplied ≠ something unusable.** A junk body — a bare array, an
   unparseable payload, a `Parameters` whose subject will not resolve — is
   `ignored`, never a fallback. A malformed request must not widen into the
   widest possible read. (This was D1, fixed in #342.)
2. **The fallback is bounded** by `STORED_OBSERVATION_CAP`, matching the
   search route's `_count` ceiling, so no surface can quietly read more than
   another.
3. **The response says how much it interpreted** (`summary.total`), which is
   also what makes shakeout row S1 measurable from the audit trail.

**Not covered by this ruling:** the `?subject=` branch still loads every
Observation the tenant owns into memory before filtering. Same defect class,
different branch, tracked separately — capping it would truncate callers who
get everything today.

---

## Q2 — Terminology lookup: public endpoints now, self-hosted later

**Question.** Enabling `TERMINOLOGY_LOOKUP_ENABLED` sends `(system, code)`
pairs to NLM / RxNav / tx.fhir.org. Never patient data, but it discloses the
*set of clinical codes present in the deployment*. Acceptable for launch, or
does self-hosting come first?

**Ruling: enable the public endpoints now; move to a self-hosted terminology
server afterwards.** The disclosure is narrow and the alternative — an agent
that can name 1 of 26 conditions — is a worse product failure than the
disclosure is a privacy failure.

**The disclosure, stated precisely, so "narrow" is not doing unexamined work:**

- What leaves: a code system URI and a code. Nothing else — no tenant, no
  patient, no agent id, no free text, and (per the resolver's design) never
  an upstream `display`.
- What an observer learns: that *somewhere in this deployment* a record
  carries e.g. `L40.9`. Not whose, not how many, not when relative to any
  person — the per-process cache means each distinct code is asked **once**,
  so query volume carries no per-patient signal either.
- What an observer does **not** learn: any linkage between codes, any
  identifier, or anything at all about a specific individual.

**Conditions attached to this ruling:**

1. It stays a per-deployment switch. A deployment that cannot accept the
   disclosure leaves it unset and behaves exactly as before.
2. **Self-hosting is scheduled work, not an aspiration** — tracked as its own
   issue, and the change is a base-URL swap in `r6/curatr.py` because the
   routing already exists.
3. If the code set itself ever becomes sensitive for a particular tenant
   (a small population where a rare code is identifying), that tenant is a
   reason to self-host sooner, not a reason to re-argue this ruling.

---

## Q3 — Sustained throttling: fail fast and honestly

**Question.** Under sustained provider rate limiting, keep #345's
fail-fast-with-an-honest-message, or park the run as `waiting` and complete
it when capacity returns?

**Ruling (delegated to the implementer, so the reasoning is recorded here):
FAIL FAST. Do not build queue-and-resume before Aug 18.**

**Three reasons, in order of weight:**

1. **A parked run is a worse experience than an honest one at demo time.**
   An answer that silently arrives ninety seconds later, after the presenter
   has moved on, lands in the wrong place in the conversation. "I'm busy, ask
   again in a moment" hands control back to the person, immediately.
2. **Queue-and-resume is a new run state reaching the UI**, with its own
   notification path and its own failure modes, landing two weeks before a
   demo. #345's retry already absorbs the common case (a single 429 clearing
   within seconds); what remains is *sustained* throttling, which parking
   would not fix either — it would only hide it.
3. **The honest message is already correct behaviour**, not a placeholder.
   Being throttled is not a defect, and the system now says so.

**What this ruling requires instead:** throttling must be *visible* to us
even though it is not a defect. `scripts/shakeout_live.py` counts
`LLMRateLimited` runs and reports them on the scorecard as information
rather than failure — so "the provider throttles us occasionally" and "the
provider is throttling us constantly" cannot look identical.

**Revisit this ruling if** the scorecard shows rate-limited runs as a
material share of traffic. At that point the answer is probably provider
capacity or concurrency control, not parking — but the data decides.
