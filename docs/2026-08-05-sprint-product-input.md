# Product input for the Aug 5-18 sprints

Companion to `docs/2026-08-05-prioritized-backlog.md` (the ranking) and
`docs/2026-08-04-wrapup-review-and-plan.md` §7 (the task queue). This document
holds the product calls those two defer to an owner: the demo narrative, the
ruling on #310, the recommendation on #157, an honesty review of what a
viewer will see, and five value gates.

Everything below assumes the G1 result: the five questions now answer
correctly on a live tenant. That changes the job from "make it work" to
"decide what to show", which is what this is.

## 1. The demo narrative

### 1.1 The constraint that shapes everything

An import takes 5-45 minutes (`careagents/intake_state.py`,
`ARRIVAL_WINDOW_SECONDS = 45 * 60`; the same window is promised in
`templates/fasten_connect.html:218`). A live connection cannot produce
readable records inside a 10-minute talk.

So the demo runs on two accounts, and says so out loud. A fresh account for
the spin-up and connect beats, and a second account connected in advance for
the reading beats. Naming the switch costs eight seconds and removes the only
thing in the demo an audience could later call a trick.

The alternative — a pre-baked account presented as if it connected live — is
the exact defect this product exists to argue against. It is also what #310
does today, which is why the ruling in §2 goes the way it does.

### 1.2 The beats

Eight beats, about 10 minutes, with 2 minutes of slack.

| # | Beat | Time | What the audience concludes |
|---|---|---|---|
| 1 | Spin up an agent on a phone: sign in, name it, choose a connector | 1:00 | No install, no jargon, about a minute. This is a consumer product |
| 2 | Run a real provider connect. Land on the pending screen: records arrive over 5-45 minutes | 1:30 | The connection is real, and the product refuses to pretend the data has arrived. **Switch accounts here, out loud** |
| 3 | The pre-connected agent's first screen: real counts of conditions, medications, results | 1:30 | It read an actual record, not a fixture |
| 4 | "What medications am I on?" | 1:00 | It reads coded clinical data and names drugs |
| 5 | "Do I have any allergies?" | 1:00 | It will not claim absence. The strongest guardrail beat available |
| 6 | "Give me a timeline of my cholesterol results" | 1:00 | Not retrieval — a view a patient could not assemble alone |
| 7 | Intake form: propose, review, the refused submit, attest, PDF | 2:30 | The human gate is real and fails closed. **The differentiator** |
| 8 | The audit trail behind what just happened | 1:00 | None of this is trust-me |

### 1.3 Which of the five questions earn a place, and why

**In: medications, allergies, cholesterol timeline. Out: labs, conditions.**

- **Medications, first.** It is the most concrete answer and the one most
  recently proven. It is also the honest place to tell the E2 story: a day
  before, this answer was a confident falsehood, and here is what changed.
  An audience of health-IT people has never seen a vendor open with its own
  defect. It buys more credibility than any feature in the demo.
- **Allergies, second.** "Recorded but not coded at the source" is the single
  best sentence the product produces. It declines to say "no known
  allergies" from a hole in the data, and that refusal is the thesis.
  Putting it second means the guardrail lands before the visual payoff.
- **Cholesterol timeline, third.** A chart is the only beat with a visual
  payoff, and it sets up "now let us do something with this."
- **Labs, cut.** The timeline is a lab question that renders a chart. Strictly
  better on a stage, and keeping both spends a minute to say one thing twice.
- **Conditions, cut.** It is the least surprising answer in the set. Nothing
  is learned that beat 4 did not already establish.

Cutting two questions buys roughly two minutes. That goes to beat 7, which
is the only beat a read-only competitor cannot copy.

### 1.4 Beats that depend on unfinished work

- **Beat 7 is the exposed one.** See §6 — the rail has never been exercised
  end-to-end through the CareAgents relay against a running engine.
- **Beat 8** should describe the audit trail, not quote a conformance grade.
  #213 says Grade A is earnable by a deployment with the guardrails off. The
  grade is not wrong here, but a claim whose own issue is open is not a claim
  to make from a stage.
- **The pre-appointment brief is not in the demo.** See §5.3. It does not
  currently work at all (§4.2), which makes the cut easy.
