# Constitution

Table stakes for everyone building HealthClaw and CareAgents — human or agent.
Not aspirations. A change that violates one of these is wrong even when it
works, and reviewers should say so.

The invariants in [agent-task-guide.md](agent-task-guide.md#2-the-invariants--non-negotiable)
still outrank everything here. This file governs *how* we build; that one
governs what must never break.

Visual rules live in [design.md](../design.md).

---

## 1. Architecture

**Build deep modules.** A module earns its place by hiding more than it
exposes. A simple interface over real complexity is the goal; a wide interface
over a thin wrapper is a liability, because every caller now depends on
detail that bought them nothing.

**Judge an interface by what a caller must learn.** If using a function
correctly requires knowing how it works inside, the interface is wrong. Our
own worst examples were controls that *looked* simple and hid a second
behaviour — see the rule below.

**Keep related things together.** A feature, its tests, its failure modes and
its docs change together, so they live together. Splitting by technical layer
means every change touches five directories and no one can see the whole
behaviour at once.

**Name the seams.** The places where a module meets the outside world —
HealthClaw's HTTP client, the LLM provider, the clock, the mail sender, the
FHIR server — are where behaviour is swapped and where tests attach. Put an
adapter there deliberately rather than letting a call to a third-party SDK
sprawl through the code.

**Test at the seams, and probe the real thing.** A fake proves a call is
*made*, not *accepted*. CareAgents tests fake the HealthClaw client, so they
cannot tell you a cross-boundary call would be honoured. Anything that crosses
a boundary gets checked against a running system before it is called done.

**One control, one property.** Every check protects exactly one property, and
you must be able to say which in one sentence. Then ask what *else* could make
the check pass. If the answer includes the broken behaviour, the check is
decoration.

This is the rule the project has broken most often — six times in one week.
An id regex that carried a data-loss length cap inside a charset check. An
assertion that accepted both the fix and the bug. A monitor that counted how
many checks ran rather than which. A readiness endpoint used as a liveness
probe. See [2026-08-02-retro.md](2026-08-02-retro.md).

**Mutation-test anything load-bearing.** Break the code, watch a test go red,
restore it. A guard nobody has seen fail is a guard nobody has tested. This
takes two minutes and has caught real regressions here.

**Ship a feature and its hardening as one unit,** or ship neither. Splitting
them across merges converts "we caught two vulnerabilities in review" into
"we shipped two vulnerabilities and fixed them later." That has happened.

**Documentation is product surface.** A doc that contradicts the system is a
defect with the same severity as the code being wrong, because it is what
users act on. If you change what the system does, the docs change in the same
PR — and where it matters, add a guard that fails when they drift apart.

---

## 2. Writing

Applies to READMEs, docs, PR descriptions, commit messages, code comments,
error messages, and interface copy. Adapted from ASD-STE100, the aerospace
standard for text that must not be misread.

**One name per thing.** Pick the word and keep it. A *tenant* is never also a
workspace, an account, or an org. A *record* is never also an entry or a
document. Synonym rotation reads as variety to the writer and as three
different concepts to the reader.

**Short sentences.** Instructions: 20 words. Descriptions: 25. **No
semicolons** — if you need one, you need two sentences.

**Active verbs, not nouns.** "Analyze the bundle", not "perform an analysis
of the bundle". "Redact the record", not "apply redaction to the record".

**One helper verb, maximum.** "This may potentially help" says nothing. Write
what happens, or say you do not know.

**No marketing adjectives.** Seamless, robust, cutting-edge, powerful,
effortless, comprehensive, enterprise-grade. Delete them; the sentence is
always better. Say what it does and what it costs.

**No chatty phrasal verbs.** Remove, not *take off*. Contact, not *reach out*.
Continue, not *carry on with*.

**State limits in the same breath as claims.** "Grade A on seven properties"
needs the seven named. "Works with any FHIR server" needs the ones actually
tested. A claim that omits its scope is the kind of thing we have had to
retract.

**Errors say what happened and what to do.** "Something went wrong" is not an
error message. Name the thing that failed and the next action, and never leak
PHI or a token into the text.

This is not a banned-word list. The point is that vague writing hides broken
thinking, and a reader acting on our text can be a patient.

---

## 3. Working with agents

**The human makes the strategic calls.** An agent is a fast tactical
engineer: excellent at the change in front of it, structurally unable to weigh
what the codebase should look like in six months or what a patient will feel.
Architecture, product direction, and anything irreversible stay with the
person. Decision rights are in `.claude/team.md`.

**Never accept the first output.** Quality comes from iteration. A first draft
that runs is a starting point, not a result. If a change was accepted without
a single revision, it probably was not read.

**Record recurring mistakes where they will be read again.** When an agent —
or a person — makes the same error twice, write it down in the guide or the
retro, not in a chat message that scrolls away. This file and
[agent-task-guide.md](agent-task-guide.md#6-traps-that-have-actually-bitten-this-project)
exist because of that rule.

**Report faithfully.** If tests fail, say so and show the output. If a step
was skipped, say which. "Done" means verified, not "the code is written". An
agent that reports success it did not confirm is worse than one that fails
loudly.

**Verify the operation, not the exit code.** `git push -q` has silently failed
here and a guard was reported as shipped while the remote carried the old
snippet. Check the remote ref. Read the deployed build's version. Curl the
endpoint. The pattern that keeps hurting us is an operation that *looked*
successful because nothing said otherwise.

---

## 4. Deliberately not adopted

Recorded with reasons so nobody adds them later assuming an oversight.

- **A banned-word list as the writing rule.** Banning "delve" moves slop
  somewhere else. The length, naming and hedging rules above attack the cause.
- **WebGL heroes, smooth-scroll libraries, skeuomorphism.** Blocked by our
  CSP, hostile to in-app webviews, and wrong for someone checking a lab
  result. See [design.md](../design.md#where-we-depart-from-general-anti-slop-advice).
- **A "cheeky" or "abrasive" brand voice.** People open this product worried.
  Edge reads as not taking them seriously.
- **Banning system fonts outright.** Our display faces are distinctive;
  `-apple-system` in the *fallback* chain is a performance decision on phones.
