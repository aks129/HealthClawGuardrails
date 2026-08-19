# Product requirements, by feature set

Six sets. Six pages. Same six headings on each, so they cannot drift in
shape.

| # | Set | What "works" means | Measured state |
|---|---|---|---|
| [1](01-guardrail-core.md) | Guardrail core | conformance A local / B proxy, gap named | **A 7/7 local; proxy unmeasured** |
| [2](02-connectors.md) | Upstream connectors | each kind runs its own live walkthrough | **2 of 4 proven live** |
| [3](03-consumer-journey.md) | CareAgents journey | a person finishes it without help | **unmeasured** |
| [4](04-action-rail.md) | Action rail | nothing executes without an out-of-band human | **unmeasured; #215 says nothing could execute** |
| [5](05-clinical-rails.md) | Clinical rails | a clinician does not have to correct it | **unmeasured** |
| [6](06-surfaces.md) | Surfaces | every tool answers, and refuses when it should | **partial; #290 blocks the locked endpoint** |

Also here: [cross-cutting issues](00-cross-cutting.md) that no set owns.

## How to read a state line

*Unmeasured* means **nobody has run it end to end**, not that it is broken.
Four of the six sets are unmeasured. That is the finding, not a formatting
artefact — and it is why
[the delivery process](../2026-08-16-delivery-process.md) puts a QA gate with
a named tester and real data between "merged" and "done".

## How to read a gap table

Every open issue in the tracker carries exactly one `set:` label, and appears
on the page that owns it. **One issue is listed twice on purpose:** #214 is
labelled `set: 1` and appears in both [01](01-guardrail-core.md) and
[04](04-action-rail.md), because set 1 owns proving it and set 4 owns
designing its closure. Page 04 explains that at the foot of its gap table.
The label is the partition; a page listing is a reading aid and may
cross-reference.
The mapping was done by title on 2026-08-16 against 87 open issues, **52 of
which carry no label at all** — so the tables are the current answer to "what
is broken in this feature", and labelling the tracker to match is the first
move in the process document.

That move is now done, and the sentence above is the historical state: as of
2026-08-16 all 87 open issues carry a `set:` label. The tracker, not this
page, is the live answer.

## Two axes: `area:` and `set:`

The tracker carries two label families. They are not competing taxonomies and
neither is derived from the other. An issue can carry both. `area:` is applied
where someone thought to; **`set:` is applied to every open issue, without
exception** — that is what makes the queries below a partition rather than a
sample.

| Family | Answers | Example |
|---|---|---|
| `area:` | which part of the **code** does this live in | `area: fhir`, `area: careagents` |
| `set:` | which **PRD owns proving it works** | `set: 1 guardrail-core` |

The distinction earns its keep where the two disagree, and they disagree
often. #53 (broaden the labs analyte table) is `area: fhir` code but
`set: 5 clinical-rails`, because a clinician reading the output is what
proves it. #136 (iMessage) is `area: surfaces` but `set: 3 consumer-journey`,
because the thing to demonstrate is a patient finishing, not a transport
working. #56 (carve up `r6/routes.py`) is `area: fhir` and
`set: cross-cutting`, because no feature set gets more provable when it
lands. Fold either axis into the other and those issues sit in a bucket whose
owner cannot close them.

So: `area:` routes a fix to the code. `set:` routes an issue to the person who
has to demonstrate the feature works, and to the evidence pack that
demonstration lands in.

### The query

What is broken in feature *N*:

```sh
gh issue list --label "set: 3 consumer-journey" --state open
```

Every open issue carries exactly one `set:` label, so the seven queries
partition the tracker with nothing double-counted and nothing dropped. Issues
that no feature set owns are `set: cross-cutting` ([00](00-cross-cutting.md)).

When you file an issue, add its `set:` label. An unlabelled issue is invisible
to the only question this structure exists to answer.