- **The "Any screenings I'm due for?" starter is visible during beats 3-6**,
  and today it always answers that nothing is due (§4.3). Nobody needs to tap
  it on stage, but a stranger testing the product will. Hide the starter or
  fix the subject before the 18th.

## 2. Ruling on #310: no

`/r6-dashboard` is **not** in the Aug 18 demo.

The page runs a real Stitch connection, animates a simulated import driven by
writes to a different tenant, and toasts "Health data imported" carrying the
patient's real org id — before the import has happened. A page whose entire
pitch is "watch the guardrails work" fabricates the one thing it shows.

Three reasons the answer is no rather than "fix it first":

1. **The honest version is a journey design, not a repair.** Making the
   animation truthful needs a real pending-and-poll state, because the import
   is asynchronous. That is the same 5-45 minute problem as §1.1. It is not
   13 days of work alongside five P0s.
2. **It is redundant.** `templates/fasten_connect.html` is already the honest
   version of this exact moment, and it is the one on the real patient path.
   The demo shows that one in beat 2.
3. **Showing both would be the contradiction.** Demoing the honest connect
   screen while the dishonest one stays a click away in the same product is
   worse than having only one of them.

### What follows from the ruling

- **#310 drops out of the pre-webinar set** and is re-dated as a fast-follow.
  It stays open; it stops being stop-ship.
- **#305 gets simpler and should be closed by deletion.** `/fasten/demo` is
  an unauthenticated write path whose only consumer is the dashboard
  animation. With the dashboard out of the demo, there is no reason to gate
  it rather than remove it. Deleting a P0 write path is a better outcome than
  authenticating it. This is the main practical value of the ruling.
- **One thing still needs doing before the 18th.** The page stays reachable
  on the public internet while the founder tells an audience the guardrails
  are real. Someone who searches for the product can land on it. Take
  `/r6-dashboard` off the public navigation for the webinar window and leave
  the URL working for anyone holding a link. That is an hour, and it is not
  the redesign.

## 3. Recommendation on #157: scope to one tenant, and name the source

Eugene decides. The recommendation is **option 3, plus one copy fix**, with
unify filed as the epic it is.

### 3.1 What the code actually does

This matters more than the three options, because it rules one of them out.

- `careagents/connectors.py` mints a **fresh tenant per connection**
  (`client.new_tenant_id()`, `ca-<hex>`) for every real-record connector.
- `careagents/models.py` gives `Agent` a **single** `connection_id`, and
  `Connection` a single `tenant_id`.

One agent reads exactly one tenant, which holds exactly one source. A patient
with two doctors gets **two agents**, and each one answers "your medications"
completely and confidently from half the list. That is the absence defect at
the architecture level, and no wording fixes it.

The new evidence fits this exactly. The live agent runs on a
CareAgents-minted tenant, not the separately imported MEDENT tenant, because
no agent can point at a tenant the product did not mint. The split is the
shipped model, not an accident.

### 3.2 The three options, by patient consequence

| Option | What a patient gets | What it costs |
|---|---|---|
| **Unify** — one tenant per human, connections add to it | One agent that sees everything. The right end state | Consent is recorded per connection with a version (`Connection.consented_at`, `consent_version`). Merging sources puts data consented under different terms in one bucket, and "delete this provider" stops being expressible without per-source provenance. A live-account migration inside the window. If rushed, a patient deletes one provider and loses another's records |
| **Selector** | The patient chooses which records to ask about, per question | The patient carries the data model. On a phone, in an in-app browser, it is a chore before every question. It does not stop an answer sounding complete — it only makes the incompleteness the patient's fault |
| **Scope to one tenant** (recommended) | One agent, one provider, answered well | The agent sounds complete over one provider's slice. This is real, and it is the reason for the copy fix below |

### 3.3 The recommendation

Take option 3 for the 18th. Demo the agent that holds the records the demo
asks about. Do not migrate the separately imported tenant, and do not connect
that provider through the product for the demo either — that mints a second
agent and puts the split on stage.

