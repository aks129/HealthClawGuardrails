# Hard truths — the architecture review, run once on the vision

A worked example of step 3 of `docs/2026-08-16-delivery-process.md`, applied
to the whole product rather than one feature. Every claim is measured on
`main`, 2026-08-16.

The rule for this document: **only self-implicating findings count.** A review
that flatters the thing it reviews has not been run.

---

## 1. Nobody knows whether the product works

Four of the six feature sets have **never been run end to end**. Not "run and
found wanting" — never run.

| Set | Measured state |
|---|---|
| Guardrail core | Grade A local; **proxy mode unmeasured** |
| Connectors | **2 of 4** kinds proven live — reproduced by a second person 2026-09-04 |
| Consumer journey | **unmeasured** |
| Action rail | **unmeasured** |
| Clinical rails | **unmeasured** |
| Surfaces | partial |

Two of those four are the ones a patient touches.

## 2. The test suite is large, and it is not the thing that finds defects

**50,429 lines of test guard 29,508 lines of engine.** 3,153 tests pass, at
`4cb3771`. (First published as 50,125 / 29,446 / 3,151 — measured four merges
earlier than the header claimed, with a collected count printed as a passing
one. See the topology's note; the ratio and the point are unchanged.)

All eight defects found on 2026-08-16 were found by **running the system**.
Zero came from that suite. Three had passing tests sitting directly over them,
and one of those tests asserted the defect *as a specification*
(`("hapi", AUTH_NONE)`).

The suite is not wasted — it is what makes refactoring safe and it is what the
mutation batteries exercise. But it measures *"the code does what the code
says"*, and that has been quietly standing in for *"the product works"*.

## 3. The agentic loop is net-neutral on legibility

One full day of it: **7 PRs merged, 4 more opened, 2 new issues filed, open
count unchanged at 87.**

Every fix was real and every finding was true. It still did not move the
question the owner actually has — *what works?* — because discovery and
delivery were happening in the same motion, at the most expensive point in the
pipeline.

## 4. The most polished artifacts made the worst claims

This is the uncomfortable pattern, and it is consistent:

| Artifact | The claim | The reality |
|---|---|---|
| `$conformance`, the flagship | a grade a partner can trust | graded **F, 1/7** against a real HAPI, with **five of six failures caused by the probe and two of them blaming a gate by name** — the probe was creating a constant Patient and the server rejected the duplicate. *(Count corrected 2026-09-04, #605: this cell read "four of six failures blaming the guardrails"; no count in the run's transcript yields four.)* |
| `SECURITY.md`, the policy doc | "HIPAA Safe Harbor … redaction" | redaction **truncates** identifiers to their last four characters; Safe Harbor requires SSNs removed |
| the connector registry summary | "add `CLIENT_ID`/`_SECRET` for a HAPI behind Basic" | `AUTH_NONE` — the credentials were read, accepted and **dropped** |
| `smoke_medplum.py`, the QA script | "7/8 guardrail checks passed" | there was **no Medplum**; two of the seven passed on the body of a 401 |
| `form_fill.py`, the action rail | "if it vanished (deleted or stale) … fail loud" | the query did not filter, so a deleted record would be **rendered into a submitted form** |

The polish is the risk. A rough edge invites inspection; a confident sentence
next to a passing test does not.

Row 1 and row 3 have since been fixed (#514, #512). The 2026-09-04 re-run of
the same walkthrough grades that same HAPI **B 6/7** — the pack's diagnosis
that the F came from the probe and not from the guardrails, confirmed by
changing only the probe. The rows stay as written because they are what was
true on 2026-08-16, and the pattern they illustrate is not repealed by fixing
two instances of it.

## 5. Guards are written narrower than the property they are named after

Three of today's eight were guards that existed and were green:

- `test_no_image_is_floating` matched `ghcr.io/…:latest` — one word, one
  registry — while `healthsamurai/aidboxone:edge` sat three lines away.
- The de-identification language guard matched **four exact phrases on nine
  files**; `SECURITY.md` was neither a phrase nor a file.
- The #478 leak was closed after three of its **five** sites were fixed,
  because nothing checked the claim the issue itself made.

The failure mode is always the same: the guard is written from the fix in
front of it rather than from the property, and then it certifies the gap.

## 6. The action rail's gates are real and there is nothing behind them

#215: **no Tier-2 approve surface exists, so nothing could execute even with
vendor keys.** The 428/401/428/201 matrix is genuine and pinned. It is
guarding a door into a room that has not been built.

That is fine as a stage of development. It is not fine as an unstated one,
because every demo of the gates implies the thing behind them.

## 7. The riskiest deploy step is manual and has drifted before

CareAgents deploys by hand, from a staged directory. It has silently run **13
commits behind `main`**. Today its build is stale again (#427) and that is the
only thing keeping the issue open. The MCP server is the same shape.

## 8. The issue tracker is a symptom log, not a work list

87 open. **52 carry no label at all.** 58 of the 87 were opened in the last 16
days. There is no view that answers "what is broken in the consumer journey"
without reading all 87 titles — which is what the PRD gap tables in
`docs/prd/` now do by hand, once.

---

## What problem is actually being solved

Stated plainly, because the review is worthless without it.

> An agent handed a FHIR credential can read everything, write anything, and
> leave no trace. The only answers on offer are all-or-nothing. HealthClaw is
> the middle: **an enforcement layer that lets an agent be useful on real
> health records without being trusted.**

Everything in the six sets either serves that or does not:

- **Serves it directly:** guardrail core (the enforcement), connectors (where
  it can be deployed), surfaces (where it can be reached).
- **Serves it as proof:** clinical rails and the consumer journey are the
  demonstration that guardrailed access is *still useful* — the answer to "so
  it is safe, but can it do anything?"
- **Serves it as the hardest case:** the action rail. Reading under
  constraint is the easy half; acting under constraint is the claim nobody
  else is making.

Nothing in the current tracker is off-thesis. That is the good news, and it
is the only good news in this document.

## How we would prove it

One sentence per set, and each is already the PRD's §2:

| Set | The proof |
|---|---|
| Guardrail core | conformance A local, B in proxy, **with the failing property named** |
| Connectors | each of the four kinds runs its own live walkthrough |
| Consumer journey | a person finishes it on a phone, unaided, and signs off |
| Action rail | a recording where a human acts somewhere the agent cannot reach |
| Clinical rails | a clinician reads the output and does not correct it |
| Surfaces | every advertised tool answers, **and refuses when it should** |

None of these needs new architecture. Five of the six need a person to sit
down and run the thing.

## The three decisions this review surfaces, for the owner

1. **Adopt the pipeline, or say why not.** SOW → PRD → architecture review →
   build-as-60–80% → QA with a named tester → done-is-an-evidence-pack. It
   costs a gate that does not exist today; it is the only thing here that
   changes the ratio in §3.
2. **Fund the tester role.** Every §1 gap and every §4 lie was findable by one
   person running the product for an afternoon. There is no cheaper defect
   detector available and we do not have one.
3. **Unblock the two things only you can.** The local stack (Docker, the
   missing `.env`, Aidbox activation) and the CareAgents redeploy. Until both,
   two of the six sets cannot be measured at all — and this document cannot
   get shorter.
