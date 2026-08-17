# Getting humans to test this

Answers decision 2 of `docs/2026-08-16-delivery-process.md` — *"fund the tester
role"* — with the arithmetic, the three populations, what each one legally
requires, and what is blocked on what.

**Status: proposed. Nothing here has been posted, offered, or promised to
anyone.**

## It is a squad, not an army

The process asks for **two sign-offs on each of six feature sets** — QA
(adversarial, ran it hostile) and end-user (someone who is not us, using it
for its purpose). Twelve touchpoints, and several are already covered.

| Set | QA sign-off | End-user sign-off |
|---|---|---|
| 1 Guardrail core | contract QA | a partner integrator |
| 2 Connectors | contract QA | vendor DevRel — **a channel exists** (Aidbox examples PR #43) |
| 3 Consumer journey | contract QA | **a stranger with a phone — unassigned, and the founder cannot fill it** |
| 4 Action rail | contract QA | a clinician receiving the output |
| 5 Clinical rails | contract QA | **the physician advisor — already engaged** |
| 6 Surfaces | contract QA | a partner installing it without our help |

So the real gap is **2–3 contract QA testers** (one person can carry two or
three sets) and **about four end-users we do not currently have**. Issue #184
supplies breadth on top of that, not instead of it.

Recruiting fifty people would produce fifty first impressions of a consumer
app whose deployed build is stale (#427). Sizing matters more than volume.

## Three populations, three legal profiles. Do not conflate them.

| | Who | Data they touch | Legal setup | Blocked? |
|---|---|---|---|---|
| **A** | Contract QA testers (Upwork or similar) | **synthetic only** | ordinary contractor terms, NDA | **no** |
| **B** | Beta users connecting their own records | **real PHI** | consent (built), privacy policy, FTC HBNR obligations | **yes — #168** |
| **C** | Developers and integrators | their own sandbox FHIR server, no PHI of ours | none | **no** |

**A and C can start now. B cannot**, and the reason is written down already:
#168 says the FTC Health Breach Notification Rule very likely applies, because
the rule defines a personal health record by its *technical capacity to draw
health data from multiple sources* — which is exactly the connector
marketplace. The issue's own words: *"this needs a decision before we recruit
beta testers who connect real records."*

HBNR is an **obligation, not a prohibition**: breach notification to each
affected individual within 60 days, to the FTC, and to media above a size
threshold — with unauthorized disclosure counted as a breach, not only an
intrusion. The owner's decision is *accept the obligation and prepare for it,
or do not run a real-record beta yet.* Carrot design follows from which way
that goes.

## What agents can do, and the specific thing they cannot

Worth stating precisely, because "this cannot be solved agentically" is only
two-thirds true.

**Agents did this today, in an afternoon:** ran the conformance harness both
ways, exercised the write-gate matrix against a live server, drove four
connector walkthroughs, and surfaced eight real defects — every one found by
running rather than reading.

**Agents cannot:**

- **be a stranger.** Every agent here knows how the product is supposed to
  work, so it never takes the wrong path — and the wrong path is the finding.
- **hesitate.** "I stopped here because I did not trust it" has no machine
  equivalent, and it is the single most valuable sentence a consumer-journey
  tester produces.
- **use their own records.** Set 3 and set 5 are about a real person's real
  data. Nobody can delegate that.
- **sign their name to it.** An end-user sign-off is an accountable human
  saying *I used this and it worked.* An agent's assurance is the thing we are
  trying to stop relying on.

So the budget buys the four things above. It does not buy more curl matrices.

## Carrots, by population

**A — contract QA.** Pay them. It is work, at market rate, on synthetic data.
The carrot question does not arise.

**C — developers and integrators.** The strongest and cheapest lever, because
the reward is professional rather than financial:

- **Credit in the guardrail spec.** #234 is an open issue to publish a
  versioned guardrail specification with a threat model. Contributors named in
  a spec other people implement is a real career artifact.
- **A security hall of fame.** `SECURITY.md` exists; a "found this" list costs
  nothing and is the standard currency in this field.
- **~~Conformance badge for their own server.~~ Withdrawn, and this is why.**
  The offer was: run `$conformance` against your FHIR deployment, publish the
  scorecard. It cannot be honoured today and it fails twice. #525 — the probe
  requires the declared supported-parameter set to *equal ours*, `context-id`
  included, so a server that refuses a bad parameter perfectly correctively
  still grades C, capping the deployment at B. And `scripts/guardrail_
  conformance.py` puts the repo root on `sys.path` and imports
  `r6.conformance`, so **a third party has to clone our application to grade
  their own server**. Every recruit would get a B for a reason that is our
  defect, not their server's. It returns when both close.
- **A finding credited by name in the specification.** The stronger offer, and
  it is already real: `docs/specs/guardrail-spec-0.1.0-draft.md` publishes a
  Credits section before anyone is in it, and says that *a finding that a
  check passed without its subject ever running is worth more to us than a
  feature.* So the invitation is: run the harness against your own server and
  tell us where it grades you wrongly. The first external run is a finding we
  credit — and #525 is the one we already know about.
- **Fast, real review of first PRs.** #184 already promises this. Honouring it
  visibly is the retention mechanism.

**B — real-record beta.** Service-shaped only: founding-member status,
permanent free tier, early access to new connectors. **Never cash for
connecting records** — paying a person to hand over health data is a different
ethical and legal question from paying a contractor to test software, and it
should be reviewed with #168 rather than decided here.

## Three phases

**Phase 1 — this week, costs nothing.**
Activate #184. It is written, it is good, and it has never been promoted.
The webinar on 18 August is a room full of population C; one slide and one
link is the highest-leverage recruiting act available, and it is the owner's
to make.

**Phase 2 — the pilot. 2–3 contractors, synthetic data.**
Hire individually rather than through a managed crowd-testing service. The
enterprise platforms in this space price on a platform fee plus an annual
consumption commitment, which is the wrong weight for a six-set pilot. Each
contractor gets one feature set and the charter that already exists for it in
`.claude/agents/owner-*.md`. **Those briefs are not in the repository** —
`.claude/` is gitignored on purpose, and `git ls-files .claude` returns
nothing. They read like a statement of work and no contractor can open one.
Publishing the six under `docs/`, or writing a contractor-facing SOW that does
not depend on them, is a prerequisite to hiring anyone and is tracked
separately.

Run one set end to end first (**set 2, connectors** — smallest, two of four
kinds already proven, a real deadline attached) to find out what the gate
actually costs before committing five more.

**Phase 3 — real-record beta, gated.**
A **waitlist can open now**; a *connect* cannot. Collecting an email and an
intent is not regulated by HBNR — connecting a record is. So: open the
waitlist, gate the connect on the #168 decision **and** the CareAgents
redeploy (#427), and do not burn first impressions on a stale build.

## What blocks what

| Blocker | Blocks | Owner |
|---|---|---|
| #168 — FTC HBNR decision | population B entirely | owner |
| #427 — stale CareAgents build | any set-3 recruiting | owner (manual `railway up`) |
| Docker + missing `.env` + Aidbox activation | sets 1 and 2 evidence | owner |
| Nothing | population A and C | — |

## Appendix A — draft contractor brief (not posted)

> **Adversarial QA for a healthcare AI guardrail layer — synthetic data only.**
>
> We build an enforcement layer that sits between an AI agent and a FHIR
> health-record server: reads come back redacted, every access is audited,
> writes need a credential the agent cannot mint, and clinical writes need a
> human.
>
> We need someone to **try to break that**, and to write down honestly what
> did not work.
>
> You will: run a documented walkthrough end to end against a live system;
> try to get data past the redactor, a write past the auth gate, or one
> tenant's data out of another's; record what you did as a run log; and write
> an edge-case register naming what does not work.
>
> **You will not touch anyone's real health data.** Everything is synthetic.
> No BAA, no clearance, no health-industry background required.
>
> Useful: API testing (curl/Postman), reading HTTP status codes as a contract,
> writing a defect report someone can reproduce. Not required: FHIR knowledge,
> healthcare experience.
>
> The one thing we care about most: if a check looks like it passed but you do
> not believe it actually ran, say so. That finding is worth more to us than
> the feature.

## Appendix B — draft founding-tester terms (not offered)

> Not offered to anyone, and not offerable until #168 is decided.
>
> - Permanent free access to the consumer app for founding testers.
> - Named in the release notes for the version their feedback shaped, if they
>   want to be.
> - Direct line to the team, and their edge cases enter the register with
>   their issue number.
> - **No payment for connecting records.** If we ever pay for anything it is
>   for time in a scheduled session, on terms reviewed with #168 — not for
>   the data.

## Sources consulted

Bounded, and stated: two web searches on crowd-testing platforms and on
synthetic-data practice for health-app testing. The results were vendor blogs
and listicles, so **no pricing figure from them is repeated here** — only the
structural observation that the managed platforms price on a platform fee plus
an annual consumption commitment. The legal reasoning is #168's, which cites
primary `.gov` sources; nothing new was researched for it.