**The copy fix that makes option 3 honest.** The first-screen greeting
(`careagents/templates/chat.html`) says "I found N conditions ... **in your
records**", with no source named. For a patient with one connection that is
fine; for a patient with two it is false in the #336 way — a fact about one
data slice stated as a fact about the person. The arriving and overdue
notices in the same template already name the provider, and
`IntakeState.provider` carries it. Name it in the ready greeting too.

That is a one-line change that converts the recommendation from "we scoped
the demo" into "the product tells you what it can see." File unify as the
post-webinar epic it always was.

## 4. Honesty review: what a viewer will see

The medication defect is the sharpest lesson available. An agent, handed a
hole, invented a confident explanation for the hole and attributed it to the
patient's clinic. The pattern is: **a branch that learned nothing made a
claim about something it never observed.** Here is where else that can happen
on the demo path.

### 4.1 The medication fix has a fourth route still open — measured

`careagents/agent.py:261` routes only two lookup reasons to the honest
wording:

```python
elif lookup_reason in ("unavailable", "not-attempted"):
```

`_medication_resolver.resolve` returns four reasons. The fourth,
`"not-a-ref"`, is returned whenever `medicationReference.reference` does not
start with `Medication/` — which includes `#contained` references and
`urn:uuid:` references, both ordinary FHIR shapes. It falls through to the
`else` branch and produces the sentence #379 was written to prevent.

Measured against synthetic input, not read:

| Input `medicationReference.reference` | What the model is told |
|---|---|
| `#med1` (contained) | "recorded but not coded at the source" plus "The source system sent this record as free text with no standard code" |
| `urn:uuid:1234` | the same |
| genuinely uncoded control | the same — and here it is true |

The first two are false. We declined to follow the reference and then
described how the clinic recorded it. #379's own commit message names this
case: *"the ref was not a Medication -> we declined to chase it"*, listed
among the outcomes that must not produce the sentence. The three-way branch
closed two of the three.

This is on beat 4. **Severity: the same as #376.** Adding `"not-a-ref"` to
that tuple is the whole fix, and the mutation that proves it is removing it
again.

### 4.2 The Appointment Brief has never run — measured

`careagents/templates/brief.html` renders five sections. Four use this empty
state:

> Not available from your connected records.

The care-gaps section, at line 89, uses this one:

> We found no preventive care items based on your current records.

That is an affirmative clinical finding. It renders today, for every patient,
and the cause is not #381.

The route is registered at the wrong URL. `r6_blueprint` already carries
`url_prefix='/r6/fhir'` (`r6/routes.py:74`), and `r6/brief/routes.py:90` adds
`@blueprint.get("/fhir/AppointmentBrief")` on top of it. Inspecting the built
URL map:

| Step | Value |
|---|---|
| Rule actually registered | `/r6/fhir/fhir/AppointmentBrief` |
| URL CareAgents requests (`careagents/healthclaw.py:334`) | `/r6/fhir/AppointmentBrief` |
| Rule that matches it | the generic `search_resources`, `resource_type='AppointmentBrief'` |
| Response | `400`, "Resource type is not supported" |

`fetch_appointment_brief` returns `None` on any non-200
(`careagents/healthclaw.py:338-341`), `careagents/app.py:663` turns that into
`sections = {}`, and all five sections render empty — including line 89.

Three consequences, and the third is the one that matters for sequencing:

1. **The whole feature has never worked.** Not degraded — never reached.
2. **#382's leak cannot currently occur**, because the handler does not run.
   The `r.resource` attribute it describes does not exist on `R6Resource`
   either, so the handler would raise before returning anything.
3. **Fixing the URL activates #381 and #382 at once.** A one-line route
   correction turns on an unredacted read path and a crash-renders-as-good-news
   path in the same deploy. These three must land together, with #382's
   marker probe first, or the repair is the incident.

### 4.3 `$care-gaps` answers every patient with silence

`careagents/healthclaw.py:193` calls `Patient/$care-gaps` with no `subject`.
`_subject_from_request` (`r6/caregaps/routes.py:38`) reads only `?subject=`
or a `Parameters` body, with no fallback to the tenant's own Patient, so
`subject` is `None`. Then `_resources_for` filters with
`res.get("subject", {}).get("reference") == subject`, and every real resource
fails that comparison against `None`. Conditions, observations, immunizations
and procedures all come back empty, and `build_consumer_summary` yields
`{"lines": []}`.

