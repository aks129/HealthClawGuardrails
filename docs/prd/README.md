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

Every open issue in the tracker appears in exactly one of these seven pages.
The mapping was done by title on 2026-08-16 against 87 open issues, **52 of
which carry no label at all** — so the tables are the current answer to "what
is broken in this feature", and labelling the tracker to match is the first
move in the process document.
