# Intent — HealthClaw 2.0, this fortnight

**Status:** working document. Iterated each session, reviewed against
`docs/2026-08-16-hard-truths.md` ("what problem is actually being solved")
before any build starts. The council ruling of 2026-09-02 is binding and this
file does not override it. It records why we are doing the ruled work and what
we refuse to do meanwhile.

## The outcome, in the owner's words

> Reach HealthClaw 2.0 with real users and testers. Always focus on real
> problems to solve for patients.

## The problem, stated plainly

An agent handed a FHIR credential can read everything, write anything, and
leave no trace. HealthClaw is the middle: an enforcement layer that lets an
agent be useful on real health records without being trusted.

Today the enforcement is real (Grade A, 7/7 local) and the product around it
is unmeasured. Four of six feature sets have never been run end to end. The
first three screens a stranger sees were broken on 2026-09-02, verified live.

## Who the user is

- **A patient on a phone** who signs in, connects records, asks a question,
  and approves an action. Cohort 1 uses synthetic records. Cohort 2 (own
  records) waits on seven preconditions (ruling D3).
- **The physician design partner**, whose demos are the acceptance suite.
- **A partner integrator** reaching the MCP endpoint with a conformant client.

## What we build this fortnight, and the patient problem each solves

| Ruling item | Patient problem it solves |
|---|---|
| #536 hide, beta banner, `CARE_REAL_RECORDS` | A tester is not sent down a dead end or told a beta is a product |
| D10 `$populate` bound | A form does not read more of a person's record than the form needs |
| #542, #436, #458 honesty | The page never says "0 due" or "checked" about a record nobody evaluated |
| #528 payload immutability | What a human approved is what executes |
| Ratchets A5, B2, F5 | Every read is tenant-scoped, every write is audited, deleted rows stay deleted |
| MCP phase 1 (flagged, inert) | A partner's client can find its way from a 401 to a token |

## Deliberately out of scope (the STOP list, two weeks)

No new surfaces. No new kernel or audit slices beyond A5, B2, F5. No building
on MCP-auth or action-rail specs beyond #528 and flagged phase 1. No absolute
URLs from `request.host_url`. No measurement a non-author cannot reproduce.
No closing an honesty issue when the fix ends at the JSON.

## The decisions the owner is being asked for

They are in `docs/2026-09-02-council-ruling.md` §4. The first is one action:
arm merge on #544, then #540 and #541. Every open PR is red on
`dependency-audit` until #544 lands.

## How we work on it

- First principles: name the property each change protects, in one sentence.
- A team of specialists, one seat each, in isolated worktrees. Nobody merges.
- Reference working solutions in the same space before writing new code
  (Medplum bots, Block Buzz, Goose recipes, the MCP SDK's own auth router).
- Less code. Delete before adding. Use the helper the codebase already has.
- QA with the owner's data and connections, through the surfaces a person
  uses: careagents.cloud and the Telegram personas. No PHI in any artifact.
