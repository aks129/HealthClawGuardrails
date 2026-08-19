# How work gets defined, built, and proven

**Status: proposed. Needs the owner's decision before it binds anyone.**

## Why this exists, stated as the owner stated it

> "the prompting agentic engineering is not moving the ball forward and finds
> more issues than it solves … we have lots of issues just tracking what is
> broken and what is working unless someone actually tests it in real world"

That is measurable and it is correct. On 2026-08-16 a single day of agentic
work merged seven fixes, opened four more, filed two new issues, and left the
open count at **87 — unchanged**. Every defect it found was real. None of them
was found by reading code, and none was found by the 3,151-test suite. They
were found by *running the system*, and each one had been shipped by the same
loop weeks earlier.

A loop that finds defects faster than it retires them is not broken. It is
**mis-scoped**: it is doing discovery work at the point in the pipeline where
delivery work belongs, because there is no earlier point where the question
"what is this for, and how would we know it works" gets answered.

## What this amends, and what it does not replace

Three documents already exist and stay authoritative in their own halves. A
fourth overlapping protocol *is* the illegibility being complained about.

| Document | Owns | Unchanged by this |
|---|---|---|
| `docs/2026-08-05-healthclaw-2.0-playbook.md` | the **architecture** half of 2.0 — eight ratchets to zero, by strangler | yes |
| the 2.0 verification plan (artifact) + the owner briefs (local only) | the **QA** half — six feature sets, four artifacts, two sign-offs | yes |
| `docs/2026-08-03-refactor-working-protocol.md` | **per-PR** rules inside a migration slice | yes |

This document adds only the front of the pipeline — everything before a
branch is cut — and one changed definition at the back: **done**.

## The pipeline

```
  SOW ──▶ PRD ──▶ architecture review ──▶ build ──▶ QA ──▶ done
   │       │              │                 │        │       │
 what &  what it       hard truths      60–80%    real    evidence
 why      solves,      + how we prove              data    pack,
          for whom     it, before code                     not a merge
```

### 1. SOW — one page, before anything

Not a contract. A statement of what is being taken on and what is explicitly
not, written **before** the PRD so the PRD has a boundary.

- The outcome in one sentence, in the user's words.
- Who the user is. A named role, not "users".
- What is deliberately **out** of scope, with the reason.
- The decision the owner is being asked for.

A SOW that cannot name what it excludes is not scoped.

### 2. PRD — per feature set, one page

`docs/prd/` holds one per set. Six sets, six files, indexed in
`docs/prd/README.md`. Each carries the same six headings and nothing else:

1. **The problem, and whose it is**
2. **What "works" means** — the sentence a person would say
3. **How it is proven** — the four artifacts, named for this set
4. **Current state, measured** — cite an evidence pack or say *unmeasured*
5. **Known gaps** — the open issues that live in this set, listed
6. **Specifications** — links to the specs that govern it; missing ones are
   SOW items, not prose written here

The PRD is where "what problem is being solved" stops being implicit. A
feature with no PRD does not get built.

### 3. Architecture review — the hard truths gate

The CTO gate, before code. It answers four questions in writing and can
**block**:

- Does this serve the vision, or is it adjacent work that feels productive?
- What is the honest failure mode, and who notices it first?
- What does it make harder later?
- **How will we prove it works, with what data, run by whom?**

If the fourth has no answer, the design is not finished. That single question
is what today's eight defects were all downstream of.

### 4. Build — treat every PR as 60–80% complete

This is the change in posture the owner asked for, and it is a change in
**language**, not effort.

A merged PR currently reads as done. It is not: it is code that compiles,
passes unit tests, and has never been used. Under this process a merged PR is
**a candidate**, and the vocabulary follows:

- PR descriptions say *"candidate for <feature set>"*, never *"fixes X"* alone.
- The remaining 20–40% is named explicitly in the PR: what was **not** run,
  what data it has **not** seen, which surface has **not** exercised it.
- Mutation evidence stays required. It proves the test is real. It does not
  prove the feature works, and the two have been conflated.

### 5. QA — testers, end to end, with real data

The gate that does not exist today, and the whole reason for the rest.

- **A tester who did not write it.** Adversarial: ran it hostile, tried to
  make it lie.
- **Real data.** The owner's own records for the consumer and clinical sets.
  Synthetic for the guardrail core, connectors and action rail — an action
  rail proven against a real pharmacy is an incident, not evidence.
- **End to end** means through the surface a person actually uses, not through
  a test client.
- **No PHI in any artifact.** Recordings run with redaction on or scoped to
  the guardrailed view. A run that cannot be recorded PHI-free is still run;
  the register says why there is no video.

### 6. Done — an evidence pack, not a merge

A feature set is done when it has all four:

| Artifact | The rule that makes it real |
|---|---|
| Run log | every step executed, with the response. Not a narrated screenshot. |
| Recording | produced **by the run that asserts**. A separately-made video is a re-enactment, and re-enactments drift. |
| Edge-case register | what does **not** work, in writing, with issue numbers. The artifact most likely to be skipped and worth the most. |
| Two sign-offs | QA (adversarial) and end-user (not us) |

## What this fixes, mapped to the complaint

| Complaint | Mechanism |
|---|---|
| "finds more issues than it solves" | discovery moves to the architecture review, where it is cheap; the build loop stops being where problems are first discovered |
| "tracking what is broken and what is working" | every open issue has a feature set; every set has a PRD with a measured state line |
| "unless someone actually tests it in real world" | the QA gate, with a named tester and real data, is a required artifact rather than an aspiration |
| "we are now so complex" | six sets, six pages. The topology is one page on purpose. |

## How comparable products do this

Bounded, and stated as what was actually consulted: prior pattern research
recorded 2026-08-05, plus the two reference folders in this repo. No external
repository was cloned or read for this document.

- **Block Buzz** (Apache-2.0, launched 2026-07-21) — every event
  cryptographically signed; **the audit trail is the data model**, not a
  side-effect of it. Agent keypair separate from the harness with owner
  attestation: *authorization does not erase authorship*. External validation
  of the step-up / human-confirm / AuditEvent thesis.
- **Goose recipes** — versioned YAML tasks with declared MCP dependencies and
  **schema-validated output**. The lesson for us is the declaration: a task
  states its dependencies and its output shape before it runs.
- **hermes-agent** (Nous) — integration **by standards**: SKILL.md /
  agentskills.io plus native MCP over Streamable HTTP, with server-side
  per-call auth as the only auth. `hermes/` in this repo is that reference
  (a README and an `mcp.json`, no code); `openclaw/` is the thing it would
  replace.

The common pattern across all three is the one this process adopts: **the
contract is declared before the code, and the runtime enforces it.** None of
them relies on a reviewer noticing.

## First three moves, if this is adopted

1. **Label every open issue with its feature set.** 52 of 87 carry no label at
   all. Until then "what is broken" cannot be answered per feature, and the
   PRD gap sections are hand-maintained.
2. **Run the QA gate once, on set 2 (connectors), end to end.** It is the
   smallest set with a real deadline attached and two of its four kinds are
   already proven. Use it to find out what the gate actually costs before
   committing five more sets to it.
3. **Settle the two blockers that make set 1 and 2 unprovable**: the local
   stack (Docker + the missing `.env` + Aidbox activation) and the CareAgents
   redeploy. Both are the owner's.