`careagents/agent.py:351` hands the model that and nothing else. Compare
`show_lab_timeline` twelve lines above, which carries an explicit note that
an empty series "is not the same as the person never having had this test".
`get_care_gaps` has no such note, and it discards the summary block holding
the indeterminate count.

**What a patient sees:** they tap the "Any screenings I'm due for?" starter
and are told nothing is due. On every tenant, every time. Every test in
`tests/test_caregaps_routes.py` passes `?subject=Patient/p1`; the only caller
in production never does.

### 4.4 The intake form can assert no medications and no allergies

This one is on beat 7, and it is the most consequential per occurrence.

`r6/actions/review.py:112-118` returns early from `_gather_content` when the
tenant has no `Patient` row, or when the Patient has no `id`. It also matches
clinical resources only on an exact `Patient/<id>` reference, so a feed using
`urn:uuid:` or an absolute URL contributes nothing. The result is an empty
content list, zero questionnaire repeats, and a review page that reads:

> No medications found in your records.
>
> No allergies found in your records — add them or affirmatively confirm none
> below.

A form for a new clinician, asserting no medications and no allergies, and
then pointing the patient at the "No known allergies (patient confirmed)"
checkbox. The server-side attestation gate itself is sound — it re-derives
the list from FHIR and returns 422, and nothing here defeats it. The problem
is that the gate is protecting a list emptied by a lookup, not by the record.

That is the demo's own money shot, arriving pre-emptied.

### 4.5 Lower severity, same shape

- **The truncation denominator is the page size, not the total.**
  `_summarize_bundle` counts `bundle["entry"]`, which the engine clamps to 50,
  and ignores `bundle["total"]`. The model is told "12 of 50" for a patient
  with far more. `careagents/app.py:617-624` documents this exact lesson and
  fixes it for the greeting; the tool payload did not get the fix.
- **The `"empty"` connection status is unhandled.** `classify` compares
  against `"pending"`, and the Upload-records connector creates connections
  with `status="empty"` (`careagents/connectors.py:152`). The result is the
  zero-greeting that `intake_state.py` exists to prevent, reachable by
  connecting Upload records and opening chat before uploading.
- **The care-gaps MCP app renders `[object Object]`.** `consumer.lines`
  entries are dicts, and the template escapes them as strings.
- **The greeting's "in your records"** — §3.3. One slice presented as the
  whole record.

Two things are right and should be left alone: `_summarize_bundle`'s
`truncated: True` marker, and `r6/labs/interpret.py`, which refuses to guess
a normal. Both are the pattern the fixes above should copy.

### 4.6 The rule for the dress rehearsal

Every on-screen sentence gets one question: **did the code that produced this
sentence observe the thing the sentence asserts?** "No results" and "we could
not look" must never render the same. That is the whole review.

## 5. Value gates

The gate is: *does this change what a patient can accomplish, this month?*

### 5.1 Intake form rail — SHIP, conditionally

It is the only beat where the patient ends up holding something new: a
completed intake form built from their own records, with a server-side
refusal to fabricate an allergy attestation. Every read-only competitor stops
before this. It is the reason the talk is worth giving.

Two conditions, both in §6. The rail has never been run through the phone
relay against a live engine, and §4.4 found that its content lookup can hand
the patient a form asserting no medications and no allergies. The gate is
sound; what feeds it is not. Nothing else in the backlog is worth more than
closing those two questions.

### 5.2 SHL sharing — DROP

It does not exist. `careagents/connectors.py:51` carries `"tier": "soon"`,
and `r6/sdc/delivery.py:13` describes the envelope as a future enhancement.
The patient-facing copy is already honest: *"Not open yet — we'll let you
know."* No work, no demo, no change. The house rule in that file — ship the
mechanism, then the copy — held, and this is what it looks like when it works.

### 5.3 Care-gaps — DEFER the feature, FIX the honesty, CUT it from the demo

Three separate calls, and they point different ways.

- **As a capability: defer.** It tells a patient to get a screening and
  cannot book one. Booking is #163 and is not in this window. Nothing new is
  accomplished this month.
