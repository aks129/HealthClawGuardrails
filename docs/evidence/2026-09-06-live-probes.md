# 2026-09-06: live adversarial probes on the night's guardrail changes

A second agent session drove each change of the night against a running
app on a migrated database, the real intake questionnaire and the real MCP
server, rather than reading the diff. Findings went to the pull request or
issue as comments; nothing was pushed or armed by that session. This note
records what was tried and what it found, so the next reader knows which
claims were measured and which were only asserted.

## What was found

Two real bypasses, both fixed the same night, both invisible to the
pinned tests and the full suite of the change they sat in.

- **#658, the human-gate payload digest.** The digest was a plain hash in
  the same table a writer with database access could rewrite. Forging the
  payload and the digest in one transaction walked through the check and
  the approval audit line vouched for the forgery as a success. Fixed by
  keying the digest with a key derived from the step-up secret; nine
  guessed keys refuse, with a control proving the harness forges when
  given the real key. Lesson: a hash stored beside what it attests attests
  nothing.
- **#664, the extraction type check.** A definition names its resource type
  twice, in the StructureDefinition URL and in the element path, and the
  check read only the path. `AllergyIntolerance#Patient.name.given` filed
  an allergen into the Patient. Fixed by checking both where the URL names
  a type; a profile URL cannot be checked without resolving it, and the
  code says so.

One live gap on main, found by the reviewer's question rather than a probe
and then reproduced live: `$extract` commit mode wrote an AllergyIntolerance
on a step-up token alone, with statuses as bare strings the validator waved
through. Closed by #668, which jumped the queue.

## What held

Each of these was driven with the variants named in the request and at
least one the prober added:

| Change | Probed | Result |
|---|---|---|
| #661 seal as a class | five bypass writers, double tap, null digest | held |
| #665 review content list | seeded rows in both resolution states | held; unanchored rows stay out of population |
| #572 part 2 (held stack) | forged marker, edited marked row, MCP write tool, mixed groups | held |
| #668 refusal | gap reproduced on main; MCP tool; mixed Observation and allergy bundle | refuses atomically |
| kernel slice 19 read-auth | twelve scenarios incl. cross-tenant token via the bearer alias | held, no oracle |
| #591 exempt paths | seventeen wrong-method requests | 13 of 17 reached the resource handler on main; all 17 answer 405 on the branch |
| kernel slice 20 rate limiter | six scenarios incl. a validator that raises | held; buckets inspected directly |
| #558 revive audit | both ingest paths, cross-tenant tombstone | held; a tombstone under one tenant is never lifted by another |
| #659 bound approval token | swap via raw SQL, restore and retap, bare token, cross-tenant, re-mint | held; a refused tap spends no nonce |

## What this changes about how we work

A guardrail change is not done when its pins and its mutation are green.
The two bypasses were in changes whose full suites passed. Before a
guardrail change is armed, ask for a pass against a live database by a
session that did not write it, with the threat model the change's own
body names, and keep the sweep as a pin when it fits (the guessed-key
sweep on #658 became one).