- **As shipped code: fix now, and fix it as one change.** §4.2 and §4.3 found
  two separate live paths that both tell a patient nothing is due. The brief
  is unreachable at its own URL, and `$care-gaps` is called without a
  subject. #381 and #382 stay P0, but neither describes what is actually
  happening, and #382's leak cannot occur until the route is fixed. The route
  correction, the redact-then-relabel pair, the care-gaps third state, and
  the missing subject belong in one sequenced piece of work with the marker
  probe first. Fixing the route alone would turn on an unredacted read path.
- **As a demo beat: cut.** `/brief` comes off the demo path, and the "Any
  screenings I'm due for?" starter should be hidden until §4.3 is fixed.
  Beat 7 already carries "produces a document for your clinician", with the
  human gate the brief does not have.

### 5.4 Lab-trends timeline — SHIP, already shipped

PRs #357 and #358 landed it. It earns beat 6 outright: a patient could not
previously get a visual answer to "how has this changed", and it is the
cheapest impressive beat in the demo. No further build.

### 5.5 Curatr data-quality surface — DROP from the demo

Two independent reasons, either sufficient:

1. It is not reachable from the patient app. Nothing in `careagents/` calls
   it. A patient cannot accomplish anything with it this month by definition.
2. #376 established that its RxNorm validation has never once succeeded. The
   endpoint was wrong from the day it was written, and the failure returned
   `None`, which reads as "could not check" — so nothing looked broken.

A data-quality surface that has never validated anything is precisely the
defect shape the talk argues against. It must not be on stage. Post-webinar,
either fix the validation or remove the claim; an unfixed claim is worse than
a missing feature.

## 6. The single biggest risk

**Beat 7 has never been run.**

The forms rail is proven on a synthetic tenant, driven from an MCP desktop
client, with a step-up token minted by hand — that is what
`docs/demos/forms-rail-run-of-show.md` describes, and it is a different
surface, tenant, and dataset from the demo. The CareAgents relay that a phone
actually uses (`GET /review/<agent_id>/<action_id>`, `careagents/app.py:886`)
is covered only by a fake whose review page is a one-line string
(`tests/test_careagents.py:1447`). That is CLAUDE.md's documented trap: the
tests prove the call is made, not that it is accepted.

Two known problems sit on that unexercised path. The review page extends the
engine's `templates/base.html`, which loads Bootstrap and Font Awesome from
two CDNs — so mid-journey the patient leaves the CareAgents visual identity
for something that looks like an admin console, and the page renders
unstyled if a venue network blocks a CDN. And the relay rewrites the form
action URL by string replacement against page HTML it has never parsed in a
test.

And §4.4 gives that unexercised path a known defect aimed at the one control
the beat exists to show. If the tenant has no `Patient` row, or references
its patient by `urn:uuid:`, the form arrives asserting no medications and no
allergies. The demo would then show the attestation gate defending a list
that a lookup emptied.

**The ask:** walk beat 7 end-to-end on a phone, on a tenant holding real
records, against the running engine, before anything else in §1 is
rehearsed. Everything else in this document is a decision. This is the one
thing only a walkthrough can answer, and §4.4 says what to look at first.

## 7. Open questions only the founder can answer

1. **Is the read-only demo acceptable if beat 7 fails?** If the answer is no,
   the walkthrough in §6 is the top of the queue this week, ahead of P0s that
   are live but not on the demo path.
2. **Which account is the pre-connected one**, and is its record set rich
   enough for beats 4, 5 and 6 without the separately imported tenant? §3
   recommends not merging; that recommendation assumes the answer is yes.
3. **Is taking `/r6-dashboard` off the public navigation acceptable**, or
   does the page have a use during the webinar window that argues for
   keeping it visible?
4. **ICP versus HIMSS.** This document plans one demo. If the second talk has
   a different audience, the medications-first ordering in §1.3 is the beat
   most worth reconsidering.
5. **Does the brief work get re-scoped?** §4.2 shows the feature has never
   run, so #381 and #382 describe code that has never executed. That is
   either a reason to fix all of it properly after the webinar, or a reason
   to do it now while the patient-visible symptom is live. It is a priority
   call, not a technical one, and the answer changes what Sprint 1 contains.
